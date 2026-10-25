"""Coordinator-facing presentation of advisory distribution results."""

from sqlalchemy.orm import Session

from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Role
from church_roster_bot.scheduling.distribution import DistributionProposal
from church_roster_bot.services.models import Slot, WorshipService


def format_proposal(db: Session, proposal: DistributionProposal) -> str:
    lines = [
        f"PRÉVIA DE DISTRIBUIÇÃO — {proposal.month:%m/%Y}",
        f"Política: {proposal.policy_version}. Nenhuma inscrição ou convite foi criado.",
    ]
    for suggestion in proposal.suggestions:
        slot = db.get(Slot, suggestion.intent.slot_id)
        service = db.get(WorshipService, slot.service_id)
        member = db.get(Volunteer, suggestion.intent.volunteer_id)
        role = db.get(Role, slot.role_id)
        lines.append(
            f"Vaga #{slot.id}: {service.service_date:%d/%m/%Y} {service.start_time:%H:%M} "
            f"— {role.name}: #{member.id} {member.name}.\n"
            f"Motivo: {suggestion.score.explanation}."
        )
    for vacancy in proposal.unfilled:
        lines.append(
            f"Vaga #{vacancy.slot_id}: {vacancy.positions} posição(ões) sem candidato elegível. "
            "A coordenação precisa revisar a cobertura."
        )
    if not proposal.suggestions and not proposal.unfilled:
        lines.append("Nenhuma vaga futura em aberto neste mês.")
    lines.append(
        "Esta prévia não reserva vagas. Aprovação persistente e convites da distribuição "
        "serão disponibilizados em um próximo incremento."
    )
    return "\n".join(lines)
