"""Persistent deterministic conversation router shared by Meta and the simulator."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from church_roster_bot.administration.conversation import AdminConversationMixin
from church_roster_bot.administration.service import (
    actor_for,
    permitted_ministries,
    require_ministry,
)
from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import (
    Coordinator,
    CoordinatorMinistry,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.scheduling.errors import MonthlyLimit, NoCapacity, RosterError
from church_roster_bot.scheduling.lifecycle import (
    invitation_text,
    publish_schedule,
    request_replacement,
    schedule_for,
)
from church_roster_bot.scheduling.models import (
    Assignment,
    MonthlySchedule,
    ThirdDayRequest,
)
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import (
    IncomingMessage,
    buttons_payload,
    list_payload,
    template_payload,
    text_payload,
)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Bot(AdminConversationMixin):
    def __init__(self, session_factory, config: Settings):
        self.session_factory = session_factory
        self.config = config
        self.roster = RosterEngine(session_factory)

    def _send(self, db: Session, phone: str, payload: dict, *, proactive: bool = False) -> None:
        if proactive:
            session = db.scalar(
                select(ConversationSession).where(ConversationSession.phone_e164 == phone)
            )
            open_window = session and datetime.now(UTC) - aware(
                session.last_inbound_at
            ) < timedelta(hours=24)
            if not open_window:
                if not self.config.whatsapp_invite_template:
                    db.add(
                        OutboundMessage(
                            recipient_e164=phone,
                            payload=payload,
                            status="blocked_window",
                            error_code="template_required",
                        )
                    )
                    return
                payload = template_payload(
                    self.config.whatsapp_invite_template, self.config.whatsapp_template_language
                )
        db.add(OutboundMessage(recipient_e164=phone, payload=payload))

    def _text(self, db: Session, phone: str, body: str) -> None:
        while len(body) > 4096:
            cut = body.rfind("\n", 0, 3500)
            if cut < 1:
                cut = 3500
            self._send(db, phone, text_payload(body[:cut]))
            body = body[cut:].lstrip("\n")
        self._send(db, phone, text_payload(body))

    def _options(self, db: Session, phone: str, body: str, options: list[tuple[str, str]]) -> None:
        if 0 < len(options) <= 10 and all(
            len(ident) <= 200 and len(label) <= 24 for ident, label in options
        ):
            self._send(db, phone, list_payload(body, options))
        else:
            self._text(
                db, phone, body + "\n" + "\n".join(f"{key} — {label}" for key, label in options)
            )

    def _state(self, session: ConversationSession, state: str, **data) -> None:
        session.state = state
        session.data = {**session.data, **data}

    def _menu(self, db: Session, session: ConversationSession, admin, coordinator, volunteer):
        phone = session.phone_e164
        session.state = "menu"
        if admin or coordinator:
            ministries = permitted_ministries(db, admin, coordinator)
            selected = session.data.get("ministry_id")
            if selected not in {m.id for m in ministries}:
                if not ministries:
                    self._text(db, phone, "Você não tem ministérios autorizados.")
                    return
                if len(ministries) > 1:
                    self._state(session, "select_ministry")
                    self._options(
                        db,
                        phone,
                        "Escolha o ministério:",
                        [(str(m.id), m.name) for m in ministries],
                    )
                    return
                session.data = {**session.data, "ministry_id": ministries[0].id}
            self._options(
                db,
                phone,
                f"ADMINISTRAÇÃO — {db.get(Ministry, session.data['ministry_id']).name.upper()}"
                "\nEscolha uma opção:",
                [
                    ("1", "Gerenciar diáconos"),
                    ("2", "Configurar cultos"),
                    ("3", "Configurar vagas"),
                    ("4", "Abrir inscrições"),
                    ("5", "Ver escala"),
                    ("6", "Vagas pendentes"),
                    ("7", "Gerenciar exceções"),
                    ("8", "Gerar escala"),
                    ("9", "Participantes"),
                ],
            )
            return
        if volunteer:
            pending = db.scalars(
                select(ThirdDayRequest.id).where(
                    ThirdDayRequest.volunteer_id == volunteer.id,
                    ThirdDayRequest.status == "pending",
                )
            ).all()
            alert = (
                f"\nConvites pendentes: {', '.join(map(str, pending))}. "
                "Envie CONVITES para datas, funções e justificativas."
                if pending
                else ""
            )
            self._options(
                db,
                phone,
                f"ESCALAS DA IGREJA\nOlá, {volunteer.name}!{alert}",
                [
                    ("1", "Escolher meus dias"),
                    ("2", "Consultar escala"),
                    ("3", "Indisponibilidade"),
                    ("4", "Substituição"),
                    ("5", "Participações"),
                    ("6", "Ajuda"),
                ],
            )
            return
        self._text(db, phone, "Número não cadastrado. Fale com a coordenação.")

    def handle(self, db: Session, message: IncomingMessage) -> None:
        now = datetime.now(UTC)
        phone = message.phone_e164
        session = db.scalar(
            select(ConversationSession)
            .where(ConversationSession.phone_e164 == phone)
            .with_for_update()
        )
        if session is None:
            session = ConversationSession(
                phone_e164=phone,
                state="menu",
                data={},
                expires_at=now,
                last_inbound_at=now,
                rate_window_at=now,
            )
            db.add(session)
            db.flush()
        if now - aware(session.rate_window_at) >= timedelta(minutes=1):
            session.rate_window_at = now
            session.rate_count = 0
        session.rate_count += 1
        if session.rate_count > self.config.inbound_limit_per_minute:
            return
        expired = aware(session.expires_at) < now
        if expired:
            session.state = "menu"
            session.data = {"ministry_id": session.data.get("ministry_id")}
        session.last_inbound_at = now
        session.expires_at = now + timedelta(minutes=self.config.conversation_ttl_minutes)
        admin, coordinator, volunteer = actor_for(db, phone)
        text = message.text.strip()
        upper = text.upper()
        if expired and upper not in {"OI", "MENU", "CANCELAR", "PARAR", "STOP", "INICIAR", "START"}:
            self._menu(db, session, admin, coordinator, volunteer)
            return
        if upper in {"PARAR", "STOP", "SAIR"}:
            if volunteer:
                volunteer.invite_opt_in = False
            self._text(db, phone, "Convites desativados. Envie INICIAR para reativar.")
            return
        if upper in {"INICIAR", "START"}:
            if volunteer:
                volunteer.invite_opt_in = True
            self._text(db, phone, "Convites ativados.")
            return
        if upper in {"MENU", "CANCELAR"}:
            session.data = {"ministry_id": session.data.get("ministry_id")}
            self._menu(db, session, admin, coordinator, volunteer)
            return
        if upper.startswith(("ACEITAR ", "RECUSAR ")) and volunteer:
            try:
                request_id = int(upper.split()[1])
                invitation = db.get(ThirdDayRequest, request_id)
                assignment_id = self.roster.respond_third_day(
                    volunteer.id, request_id, upper.startswith("ACEITAR"), db=db
                )
                self._text(
                    db,
                    phone,
                    "Convite aceito; inscrição confirmada."
                    if assignment_id
                    else "Convite recusado.",
                )
                if invitation:
                    invite_coordinator = db.get(Coordinator, invitation.coordinator_id)
                    self._send(
                        db,
                        invite_coordinator.phone_e164,
                        text_payload(
                            f"Convite excepcional #{request_id}: "
                            f"{'aceito' if assignment_id else 'recusado'}."
                        ),
                        proactive=True,
                    )
                if assignment_id:
                    slot = db.get(Slot, invitation.slot_id)
                    service = db.get(WorshipService, slot.service_id)
                    schedule = schedule_for(db, service.ministry_id, service.service_date)
                    if schedule and schedule.state == "published":
                        publish_schedule(db, service.ministry_id, service.service_date)
            except (ValueError, RosterError) as exc:
                self._text(db, phone, f"Convite não aplicado: {exc}")
            return
        if upper == "FUNCOES" and volunteer:
            roles = db.scalars(
                select(Role.name)
                .join(VolunteerRole, VolunteerRole.role_id == Role.id)
                .where(VolunteerRole.volunteer_id == volunteer.id, Role.active.is_(True))
            ).all()
            self._text(db, phone, "Funções autorizadas: " + (", ".join(roles) or "nenhuma"))
            return
        if upper == "CONVITES" and volunteer:
            invitations = db.scalars(
                select(ThirdDayRequest).where(
                    ThirdDayRequest.volunteer_id == volunteer.id,
                    ThirdDayRequest.status == "pending",
                )
            ).all()
            self._text(
                db,
                phone,
                "\n\n".join(invitation_text(db, invitation) for invitation in invitations)
                or "Nenhum convite pendente.",
            )
            return
        if upper == "ADMIN" and admin:
            self._state(session, "general_menu")
            self._text(
                db,
                phone,
                "Admin geral: MINISTERIO <nome>, RENOMEAR_MINISTERIO <id> <nome>, "
                "FUNCAO <ministério_id> <nome>, FUSO <zona IANA>, "
                "COORDENADOR <telefone> <nome> | <ids de ministérios>, "
                "EDITAR_COORD <id> NOME/TELEFONE/MINISTERIOS/ATIVAR/DESATIVAR.",
            )
            return
        if upper == "OI":
            self._menu(db, session, admin, coordinator, volunteer)
            return
        try:
            if session.state == "select_ministry":
                ministry_id = int(text)
                require_ministry(db, ministry_id, admin, coordinator)
                self._state(session, "menu", ministry_id=ministry_id)
                self._menu(db, session, admin, coordinator, volunteer)
            elif session.state.startswith("general_"):
                with db.begin_nested():
                    self._general(db, session, text, admin)
            elif admin or coordinator:
                ministry_id = session.data.get("ministry_id")
                ministry = require_ministry(db, ministry_id, admin, coordinator)
                actor_type, actor_id = (
                    ("admin", admin.id) if admin else ("coordinator", coordinator.id)
                )
                with db.begin_nested():
                    self._coordinator(
                        db, session, text, ministry, actor_type, actor_id, coordinator
                    )
            elif volunteer:
                with db.begin_nested():
                    self._volunteer(db, session, text, volunteer)
            else:
                self._menu(db, session, admin, coordinator, volunteer)
        except (
            RosterError,
            ValueError,
            ValidationError,
            KeyError,
            IndexError,
            IntegrityError,
        ) as exc:
            if isinstance(exc, NoCapacity):
                detail = "vaga lotada"
            elif isinstance(exc, MonthlyLimit):
                detail = "limite mensal atingido"
            elif isinstance(exc, IntegrityError):
                detail = "já existe um cadastro ou configuração com esses dados"
            else:
                detail = str(exc)
            self._text(
                db, phone, f"Não foi possível concluir: {detail}. Envie MENU para recomeçar."
            )

    def _volunteer(
        self, db: Session, session: ConversationSession, text: str, member: Volunteer
    ) -> None:
        phone, state = session.phone_e164, session.state
        if state == "menu":
            if text == "1":
                self._choose_dates(db, session, member)
            elif text == "2":
                self._text(db, phone, self._own_assignments(db, member))
            elif text == "3":
                self._state(session, "unavailable")
                self._text(
                    db,
                    phone,
                    "Informe início e fim: AAAA-MM-DD AAAA-MM-DD. "
                    "Motivo é opcional e não será pedido.",
                )
            elif text == "4":
                assignments = self._future_assignments(db, member.id)
                self._state(session, "replacement_pick", assignment_ids=[a.id for a in assignments])
                self._text(
                    db,
                    phone,
                    "Escolha o ID da participação:\n" + self._assignment_lines(db, assignments),
                )
            elif text == "5":
                count = self.roster.monthly_days(member.id, date.today(), db=db)
                self._text(db, phone, f"Participações neste mês: {count} dia(s) distinto(s).")
            elif text == "6":
                self._text(
                    db,
                    phone,
                    "Envie MENU para opções, FUNCOES para permissões, "
                    "CANCELAR para sair de um fluxo, PARAR para suspender convites.",
                )
            else:
                self._menu(db, session, None, None, member)
        elif state == "choose_date":
            service_id = int(text.removeprefix("S"))
            if service_id not in session.data.get("service_ids", []):
                raise RosterError("culto não disponível")
            slots = self._eligible_slots(db, member.id, service_id)
            self._state(
                session, "choose_role", service_id=service_id, slot_ids=[s.id for s in slots]
            )
            self._options(
                db,
                phone,
                "Escolha uma função autorizada:",
                [(str(s.id), db.get(Role, s.role_id).name) for s in slots],
            )
        elif state == "choose_role":
            slot_id = int(text.removeprefix("R"))
            if slot_id not in session.data.get("slot_ids", []):
                raise RosterError("função não disponível")
            slot = db.get(Slot, slot_id)
            service = db.get(WorshipService, slot.service_id)
            self._state(session, "confirm_signup", slot_id=slot_id)
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Confirmar {service.service_date:%d/%m/%Y} "
                    f"{service.start_time:%H:%M} — {db.get(Role, slot.role_id).name}?",
                    [("SIM", "Confirmar"), ("NAO", "Voltar")],
                ),
            )
        elif state == "confirm_signup":
            if text.upper() != "SIM":
                self._choose_dates(db, session, member)
                return
            slot = db.get(Slot, session.data["slot_id"])
            service = db.get(WorshipService, slot.service_id)
            schedule = schedule_for(db, service.ministry_id, service.service_date)
            if schedule is None or schedule.state != "open":
                raise RosterError("inscrições não estão abertas")
            assignment_id = self.roster.assign(member.id, slot.id, db=db)
            self._state(session, "menu")
            self._text(
                db,
                phone,
                (
                    f"Inscrição #{assignment_id} CONFIRMADA: {service.service_date:%d/%m/%Y} "
                    f"({service.start_time:%H:%M}), {db.get(Role, slot.role_id).name}."
                ),
            )
        elif state == "unavailable":
            start, end = (date.fromisoformat(v) for v in text.split())
            conflicts = self.roster.set_unavailability(member.id, start, end, db=db)
            for assignment_id in conflicts:
                assignment = db.get(Assignment, assignment_id)
                if assignment.status == "confirmed":
                    request_replacement(db, member.id, assignment_id)
            self._state(session, "menu")
            self._text(
                db,
                phone,
                f"Indisponibilidade registrada. {len(conflicts)} "
                "participação(ões) exigem substituição.",
            )
        elif state == "replacement_pick":
            assignment_id = int(text)
            if assignment_id not in session.data.get("assignment_ids", []):
                raise RosterError("participação não disponível")
            self._state(session, "replacement_confirm", assignment_id=assignment_id)
            self._send(
                db,
                phone,
                buttons_payload(
                    "Solicitar substituição? Sua vaga permanece ocupada até ser substituída.",
                    [("SIM", "Confirmar"), ("NAO", "Voltar")],
                ),
            )
        elif state == "replacement_confirm":
            if text.upper() != "SIM":
                self._menu(db, session, None, None, member)
                return
            request_id = request_replacement(db, member.id, session.data["assignment_id"])
            self._state(session, "menu")
            self._text(
                db,
                phone,
                f"Pedido de substituição #{request_id} registrado. "
                "A coordenação foi avisada quando permitido pela janela de envio.",
            )
            slot = db.get(Slot, db.get(Assignment, session.data["assignment_id"]).slot_id)
            service = db.get(WorshipService, slot.service_id)
            for coordinator in db.scalars(
                select(Coordinator)
                .join(CoordinatorMinistry, CoordinatorMinistry.coordinator_id == Coordinator.id)
                .where(
                    Coordinator.church_id == member.church_id,
                    CoordinatorMinistry.ministry_id == service.ministry_id,
                    Coordinator.active.is_(True),
                )
            ).all():
                self._send(
                    db,
                    coordinator.phone_e164,
                    text_payload(
                        f"Substituição pendente #{request_id} "
                        f"em {service.service_date:%d/%m}. Consulte MENU."
                    ),
                    proactive=True,
                )
        else:
            self._menu(db, session, None, None, member)

    def _choose_dates(self, db: Session, session: ConversationSession, member: Volunteer) -> None:
        church = db.get(Church, member.church_id)
        today = datetime.now(ZoneInfo(church.timezone)).date()
        services = db.scalars(
            select(WorshipService)
            .join(Slot, Slot.service_id == WorshipService.id)
            .join(VolunteerRole, VolunteerRole.role_id == Slot.role_id)
            .where(
                VolunteerRole.volunteer_id == member.id,
                WorshipService.service_date >= today,
                WorshipService.service_date <= today + timedelta(days=62),
            )
            .order_by(WorshipService.service_date, WorshipService.start_time)
            .distinct()
        ).all()
        services = [
            s
            for s in services
            if (
                schedule_for(db, s.ministry_id, s.service_date) or MonthlySchedule(state="draft")
            ).state
            == "open"
        ]
        self._state(session, "choose_date", service_ids=[s.id for s in services])
        if not services:
            self._text(db, session.phone_e164, "Não há inscrições abertas para suas funções.")
        else:
            self._options(
                db,
                session.phone_e164,
                "Escolha um culto disponível:",
                [(str(s.id), f"{s.service_date:%d/%m} {s.start_time:%H:%M}") for s in services],
            )

    def _eligible_slots(self, db: Session, volunteer_id: int, service_id: int) -> list[Slot]:
        return db.scalars(
            select(Slot)
            .join(VolunteerRole, VolunteerRole.role_id == Slot.role_id)
            .where(Slot.service_id == service_id, VolunteerRole.volunteer_id == volunteer_id)
        ).all()

    def _future_assignments(self, db: Session, volunteer_id: int) -> list[Assignment]:
        return db.scalars(
            select(Assignment)
            .join(Slot, Slot.id == Assignment.slot_id)
            .join(WorshipService, WorshipService.id == Slot.service_id)
            .where(
                Assignment.volunteer_id == volunteer_id,
                Assignment.status == "confirmed",
                WorshipService.service_date >= date.today(),
            )
            .order_by(WorshipService.service_date)
        ).all()

    def _assignment_lines(self, db: Session, assignments: list[Assignment]) -> str:
        if not assignments:
            return "Nenhuma participação futura confirmada."
        lines = []
        for assignment in assignments:
            slot = db.get(Slot, assignment.slot_id)
            service = db.get(WorshipService, slot.service_id)
            lines.append(
                f"#{assignment.id} — {service.service_date:%d/%m/%Y} "
                f"{service.start_time:%H:%M} — {db.get(Role, slot.role_id).name} "
                f"({assignment.status})"
            )
        return "\n".join(lines)

    def _own_assignments(self, db: Session, member: Volunteer) -> str:
        assignments = db.scalars(
            select(Assignment)
            .join(Slot, Slot.id == Assignment.slot_id)
            .join(WorshipService, WorshipService.id == Slot.service_id)
            .where(
                Assignment.volunteer_id == member.id, WorshipService.service_date >= date.today()
            )
            .order_by(WorshipService.service_date)
        ).all()
        return self._assignment_lines(db, assignments)
