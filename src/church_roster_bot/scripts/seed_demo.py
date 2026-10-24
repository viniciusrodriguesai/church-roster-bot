"""Populate an empty database with clearly fictional Diaconato data."""

from sqlalchemy import select

from church_roster_bot.core import models  # noqa: F401
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.members.models import Coordinator, Volunteer, VolunteerRole
from church_roster_bot.ministries.models import Church, Ministry, Role, RoleConflict


def main() -> None:
    with SessionLocal.begin() as db:
        if db.scalar(select(Church.id).where(Church.name == "Igreja Exemplo (dados fictícios)")):
            print("Dados fictícios já existem.")
            return
        church = Church(name="Igreja Exemplo (dados fictícios)")
        db.add(church)
        db.flush()
        ministry = Ministry(church_id=church.id, name="Diaconato")
        db.add(ministry)
        db.flush()
        role_names = ["Abrir a igreja", "Fechar a igreja", "Recolher ofertas", "Abrir e fechar"]
        roles = [Role(ministry_id=ministry.id, name=name) for name in role_names]
        db.add_all(roles)
        db.flush()
        for role in (roles[0], roles[1], roles[3]):
            a, b = sorted((role.id, roles[2].id))
            db.add(RoleConflict(role_a_id=a, role_b_id=b))
        db.add(
            Coordinator(
                church_id=church.id,
                name="Coordenador Exemplo",
                phone_e164="+15550100000",
            )
        )
        demo_permissions = [[0, 1], [0, 1, 2], [2], [2], [3], [0, 1], [2], [0, 1, 2]]
        for index, permission_indices in enumerate(demo_permissions, start=1):
            member = Volunteer(
                church_id=church.id,
                name=f"Voluntário Fictício {index}",
                phone_e164=f"+1555010{index:04d}",
                invite_opt_in=False,
            )
            db.add(member)
            db.flush()
            db.add_all(
                VolunteerRole(volunteer_id=member.id, role_id=roles[rid].id)
                for rid in permission_indices
            )
    print("Diaconato fictício criado com oito voluntários. Nenhum culto foi presumido.")


if __name__ == "__main__":
    main()
