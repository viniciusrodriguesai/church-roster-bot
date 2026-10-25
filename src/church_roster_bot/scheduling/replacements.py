"""Invite one eligible substitute at a time; the original remains until accepted."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import database_utc, utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.notifications.automation import notify_offer
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.notifications.members import member_notice
from church_roster_bot.scheduling.candidates import ranked_candidates
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.invitations import create_invitation
from church_roster_bot.scheduling.lifecycle import audit
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, ReplacementRequest
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_fingerprint, service_start


def replacement_completed(db: Session, request_id: int, config: Settings) -> None:
    request = db.get(ReplacementRequest, request_id)
    original = db.get(Assignment, request.assignment_id)
    slot = db.get(Slot, original.slot_id)
    service = db.get(WorshipService, slot.service_id)
    member_notice(
        db,
        original.volunteer_id,
        f"replacement:{request.id}:fulfilled",
        f"Substituição do pedido #{request.id} confirmada para {service.service_date:%d/%m/%Y} "
        f"às {service.start_time:%H:%M}. Sua participação original foi substituída.",
        config,
    )


def dispatch_replacements(
    db: Session, roster: RosterEngine, config: Settings, *, now: datetime | None = None
) -> int:
    instant = utc_now(now)
    requests = db.scalars(
        select(ReplacementRequest)
        .where(ReplacementRequest.status == "pending")
        .order_by(ReplacementRequest.id)
    ).all()
    offers = db.scalars(
        select(AutomationInvitation)
        .where(AutomationInvitation.kind == "replacement", AutomationInvitation.status == "pending")
        .order_by(AutomationInvitation.id)
    ).all()
    request_ids = {r.id for r in requests} | {o.replacement_request_id for o in offers}
    if not request_ids:
        return 0
    originals = db.scalars(
        select(Assignment)
        .join(ReplacementRequest, ReplacementRequest.assignment_id == Assignment.id)
        .where(ReplacementRequest.id.in_(request_ids))
    ).all()
    slot_ids = {a.slot_id for a in originals}
    church_ids = set(
        db.scalars(
            select(Ministry.church_id)
            .join(WorshipService, WorshipService.ministry_id == Ministry.id)
            .join(Slot, Slot.service_id == WorshipService.id)
            .where(Slot.id.in_(slot_ids))
        )
    )
    # The same total order is used by direct registration, queue allocation and
    # acceptance. Requests are locked only after members, slots and existing offers.
    list(
        db.scalars(
            select(Volunteer)
            .where(Volunteer.church_id.in_(church_ids))
            .order_by(Volunteer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    list(
        db.scalars(
            select(Slot)
            .where(Slot.id.in_(slot_ids))
            .order_by(Slot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    offers = db.scalars(
        select(AutomationInvitation)
        .where(AutomationInvitation.id.in_([o.id for o in offers]))
        .order_by(AutomationInvitation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    requests = db.scalars(
        select(ReplacementRequest)
        .where(ReplacementRequest.id.in_(request_ids))
        .order_by(ReplacementRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    list(
        db.scalars(
            select(Assignment)
            .where(Assignment.id.in_([a.id for a in originals]))
            .order_by(Assignment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for offer in offers:
        request = db.get(ReplacementRequest, offer.replacement_request_id)
        member = db.get(Volunteer, offer.volunteer_id)
        slot = db.get(Slot, offer.slot_id)
        try:
            if request.status != "pending" or not member.invite_opt_in:
                raise RosterError("replacement request or consent changed")
            if service_fingerprint(db, slot) != offer.service_fingerprint:
                raise RosterError("service changed")
            roster.check_replacement(db, member.id, request.id)
        except RosterError:
            offer.status, offer.responded_at = "cancelled", instant
            notify_offer(db, offer, "cancelled", config)
        if offer.status == "pending" and database_utc(offer.expires_at) <= instant:
            offer.status, offer.responded_at = "expired", instant
            notify_offer(db, offer, "expired", config)
    created = 0
    for request in requests:
        if request.status != "pending":
            continue
        original = db.get(Assignment, request.assignment_id)
        slot = db.get(Slot, original.slot_id)
        service = db.get(WorshipService, slot.service_id)
        ministry = db.get(Ministry, service.ministry_id)
        try:
            started = service_start(db, service) <= instant
        except ValueError:
            notify_ministry(
                db,
                ministry.id,
                f"replacement:{request.id}:invalid_time",
                f"Pedido #{request.id}: horário local do culto inválido. "
                "Corrija o horário ou fuso antes de retomar a busca automática.",
                config,
            )
            continue
        if original.status != "replacement_requested" or started:
            request.status = "cancelled"
            notify_ministry(
                db,
                ministry.id,
                f"replacement:{request.id}:closed",
                f"Pedido de substituição #{request.id} encerrado sem troca automática; "
                "o culto começou ou a participação original mudou. Revise a escala.",
                config,
            )
            audit(
                db,
                ministry.church_id,
                "system",
                0,
                "replacement_closed",
                "replacement_request",
                request.id,
            )
            continue
        if db.scalar(
            select(AutomationInvitation.id).where(
                AutomationInvitation.replacement_request_id == request.id,
                AutomationInvitation.status == "pending",
            )
        ):
            continue
        tried = set(
            db.scalars(
                select(AutomationInvitation.volunteer_id).where(
                    AutomationInvitation.replacement_request_id == request.id
                )
            )
        ) | {original.volunteer_id}
        candidates = ranked_candidates(
            db, roster, slot.id, excluded=tried, replacement_request_id=request.id, now=instant
        )
        selected = None
        for member_id, score in candidates:
            if not db.scalar(
                select(AutomationInvitation.id).where(
                    AutomationInvitation.volunteer_id == member_id,
                    AutomationInvitation.slot_id == slot.id,
                    AutomationInvitation.status == "pending",
                )
            ):
                selected = member_id, score
                break
        if selected is None:
            notify_ministry(
                db,
                ministry.id,
                f"replacement:{request.id}:no_candidate",
                f"Busca do pedido #{request.id}: nenhum substituto elegível com consentimento "
                f"encontrado para {service.service_date:%d/%m/%Y} {service.start_time:%H:%M}. "
                "A participação original continua sem substituição.",
                config,
            )
            continue
        member_id, score = selected
        offer = create_invitation(
            db,
            config,
            kind="replacement",
            source_key=f"replacement:{request.id}:{member_id}",
            replacement_request_id=request.id,
            volunteer_id=member_id,
            slot_id=slot.id,
            reason=f"Substituição da participação #{original.id}. {score.explanation}",
            now=instant,
        )
        notify_ministry(
            db,
            ministry.id,
            f"replacement:{request.id}:candidate:{member_id}",
            f"Pedido #{request.id}: convite A{offer.id} registrado para um substituto elegível. "
            "A troca depende do aceite e da revalidação da vaga.",
            config,
        )
        audit(
            db,
            ministry.church_id,
            "system",
            0,
            "replacement_offered",
            "replacement_request",
            request.id,
            {"invitation_id": offer.id, "volunteer_id": member_id},
        )
        created += 1
    return created
