"""Coordinator conversation flows separated from volunteer routing."""

from __future__ import annotations

from datetime import date, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from church_roster_bot.administration.proposals import format_proposal
from church_roster_bot.administration.reports import format_audit, format_monthly_report
from church_roster_bot.administration.service import (
    add_coordinator,
    add_ministry,
    add_role,
    edit_coordinator,
    edit_service,
    edit_volunteer,
    rename_ministry,
    set_church_timezone,
)
from church_roster_bot.audit.models import ScheduleVersion
from church_roster_bot.conversations.dates import parse_month
from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.members.models import Coordinator, Volunteer, VolunteerRole
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.ministries.models import Role
from church_roster_bot.notifications.invitations import DELIVERY_LABELS, enqueue_invitation
from church_roster_bot.scheduling.automation import configure_opening
from church_roster_bot.scheduling.distribution import DistributionPlanner
from church_roster_bot.scheduling.emergencies import configure_emergencies
from church_roster_bot.scheduling.engine import OCCUPYING
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.lifecycle import (
    SCHEDULE_TRANSITIONS,
    audit,
    format_schedule,
    manual_replace,
    publish_schedule,
    schedule_for,
    set_slot_capacity,
    transition_schedule,
)
from church_roster_bot.scheduling.models import Assignment, ReplacementRequest, ThirdDayRequest
from church_roster_bot.scheduling.offers import approve_proposal
from church_roster_bot.scheduling.proposals import (
    cancel_proposal,
    create_proposal,
    proposal_snapshot,
    require_proposal,
)
from church_roster_bot.scheduling.reminders import configure_reminders
from church_roster_bot.scheduling.reports import audit_history, monthly_report
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import buttons_payload, normalize_phone, text_payload


class AdminConversationMixin:
    def _coordinator(
        self,
        db: Session,
        session: ConversationSession,
        text: str,
        ministry,
        actor_type: str,
        actor_id: int,
        coordinator: Coordinator | None,
    ) -> None:
        phone, state, data = session.phone_e164, session.state, session.data
        upper = text.upper()
        if state == "menu":
            if upper == "1":
                self._state(session, "admin_members")
                self._text(db, phone, "Diáconos: NOVO, LISTAR ou EDITAR <id>.")
            elif upper == "2":
                self._state(session, "admin_service_date")
                self._text(db, phone, "Data do novo culto: AAAA-MM-DD, ou EDITAR <culto_id>.")
            elif upper == "3":
                self._state(session, "admin_slot_pick")
                self._text(db, phone, "ID da vaga para alterar capacidade:")
            elif upper == "4":
                self._state(session, "admin_schedule_action")
                self._text(
                    db, phone, "Informe ABRIR, REVISAR ou FECHAR e o mês. Exemplo: ABRIR 2026-11"
                )
            elif upper == "5":
                self._state(session, "admin_view_month")
                self._text(db, phone, "Mês da escala: AAAA-MM")
            elif upper == "6":
                self._state(session, "admin_vacancies_month")
                self._text(db, phone, "Mês das vagas: AAAA-MM")
            elif upper == "7":
                self._state(session, "admin_exceptions")
                self._text(
                    db,
                    phone,
                    "EXCECAO <voluntário_id> <vaga_id>, "
                    "SUBSTITUIR <pedido_id> <voluntário_id> ou LISTAR.",
                )
            elif upper == "8":
                self._state(session, "admin_publish_month")
                self._text(db, phone, "Mês para publicar: AAAA-MM")
            elif upper == "9":
                self._text(db, phone, self._members(db, ministry.id, participants=True))
            elif upper.startswith("ESCALA "):
                self._text(
                    db, phone, format_schedule(db, ministry.id, parse_month(text.split(" ", 1)[1]))
                )
            elif upper.startswith("RELATORIO "):
                report = monthly_report(
                    db, ministry.id, parse_month(text.split(" ", 1)[1]), actor_type, actor_id
                )
                self._text(db, phone, format_monthly_report(report))
            elif upper == "AUDITORIA" or upper.startswith("AUDITORIA "):
                parts = text.split()
                if len(parts) > 2:
                    raise RosterError("use AUDITORIA ou AUDITORIA <id anterior>")
                page = audit_history(
                    db,
                    ministry.id,
                    actor_type,
                    actor_id,
                    before_id=int(parts[1]) if len(parts) == 2 else None,
                )
                self._text(db, phone, format_audit(page))
            elif upper.startswith("PROPOR "):
                proposal = DistributionPlanner(self.roster).propose(
                    db,
                    ministry.id,
                    parse_month(text.split(" ", 1)[1]),
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                self._text(db, phone, format_proposal(db, proposal))
            elif upper.startswith("PLANEJAR "):
                proposal_id = create_proposal(
                    db,
                    self.roster,
                    ministry.id,
                    parse_month(text.split(" ", 1)[1]),
                    actor_type=actor_type,
                    actor_id=actor_id,
                )
                self._text(
                    db,
                    phone,
                    f"Proposta #{proposal_id} salva como rascunho. "
                    f"Envie VERPROPOSTA {proposal_id} para revisar ou DESCARTAR {proposal_id}. "
                    f"Para convidar, envie APROVAR {proposal_id}. "
                    "Nenhuma inscrição ou convite foi criado.",
                )
            elif upper.startswith("EMERGENCIAS "):
                parts = upper.split()
                if parts == ["EMERGENCIAS", "DESATIVAR"]:
                    self._state(session, "admin_emergencies_confirm", emergencies_enabled=False)
                    prompt = "Desativar novas ofertas automáticas de emergência deste ministério?"
                elif len(parts) == 3:
                    hours, minutes = int(parts[1]), int(parts[2])
                    if not 1 <= hours <= 72 or not 1 <= minutes <= 120:
                        raise RosterError("use EMERGENCIAS <horas 1..72> <minutos 1..120>")
                    self._state(
                        session,
                        "admin_emergencies_confirm",
                        emergencies_enabled=True,
                        emergency_hours=hours,
                        emergency_minutes=minutes,
                    )
                    prompt = (
                        f"Buscar cobertura nas {hours} horas antes do culto, com prazo de "
                        f"{minutes} minutos por convite? Toda confirmação exige aceite."
                    )
                else:
                    raise RosterError("use EMERGENCIAS <horas> <minutos> ou EMERGENCIAS DESATIVAR")
                self._send(
                    db, phone, buttons_payload(prompt, [("SIM", "Confirmar"), ("NAO", "Cancelar")])
                )
            elif upper.startswith("LEMBRETES "):
                parts = upper.split()
                if parts == ["LEMBRETES", "DESATIVAR"]:
                    self._state(session, "admin_reminders_confirm", reminders_enabled=False)
                    prompt = "Desativar os lembretes automáticos deste ministério?"
                elif len(parts) == 3:
                    days, hour = int(parts[1]), time.fromisoformat(parts[2])
                    if not 0 <= days <= 30 or hour.tzinfo or hour.second or hour.microsecond:
                        raise RosterError("use LEMBRETES <dias 0..30> <HH:MM>")
                    self._state(
                        session,
                        "admin_reminders_confirm",
                        reminders_enabled=True,
                        reminder_days=days,
                        reminder_hour=hour.isoformat(),
                    )
                    prompt = (
                        f"Lembrar {days} dia(s) antes, às {hour:%H:%M} no fuso da igreja? "
                        "Somente participações confirmadas, com consentimento e envio permitido."
                    )
                else:
                    raise RosterError("use LEMBRETES <dias 0..30> <HH:MM> ou LEMBRETES DESATIVAR")
                self._send(
                    db, phone, buttons_payload(prompt, [("SIM", "Confirmar"), ("NAO", "Cancelar")])
                )
            elif upper.startswith("ABERTURA "):
                parts = upper.split()
                if parts == ["ABERTURA", "DESATIVAR"]:
                    self._state(session, "admin_opening_confirm", opening_enabled=False)
                    prompt = "Desativar a abertura mensal automática deste ministério?"
                elif len(parts) == 3:
                    day, hour = int(parts[1]), time.fromisoformat(parts[2])
                    if (
                        not 1 <= day <= 28
                        or hour.tzinfo is not None
                        or hour.second
                        or hour.microsecond
                    ):
                        raise RosterError("use ABERTURA <dia 1..28> <HH:MM>")
                    self._state(
                        session,
                        "admin_opening_confirm",
                        opening_enabled=True,
                        opening_day=day,
                        opening_hour=hour.isoformat(),
                    )
                    prompt = (
                        f"Abrir as escalas no dia {day} do mês anterior às {hour:%H:%M}, "
                        "no fuso da igreja? Apenas meses com cultos cadastrados. "
                        "Aberturas vencidas do mês atual podem ser recuperadas."
                    )
                else:
                    raise RosterError("use ABERTURA <dia 1..28> <HH:MM> ou ABERTURA DESATIVAR")
                self._send(
                    db, phone, buttons_payload(prompt, [("SIM", "Confirmar"), ("NAO", "Cancelar")])
                )
            elif upper.startswith(("VERPROPOSTA ", "DESCARTAR ", "APROVAR ")):
                proposal_id = int(text.split()[1])
                proposal = require_proposal(db, proposal_id, ministry.id, actor_type, actor_id)
                if upper.startswith("VERPROPOSTA "):
                    self._text(
                        db,
                        phone,
                        f"Proposta #{proposal.id}: {proposal.status}.\n"
                        + format_proposal(db, proposal_snapshot(db, proposal)),
                    )
                elif upper.startswith("DESCARTAR "):
                    self._state(session, "admin_cancel_proposal_confirm", proposal_id=proposal_id)
                    self._send(
                        db,
                        phone,
                        buttons_payload(
                            f"Descartar proposta #{proposal_id}?",
                            [("SIM", "Descartar"), ("NAO", "Manter")],
                        ),
                    )
                else:
                    self._state(session, "admin_approve_proposal_confirm", proposal_id=proposal_id)
                    self._send(
                        db,
                        phone,
                        buttons_payload(
                            f"Aprovar proposta #{proposal_id} e enviar convites? "
                            "As inscrições dependem do aceite de cada voluntário.",
                            [("SIM", "Aprovar"), ("NAO", "Cancelar")],
                        ),
                    )
            else:
                self._menu(db, session, None, coordinator, None)
            return
        if state == "admin_emergencies_confirm":
            if upper == "SIM":
                options = (
                    {"hours": data["emergency_hours"], "minutes": data["emergency_minutes"]}
                    if data["emergencies_enabled"]
                    else {}
                )
                configure_emergencies(
                    db,
                    ministry.id,
                    actor_type,
                    actor_id,
                    enabled=data["emergencies_enabled"],
                    **options,
                )
                self._text(db, phone, "Emergências automáticas configuradas.")
            else:
                self._text(db, phone, "Configuração descartada.")
            self._state(session, "menu")
        elif state == "admin_reminders_confirm":
            if upper == "SIM":
                options = (
                    {
                        "days": data["reminder_days"],
                        "hour": time.fromisoformat(data["reminder_hour"]),
                    }
                    if data["reminders_enabled"]
                    else {}
                )
                configure_reminders(
                    db,
                    ministry.id,
                    actor_type,
                    actor_id,
                    enabled=data["reminders_enabled"],
                    **options,
                )
                self._text(db, phone, "Lembretes automáticos configurados.")
            else:
                self._text(db, phone, "Configuração descartada.")
            self._state(session, "menu")
        elif state == "admin_opening_confirm":
            if upper == "SIM":
                options = (
                    {"day": data["opening_day"], "hour": time.fromisoformat(data["opening_hour"])}
                    if data["opening_enabled"]
                    else {}
                )
                configure_opening(
                    db,
                    ministry.id,
                    actor_type,
                    actor_id,
                    enabled=data["opening_enabled"],
                    **options,
                )
                self._text(db, phone, "Abertura mensal automática configurada.")
            else:
                self._text(db, phone, "Configuração descartada.")
            self._state(session, "menu")
        elif state == "admin_approve_proposal_confirm":
            if upper == "SIM":
                invitations = approve_proposal(
                    db,
                    self.roster,
                    data["proposal_id"],
                    ministry.id,
                    actor_type,
                    actor_id,
                    self.config,
                )
                self._text(
                    db,
                    phone,
                    f"Proposta aprovada: {len(invitations)} convite(s) registrado(s). "
                    "Nenhuma participação é confirmada sem aceite.",
                )
            else:
                self._text(db, phone, "Aprovação descartada; rascunho mantido.")
            self._state(session, "menu")
        elif state == "admin_cancel_proposal_confirm":
            if upper == "SIM":
                cancel_proposal(db, data["proposal_id"], ministry.id, actor_type, actor_id)
                self._text(db, phone, "Proposta descartada; histórico preservado.")
            else:
                self._text(db, phone, "Proposta mantida.")
            self._state(session, "menu")
        elif state == "admin_members":
            if upper == "LISTAR":
                self._text(db, phone, self._members(db, ministry.id))
            elif upper == "NOVO":
                self._state(session, "admin_new_name")
                self._text(db, phone, "Nome do novo diácono:")
            elif upper.startswith("EDITAR "):
                volunteer_id = int(text.split()[1])
                member = db.get(Volunteer, volunteer_id)
                if member is None or member.church_id != ministry.church_id:
                    raise RosterError("voluntário não encontrado")
                self._state(session, "admin_edit", volunteer_id=volunteer_id)
                self._text(
                    db,
                    phone,
                    "Informe NOME <texto>, TELEFONE <+E164>, FUNCOES <ids>, ATIVAR ou DESATIVAR.",
                )
            else:
                self._text(db, phone, "Use NOVO, LISTAR ou EDITAR <id>.")
        elif state == "admin_new_name":
            if not text.strip() or len(text) > 160:
                raise RosterError("nome inválido")
            self._state(session, "admin_new_phone", name=text.strip())
            self._text(db, phone, "Telefone internacional, exemplo fictício +15550100001:")
        elif state == "admin_new_phone":
            self._state(session, "admin_new_roles", phone=normalize_phone(text))
            self._text(
                db,
                phone,
                "IDs das funções autorizadas, separados por vírgula:\n"
                + self._role_lines(db, ministry.id),
            )
        elif state == "admin_new_roles":
            role_ids = self._parse_roles(db, ministry.id, text)
            self._state(session, "admin_new_confirm", role_ids=sorted(role_ids))
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Cadastrar {data['name']} ({data['phone']}) com funções {sorted(role_ids)}?",
                    [("SIM", "Salvar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_new_confirm":
            if upper == "SIM":
                volunteer_id = self.roster.register_volunteer(
                    ministry.church_id,
                    VolunteerCreate(
                        name=data["name"], phone_e164=data["phone"], role_ids=set(data["role_ids"])
                    ),
                    db=db,
                )
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "add_volunteer",
                    "volunteer",
                    volunteer_id,
                    ministry_id=ministry.id,
                )
                self._text(
                    db,
                    phone,
                    f"Diácono #{volunteer_id} cadastrado. "
                    "Convites estão desativados até ele enviar INICIAR.",
                )
            else:
                self._text(db, phone, "Cadastro descartado.")
            self._state(session, "admin_members")
        elif state == "admin_edit":
            parts = text.split(" ", 1)
            command = parts[0].upper()
            if command in {"NOME", "TELEFONE", "FUNCOES"} and len(parts) == 2:
                field = {"NOME": "name", "TELEFONE": "phone", "FUNCOES": "roles"}[command]
                value = parts[1].strip()
                if field == "roles":
                    self._parse_roles(db, ministry.id, value)
                elif field == "phone":
                    normalize_phone(value)
            elif command in {"ATIVAR", "DESATIVAR"}:
                field, value = "active", "true" if command == "ATIVAR" else "false"
            else:
                raise RosterError("comando de edição inválido")
            self._state(session, "admin_edit_confirm", edit_field=field, edit_value=value)
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Confirmar alteração de {field} para o voluntário #{data['volunteer_id']}?",
                    [("SIM", "Salvar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_edit_confirm":
            if upper == "SIM":
                edit_volunteer(
                    db,
                    data["volunteer_id"],
                    data["edit_field"],
                    data["edit_value"],
                    ministry.id,
                    actor_type,
                    actor_id,
                )
                self._text(db, phone, "Cadastro atualizado.")
            else:
                self._text(db, phone, "Alteração descartada.")
            self._state(session, "admin_members")
        elif state == "admin_service_date":
            if upper.startswith("EDITAR "):
                service_id = int(text.split()[1])
                service = db.get(WorshipService, service_id)
                if service is None or service.ministry_id != ministry.id:
                    raise RosterError("culto fora do ministério autorizado")
                self._state(session, "admin_service_edit", service_id=service_id)
                self._text(db, phone, "Informe DATA AAAA-MM-DD ou HORA HH:MM.")
                return
            service_date = date.fromisoformat(text)
            self._state(session, "admin_service_time", service_date=service_date.isoformat())
            self._text(db, phone, "Horário do culto: HH:MM")
        elif state == "admin_service_edit":
            command, value = text.split(" ", 1)
            field = {"DATA": "date", "HORA": "time"}.get(command.upper())
            if field is None:
                raise RosterError("use DATA ou HORA")
            self._state(session, "admin_service_edit_confirm", field=field, value=value.strip())
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Alterar {field} do culto #{data['service_id']} para {value.strip()}?",
                    [("SIM", "Salvar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_service_edit_confirm":
            if upper == "SIM":
                edit_service(
                    db,
                    ministry.id,
                    data["service_id"],
                    data["field"],
                    data["value"],
                    actor_type,
                    actor_id,
                )
                self._text(db, phone, "Culto atualizado.")
            else:
                self._text(db, phone, "Alteração descartada.")
            self._state(session, "menu")
        elif state == "admin_service_time":
            start_time = time.fromisoformat(text)
            self._state(session, "admin_service_caps", start_time=start_time.strftime("%H:%M"))
            self._text(
                db, phone, "Funções e vagas, exemplo 1:2,3:1:\n" + self._role_lines(db, ministry.id)
            )
        elif state == "admin_service_caps":
            capacities = dict(
                (int(pair.split(":")[0]), int(pair.split(":")[1])) for pair in text.split(",")
            )
            if not capacities or any(v <= 0 for v in capacities.values()):
                raise RosterError("vagas devem ser positivas")
            self._parse_roles(db, ministry.id, ",".join(map(str, capacities)))
            self._state(session, "admin_service_confirm", capacities=capacities)
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Criar culto {data['service_date']} às {data['start_time']} "
                    f"com vagas {capacities}?",
                    [("SIM", "Criar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_service_confirm":
            if upper == "SIM":
                service_date = date.fromisoformat(data["service_date"])
                service_id = self.roster.create_service(
                    ministry.id,
                    service_date,
                    time.fromisoformat(data["start_time"]),
                    {int(k): v for k, v in data["capacities"].items()},
                    db=db,
                )
                schedule_for(db, ministry.id, service_date, create=True)
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "create_service",
                    "service",
                    service_id,
                )
                self._refresh_published(db, phone, ministry.id, service_date)
                self._text(db, phone, f"Culto #{service_id} criado.")
            else:
                self._text(db, phone, "Operação descartada.")
            self._state(session, "menu")
        elif state == "admin_slot_pick":
            slot_id = int(text)
            slot = self._scoped_slot(db, ministry.id, slot_id)
            self._state(session, "admin_slot_capacity", slot_id=slot.id)
            self._text(db, phone, f"Capacidade atual: {slot.capacity}. Nova capacidade:")
        elif state == "admin_slot_capacity":
            self._state(session, "admin_slot_confirm", capacity=int(text))
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Alterar a capacidade da vaga #{data['slot_id']} para {text}?",
                    [("SIM", "Salvar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_slot_confirm":
            if upper == "SIM":
                self._scoped_slot(db, ministry.id, data["slot_id"])
                set_slot_capacity(db, data["slot_id"], data["capacity"])
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "set_capacity",
                    "slot",
                    data["slot_id"],
                    {"capacity": data["capacity"]},
                )
                changed_slot = db.get(Slot, data["slot_id"])
                changed_service = db.get(WorshipService, changed_slot.service_id)
                self._refresh_published(db, phone, ministry.id, changed_service.service_date)
                self._text(db, phone, "Capacidade atualizada.")
            else:
                self._text(db, phone, "Operação descartada.")
            self._state(session, "menu")
        elif state == "admin_schedule_action":
            action, month = upper.split()
            target = {"ABRIR": "open", "REVISAR": "under_review", "FECHAR": "closed"}.get(action)
            if target is None:
                raise RosterError("ação inválida")
            day = parse_month(month)
            schedule = schedule_for(db, ministry.id, day, create=True)
            if target not in SCHEDULE_TRANSITIONS[schedule.state]:
                raise RosterError("transição de escala inválida")
            self._state(session, "admin_schedule_confirm", month=month, target=target)
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Mudar escala {month} para {target.upper()}?",
                    [("SIM", "Confirmar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_schedule_confirm":
            if upper == "SIM":
                schedule = schedule_for(db, ministry.id, parse_month(data["month"]), create=True)
                transition_schedule(schedule, data["target"])
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "schedule_state",
                    "schedule",
                    schedule.id,
                    {"state": data["target"]},
                )
                self._text(db, phone, f"Escala agora está {schedule.state.upper()}.")
            else:
                self._text(db, phone, "Operação descartada.")
            self._state(session, "menu")
        elif state == "admin_view_month":
            self._text(db, phone, format_schedule(db, ministry.id, parse_month(text)))
        elif state == "admin_vacancies_month":
            self._text(db, phone, self._vacancies(db, ministry.id, parse_month(text)))
        elif state == "admin_publish_month":
            day = parse_month(text)
            self._state(session, "admin_publish_confirm", month=text)
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Publicar versão da escala {text}?", [("SIM", "Publicar"), ("NAO", "Cancelar")]
                ),
            )
        elif state == "admin_publish_confirm":
            if upper == "SIM":
                version = publish_schedule(db, ministry.id, parse_month(data["month"]))
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "publish_schedule",
                    "schedule_version",
                    version.id,
                )
                self._text(
                    db,
                    phone,
                    f"Versão {version.version} pronta para encaminhar:\n{version.content}",
                )
                if version.update_message:
                    self._text(db, phone, version.update_message)
            else:
                self._text(db, phone, "Publicação descartada.")
            self._state(session, "menu")
        elif state == "admin_exceptions":
            if upper == "LISTAR":
                self._text(db, phone, self._pending_requests(db, ministry.id))
            elif upper.startswith("EXCECAO "):
                _, member_id, slot_id = upper.split()
                self._scoped_slot(db, ministry.id, int(slot_id))
                self._state(
                    session,
                    "admin_exception_reason",
                    member_id=int(member_id),
                    slot_id=int(slot_id),
                )
                self._text(db, phone, "Justificativa obrigatória para o terceiro dia:")
            elif upper.startswith("SUBSTITUIR "):
                _, request_id, member_id = upper.split()
                self._scoped_request(db, ministry.id, int(request_id))
                self._state(
                    session,
                    "admin_replace_confirm",
                    request_id=int(request_id),
                    member_id=int(member_id),
                )
                self._send(
                    db,
                    phone,
                    buttons_payload(
                        f"Substituir no pedido #{request_id} pelo voluntário #{member_id}?",
                        [("SIM", "Confirmar"), ("NAO", "Cancelar")],
                    ),
                )
            else:
                raise RosterError("use LISTAR, EXCECAO ou SUBSTITUIR")
        elif state == "admin_exception_reason":
            if not text.strip():
                raise RosterError("justificativa obrigatória")
            self._state(session, "admin_exception_confirm", justification=text.strip())
            self._send(
                db,
                phone,
                buttons_payload(
                    f"Convidar voluntário #{data['member_id']} para terceira data?",
                    [("SIM", "Convidar"), ("NAO", "Cancelar")],
                ),
            )
        elif state == "admin_exception_confirm":
            if upper == "SIM":
                if not coordinator:
                    raise RosterError("use um coordenador cadastrado para solicitar a exceção")
                request_id = self.roster.request_third_day(
                    coordinator.id,
                    data["member_id"],
                    data["slot_id"],
                    data["justification"],
                    db=db,
                )
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "third_day_request",
                    "third_day_request",
                    request_id,
                )
                already_queued = db.scalar(
                    select(OutboundMessage.id).where(OutboundMessage.invitation_id == request_id)
                )
                outgoing = enqueue_invitation(db, db.get(ThirdDayRequest, request_id), self.config)
                if already_queued:
                    self._text(
                        db,
                        phone,
                        f"Convite #{request_id} já registrado: "
                        f"{DELIVERY_LABELS.get(outgoing.status, outgoing.status)}. "
                        "Aceite do voluntário ainda é obrigatório.",
                    )
            else:
                self._text(db, phone, "Convite descartado.")
            self._state(session, "menu")
        elif state == "admin_replace_confirm":
            if upper == "SIM":
                self._scoped_request(db, ministry.id, data["request_id"])
                new_id = manual_replace(db, self.roster, data["request_id"], data["member_id"])
                audit(
                    db,
                    ministry.church_id,
                    actor_type,
                    actor_id,
                    "manual_replace",
                    "assignment",
                    new_id,
                )
                self._text(db, phone, f"Substituição confirmada. Nova participação #{new_id}.")
                request = db.get(ReplacementRequest, data["request_id"])
                original = db.get(Assignment, request.assignment_id)
                slot = db.get(Slot, original.slot_id)
                service = db.get(WorshipService, slot.service_id)
                for volunteer_id in (original.volunteer_id, data["member_id"]):
                    member = db.get(Volunteer, volunteer_id)
                    if member.invite_opt_in:
                        self._send(
                            db,
                            member.phone_e164,
                            text_payload(
                                f"Substituição confirmada para {service.service_date:%d/%m/%Y} "
                                f"às {service.start_time:%H:%M}. Consulte sua escala pelo MENU."
                            ),
                            proactive=True,
                        )
                schedule = schedule_for(db, ministry.id, service.service_date)
                if schedule and schedule.state == "published":
                    version = db.scalar(
                        select(ScheduleVersion)
                        .where(ScheduleVersion.schedule_id == schedule.id)
                        .order_by(ScheduleVersion.version.desc())
                    )
                    if version and version.update_message:
                        self._text(db, phone, version.update_message)
            else:
                self._text(db, phone, "Substituição descartada.")
            self._state(session, "menu")
        else:
            self._menu(db, session, None, coordinator, None)

    def _refresh_published(self, db: Session, phone: str, ministry_id: int, day: date) -> None:
        schedule = schedule_for(db, ministry_id, day)
        if schedule and schedule.state == "published":
            version = publish_schedule(db, ministry_id, day)
            if version.update_message:
                self._text(db, phone, version.update_message)

    def _parse_roles(self, db: Session, ministry_id: int, text: str) -> set[int]:
        role_ids = {int(v.strip()) for v in text.split(",")}
        allowed = set(
            db.scalars(
                select(Role.id).where(Role.ministry_id == ministry_id, Role.active.is_(True))
            ).all()
        )
        if not role_ids or not role_ids <= allowed:
            raise RosterError("funções devem pertencer ao ministério autorizado")
        return role_ids

    def _role_lines(self, db: Session, ministry_id: int) -> str:
        roles = db.scalars(
            select(Role).where(Role.ministry_id == ministry_id, Role.active.is_(True))
        ).all()
        return "\n".join(f"{role.id}: {role.name}" for role in roles)

    def _scoped_slot(self, db: Session, ministry_id: int, slot_id: int) -> Slot:
        slot = db.get(Slot, slot_id)
        if slot is None or db.get(WorshipService, slot.service_id).ministry_id != ministry_id:
            raise RosterError("vaga fora do ministério autorizado")
        return slot

    def _scoped_request(self, db: Session, ministry_id: int, request_id: int) -> ReplacementRequest:
        request = db.get(ReplacementRequest, request_id)
        if request is None:
            raise RosterError("pedido não encontrado")
        self._scoped_slot(db, ministry_id, db.get(Assignment, request.assignment_id).slot_id)
        return request

    def _members(self, db: Session, ministry_id: int, *, participants: bool = False) -> str:
        rows = db.scalars(
            select(Volunteer)
            .join(VolunteerRole, VolunteerRole.volunteer_id == Volunteer.id)
            .join(Role, Role.id == VolunteerRole.role_id)
            .where(Role.ministry_id == ministry_id)
            .distinct()
            .order_by(Volunteer.name)
        ).all()
        if participants:
            return (
                "\n".join(
                    f"#{m.id} {m.name}: "
                    f"{self.roster.monthly_days(m.id, date.today(), db=db)} dia(s) neste mês"
                    for m in rows
                )
                or "Nenhum participante."
            )
        return (
            "\n".join(f"#{m.id} {m.name} ({'ativo' if m.active else 'inativo'})" for m in rows)
            or "Nenhum diácono."
        )

    def _vacancies(self, db: Session, ministry_id: int, day: date) -> str:
        start, end = day, date(day.year + (day.month == 12), day.month % 12 + 1, 1)
        slots = db.scalars(
            select(Slot)
            .join(WorshipService, WorshipService.id == Slot.service_id)
            .where(
                WorshipService.ministry_id == ministry_id,
                WorshipService.service_date >= start,
                WorshipService.service_date < end,
            )
        ).all()
        lines = []
        for slot in slots:
            count = (
                db.scalar(
                    select(func.count(Assignment.id)).where(
                        Assignment.slot_id == slot.id, Assignment.status.in_(OCCUPYING)
                    )
                )
                or 0
            )
            if count < slot.capacity:
                service = db.get(WorshipService, slot.service_id)
                lines.append(
                    f"#{slot.id} {service.service_date:%d/%m} "
                    f"{db.get(Role, slot.role_id).name}: {slot.capacity - count} vaga(s)"
                )
        return "\n".join(lines) or "Sem vagas pendentes."

    def _pending_requests(self, db: Session, ministry_id: int) -> str:
        requests = db.scalars(
            select(ReplacementRequest).where(ReplacementRequest.status == "pending")
        ).all()
        lines = []
        for request in requests:
            slot = db.get(Slot, db.get(Assignment, request.assignment_id).slot_id)
            if db.get(WorshipService, slot.service_id).ministry_id == ministry_id:
                lines.append(f"Substituição #{request.id} — participação #{request.assignment_id}")
        invitations = db.scalars(
            select(ThirdDayRequest)
            .join(Slot, Slot.id == ThirdDayRequest.slot_id)
            .join(WorshipService, WorshipService.id == Slot.service_id)
            .where(WorshipService.ministry_id == ministry_id, ThirdDayRequest.status == "pending")
        ).all()
        for invitation in invitations:
            outgoing = db.scalar(
                select(OutboundMessage).where(OutboundMessage.invitation_id == invitation.id)
            )
            delivery = (
                DELIVERY_LABELS.get(outgoing.status, outgoing.status) if outgoing else "sem envio"
            )
            lines.append(f"Convite #{invitation.id}: {delivery}. Aceite ainda obrigatório.")
        return "\n".join(lines) or "Nenhum pedido pendente."

    def _general(self, db: Session, session: ConversationSession, text: str, admin) -> None:
        if admin is None:
            raise RosterError("acesso negado")
        phone = session.phone_e164
        if session.state == "general_menu":
            if text.upper().startswith("MINISTERIO "):
                self._state(
                    session, "general_confirm", action="ministry", name=text.split(" ", 1)[1]
                )
            elif text.upper().startswith("COORDENADOR "):
                detail = text.split(" ", 1)[1]
                header, ids = detail.split("|", 1)
                coordinator_phone, name = header.strip().split(" ", 1)
                self._state(
                    session,
                    "general_confirm",
                    action="coordinator",
                    name=name.strip(),
                    coordinator_phone=normalize_phone(coordinator_phone),
                    ministry_ids=sorted(int(v) for v in ids.split(",")),
                )
            elif text.upper().startswith("FUNCAO "):
                _, ministry_id, name = text.split(" ", 2)
                self._state(
                    session,
                    "general_confirm",
                    action="role",
                    target_id=int(ministry_id),
                    name=name.strip(),
                )
            elif text.upper().startswith("FUSO "):
                self._state(
                    session,
                    "general_confirm",
                    action="timezone",
                    timezone=text.split(" ", 1)[1].strip(),
                )
            elif text.upper().startswith("RENOMEAR_MINISTERIO "):
                _, ministry_id, name = text.split(" ", 2)
                self._state(
                    session,
                    "general_confirm",
                    action="rename_ministry",
                    target_id=int(ministry_id),
                    name=name.strip(),
                )
            elif text.upper().startswith("EDITAR_COORD "):
                _, coordinator_id, command = text.split(" ", 2)
                field_name, *rest = command.split(" ", 1)
                field = {
                    "NOME": "name",
                    "TELEFONE": "phone",
                    "MINISTERIOS": "ministries",
                    "ATIVAR": "active",
                    "DESATIVAR": "active",
                }.get(field_name.upper())
                if field is None:
                    raise RosterError("campo de coordenador inválido")
                value = (
                    "true"
                    if field_name.upper() == "ATIVAR"
                    else "false"
                    if field_name.upper() == "DESATIVAR"
                    else rest[0]
                )
                self._state(
                    session,
                    "general_confirm",
                    action="edit_coordinator",
                    target_id=int(coordinator_id),
                    field=field,
                    value=value,
                )
            else:
                raise RosterError("comando geral inválido")
            self._send(
                db,
                phone,
                buttons_payload(
                    "Confirmar operação de administrador geral?",
                    [("SIM", "Confirmar"), ("NAO", "Cancelar")],
                ),
            )
        elif session.state == "general_confirm":
            if text.upper() == "SIM":
                data = session.data
                if data["action"] == "ministry":
                    new_id = add_ministry(db, admin, data["name"])
                elif data["action"] == "coordinator":
                    new_id = add_coordinator(
                        db,
                        admin,
                        data["coordinator_phone"],
                        data["name"],
                        set(data["ministry_ids"]),
                    )
                elif data["action"] == "role":
                    new_id = add_role(db, admin, data["target_id"], data["name"])
                elif data["action"] == "timezone":
                    set_church_timezone(db, admin, data["timezone"])
                    new_id = admin.church_id
                elif data["action"] == "rename_ministry":
                    rename_ministry(db, admin, data["target_id"], data["name"])
                    new_id = data["target_id"]
                else:
                    edit_coordinator(db, admin, data["target_id"], data["field"], data["value"])
                    new_id = data["target_id"]
                self._text(db, phone, f"Operação concluída: #{new_id}.")
            else:
                self._text(db, phone, "Operação descartada.")
            self._state(session, "menu")
