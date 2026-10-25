"""Durable, consent-aware delivery for normal invitations from roster automation."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import database_utc, utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Role
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.notifications.outbox import window_open
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.models import AutomationInvitation
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_fingerprint, service_start
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import buttons_payload, template_payload, text_payload

LABELS = {
    "accepted": "aceito; participação confirmada",
    "declined": "recusado",
    "expired": "expirado",
    "cancelled": "cancelado; elegibilidade ou vaga mudou",
    "blocked_consent": "bloqueado por falta de consentimento",
    "blocked_window": "bloqueado: template aprovado necessário",
    "failed": "falha no envio",
    "unknown": "envio incerto; não reenviar automaticamente",
}


def offer_text(db: Session, offer: AutomationInvitation) -> str:
    slot = db.get(Slot, offer.slot_id)
    service = db.get(WorshipService, slot.service_id)
    role = db.get(Role, slot.role_id)
    return (
        f"Convite de escala A{offer.id}: {service.service_date:%d/%m/%Y} "
        f"às {service.start_time:%H:%M} — {role.name}.\n"
        f"Motivo: {offer.reason}\n"
        f"Envie ACEITAR A{offer.id} ou RECUSAR A{offer.id}. "
        "A participação só será confirmada após seu aceite e revalidação da vaga."
    )


def notify_offer(
    db: Session,
    offer: AutomationInvitation,
    status: str,
    config: Settings,
    error: str | None = None,
) -> None:
    slot = db.get(Slot, offer.slot_id)
    service = db.get(WorshipService, slot.service_id)
    member = db.get(Volunteer, offer.volunteer_id)
    body = (
        f"Convite A{offer.id} — {member.name}, {service.service_date:%d/%m/%Y} "
        f"{service.start_time:%H:%M}: {LABELS.get(status, status)}."
    )
    if error:
        body += f" Código: {error}."
    notify_ministry(db, service.ministry_id, f"automation:{offer.id}:{status}", body, config)


def prepare_offer(
    db: Session,
    outgoing: OutboundMessage,
    config: Settings,
    *,
    now: datetime | None = None,
) -> None:
    offer = db.get(AutomationInvitation, outgoing.automation_invitation_id)
    member = db.get(Volunteer, offer.volunteer_id)
    instant = utc_now(now)
    outgoing.error_code = None
    if offer.status != "pending":
        outgoing.status = "cancelled"
        return
    if database_utc(offer.expires_at) <= instant:
        offer.status = "expired"
        outgoing.status = "cancelled"
        notify_offer(db, offer, "expired", config)
        return
    if (
        not member.active
        or not member.invite_opt_in
        or member.phone_e164 != outgoing.recipient_e164
    ):
        outgoing.status, outgoing.error_code = "blocked_consent", "consent_required"
        return
    slot = db.get(Slot, offer.slot_id)
    service = db.get(WorshipService, slot.service_id)
    try:
        if service_fingerprint(db, slot) != offer.service_fingerprint:
            raise RosterError("service changed after invitation was prepared")
        if service_start(db, service) <= instant:
            raise RosterError("service has already started")
        roster = RosterEngine(sessionmaker(bind=db.get_bind()))
        if offer.kind == "replacement":
            roster.check_replacement(db, member.id, offer.replacement_request_id)
        else:
            roster.check_assignment(db, member.id, slot.id, allow_published=True)
    except (RosterError, ValueError):
        offer.status, outgoing.status = "cancelled", "cancelled"
        notify_offer(db, offer, "cancelled", config)
        return
    body = offer_text(db, offer)
    if window_open(db, member.phone_e164):
        outgoing.payload = (
            buttons_payload(
                body, [(f"ACEITAR A{offer.id}", "ACEITAR"), (f"RECUSAR A{offer.id}", "RECUSAR")]
            )
            if len(body) <= 1024
            else text_payload(body)
        )
    elif config.whatsapp_automation_template:
        outgoing.payload = template_payload(
            config.whatsapp_automation_template,
            config.whatsapp_template_language,
            [body],
            quick_replies=[f"ACEITAR A{offer.id}", f"RECUSAR A{offer.id}"],
        )
    else:
        outgoing.status, outgoing.error_code = "blocked_window", "template_required"


def enqueue_offer(db: Session, offer: AutomationInvitation, config: Settings) -> OutboundMessage:
    existing = db.scalar(
        select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offer.id)
    )
    if existing:
        return existing
    outgoing = OutboundMessage(
        automation_invitation_id=offer.id,
        recipient_e164=db.get(Volunteer, offer.volunteer_id).phone_e164,
        payload=text_payload(offer_text(db, offer)),
        status="pending",
    )
    prepare_offer(db, outgoing, config)
    db.add(outgoing)
    if outgoing.status in {"blocked_consent", "blocked_window"}:
        notify_offer(db, offer, outgoing.status, config, outgoing.error_code)
    return outgoing


def activate_offers(db: Session, volunteer_id: int, config: Settings) -> None:
    for outgoing in db.scalars(
        select(OutboundMessage)
        .join(
            AutomationInvitation,
            AutomationInvitation.id == OutboundMessage.automation_invitation_id,
        )
        .where(
            AutomationInvitation.volunteer_id == volunteer_id,
            AutomationInvitation.status == "pending",
            OutboundMessage.status.in_(("blocked_consent", "blocked_window")),
            OutboundMessage.attempts == 0,
        )
    ).all():
        outgoing.status = "pending"
        prepare_offer(db, outgoing, config)
