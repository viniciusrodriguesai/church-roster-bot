"""Create clearly fictional next-month services for a local conversation demo."""

from datetime import date, time, timedelta

from sqlalchemy import select

from church_roster_bot.core import models  # noqa: F401
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.scheduling.lifecycle import schedule_for, transition_schedule
from church_roster_bot.services.models import WorshipService


def main() -> None:
    with SessionLocal() as db:
        church = db.scalar(select(Church).where(Church.name == "Igreja Exemplo (dados fictícios)"))
        if church is None:
            raise SystemExit("Execute seed_demo antes deste exemplo.")
        ministry = db.scalar(
            select(Ministry).where(Ministry.church_id == church.id, Ministry.name == "Diaconato")
        )
        roles = {
            role.name: role.id
            for role in db.scalars(select(Role).where(Role.ministry_id == ministry.id))
        }
        ministry_id = ministry.id
    today = date.today()
    first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    end = (first + timedelta(days=32)).replace(day=1)
    roster = RosterEngine(SessionLocal)
    cursor = first
    created = 0
    while cursor < end:
        if cursor.weekday() in {2, 5, 6}:
            with SessionLocal() as db:
                exists = db.scalar(
                    select(WorshipService.id).where(
                        WorshipService.ministry_id == ministry_id,
                        WorshipService.service_date == cursor,
                        WorshipService.start_time == time(19, 0),
                    )
                )
            if not exists:
                roster.create_service(
                    ministry_id,
                    cursor,
                    time(19, 0),
                    {
                        roles["Abrir e fechar"]: 1,
                        roles["Recolher ofertas"]: 1,
                    },
                )
                created += 1
        cursor += timedelta(days=1)
    with SessionLocal.begin() as db:
        schedule = schedule_for(db, ministry_id, first, create=True)
        if schedule.state == "draft":
            transition_schedule(schedule, "open")
    print(
        f"Criados {created} cultos fictícios às 19h em {first:%m/%Y}; "
        "inscrições abertas somente no exemplo."
    )


if __name__ == "__main__":
    main()
