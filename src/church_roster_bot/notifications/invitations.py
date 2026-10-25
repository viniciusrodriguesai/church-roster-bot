"""Invitation delivery is independent from explicit roster acceptance."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Coordinator, Volunteer
from church_roster_bot.ministries.models import Role
from church_roster_bot.notifications.outbox import apply_window, window_open
from church_roster_bot.scheduling.lifecycle import invitation_text
from church_roster_bot.scheduling.models import ThirdDayRequest
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import buttons_payload, template_payload, text_payload

DELIVERY_LABELS = {
    "pending": "envio pendente na fila",
    "sending": "envio em andamento; não reenviar",
    "sent": "enviado; aguardando resposta do voluntário",
    "delivered": "entregue; aguardando resposta do voluntário",
    "read": "lido; aguardando resposta do voluntário",
    "blocked_consent": "envio bloqueado: sem consentimento (INICIAR para ativar)",
    "blocked_window": "envio bloqueado: template aprovado necessário fora da janela de 24 horas",
    "failed": "falha no envio",
    "unknown": "envio incerto; não reenviar automaticamente para evitar duplicidade",
    "cancelled": "envio cancelado: convite já respondido",
}


def notify_coordinator(
    db: Session,
    invitation: ThirdDayRequest,
    status: str,
    config: Settings,
    error: str | None = None,
) -> None:
    key = f"invitation:{invitation.id}:{status}"
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.dedupe_key == key)):
        return
    coordinator = db.get(Coordinator, invitation.coordinator_id)
    member = db.get(Volunteer, invitation.volunteer_id)
    label = DELIVERY_LABELS.get(status, status)
    detail = invitation_text(db, invitation).rsplit("\nEnvie ", 1)[0]
    body = f"{member.name}: {detail}\n{label}. Aceite explícito ainda é obrigatório."
    if status in {"aceito", "recusado"}:
        body = f"{member.name}: {detail}\n{label}."
    if error:
        body += f" Código: {error}."
    outgoing = OutboundMessage(
        recipient_e164=coordinator.phone_e164, payload=text_payload(body), dedupe_key=key
    )
    apply_window(db, outgoing, config)
    db.add(outgoing)


def prepare_invitation(db: Session, outgoing: OutboundMessage, config: Settings) -> None:
    invitation = db.get(ThirdDayRequest, outgoing.invitation_id)
    member = db.get(Volunteer, invitation.volunteer_id)
    outgoing.error_code = None
    if invitation.status != "pending":
        outgoing.status = "cancelled"
        return
    if (
        not member.active
        or not member.invite_opt_in
        or member.phone_e164 != outgoing.recipient_e164
    ):
        outgoing.status = "blocked_consent"
        outgoing.error_code = "consent_required"
        return
    if window_open(db, member.phone_e164):
        body = invitation_text(db, invitation)
        # Long justifications must not be truncated to the interactive body limit.
        outgoing.payload = (
            buttons_payload(
                body,
                [(f"ACEITAR {invitation.id}", "ACEITAR"), (f"RECUSAR {invitation.id}", "RECUSAR")],
            )
            if len(body) <= 1024
            else text_payload(body)
        )
    elif config.whatsapp_invite_template:
        slot = db.get(Slot, invitation.slot_id)
        service = db.get(WorshipService, slot.service_id)
        outgoing.payload = template_payload(
            config.whatsapp_invite_template,
            config.whatsapp_template_language,
            [
                str(invitation.id),
                service.service_date.strftime("%d/%m/%Y"),
                service.start_time.strftime("%H:%M"),
                db.get(Role, slot.role_id).name,
                invitation.justification,
            ],
            quick_replies=[f"ACEITAR {invitation.id}", f"RECUSAR {invitation.id}"],
        )
    else:
        outgoing.status = "blocked_window"
        outgoing.error_code = "template_required"


def enqueue_invitation(
    db: Session, invitation: ThirdDayRequest, config: Settings
) -> OutboundMessage:
    outgoing = db.scalar(
        select(OutboundMessage).where(OutboundMessage.invitation_id == invitation.id)
    )
    if outgoing is not None:
        return outgoing
    member = db.get(Volunteer, invitation.volunteer_id)
    outgoing = OutboundMessage(
        invitation_id=invitation.id,
        recipient_e164=member.phone_e164,
        payload=text_payload(invitation_text(db, invitation)),
        status="pending",
    )
    prepare_invitation(db, outgoing, config)
    db.add(outgoing)
    notify_coordinator(db, invitation, outgoing.status, config, outgoing.error_code)
    return outgoing


def activate_invitations(db: Session, volunteer_id: int, config: Settings) -> None:
    invitations = db.scalars(
        select(ThirdDayRequest).where(
            ThirdDayRequest.volunteer_id == volunteer_id, ThirdDayRequest.status == "pending"
        )
    ).all()
    for invitation in invitations:
        outgoing = enqueue_invitation(db, invitation, config)
        if outgoing.status in {"blocked_consent", "blocked_window"} and outgoing.attempts == 0:
            outgoing.status = "pending"
            prepare_invitation(db, outgoing, config)
            notify_coordinator(db, invitation, outgoing.status, config, outgoing.error_code)
