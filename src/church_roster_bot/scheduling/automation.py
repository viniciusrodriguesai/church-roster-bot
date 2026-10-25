"""Opt-in monthly opening policies; safe to re-run after outages and restarts."""

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import authorize_actor
from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import local_to_utc, utc_now
from church_roster_bot.ministries.models import Church, Ministry
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.lifecycle import audit, schedule_for, transition_schedule
from church_roster_bot.scheduling.models import AutomationPolicy, MonthlySchedule
from church_roster_bot.services.models import WorshipService

logger = logging.getLogger(__name__)


def configure_opening(
    db: Session,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
    *,
    enabled: bool,
    day: int | None = None,
    hour: time | None = None,
) -> None:
    if (day is not None and not 1 <= day <= 28) or (
        hour is not None and (hour.tzinfo is not None or hour.second or hour.microsecond)
    ):
        raise RosterError("use opening day 1..28 and a local HH:MM time")
    with db.begin_nested():
        ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
        policy = db.get(AutomationPolicy, ministry_id, with_for_update=True)
        if policy is None:
            try:
                with db.begin_nested():
                    policy = AutomationPolicy(
                        ministry_id=ministry_id,
                        opening_enabled=False,
                        opening_day=25,
                        opening_time=time(9),
                        updated_at=utc_now(),
                    )
                    db.add(policy)
                    db.flush()
            except IntegrityError:
                policy = db.get(AutomationPolicy, ministry_id, with_for_update=True)
                if policy is None:
                    raise
        policy.opening_enabled = enabled
        if day is not None:
            policy.opening_day = day
        if hour is not None:
            policy.opening_time = hour
        policy.updated_at = utc_now()
        audit(
            db,
            ministry.church_id,
            actor_type,
            actor_id,
            "opening_policy_changed",
            "ministry",
            ministry_id,
            {
                "enabled": enabled,
                "day": policy.opening_day,
                "hour": policy.opening_time.isoformat(),
            },
        )


def _locked_schedule(db: Session, ministry_id: int, month: date) -> MonthlySchedule:
    # Manual opening can create the same month concurrently. The unique index
    # arbitrates creation; the losing transaction reads the committed row.
    try:
        with db.begin_nested():
            schedule_for(db, ministry_id, month, create=True)
    except IntegrityError:
        if schedule_for(db, ministry_id, month) is None:
            raise
    return db.scalar(
        select(MonthlySchedule)
        .where(
            MonthlySchedule.ministry_id == ministry_id,
            MonthlySchedule.year == month.year,
            MonthlySchedule.month == month.month,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def open_monthly_schedules(db: Session, config: Settings, *, now: datetime | None = None) -> int:
    instant = utc_now(now)
    count = 0
    for policy in db.scalars(
        select(AutomationPolicy)
        .where(AutomationPolicy.opening_enabled.is_(True))
        .order_by(AutomationPolicy.ministry_id)
        .with_for_update(skip_locked=True)
    ).all():
        ministry = db.get(Ministry, policy.ministry_id)
        church = db.get(Church, ministry.church_id)
        try:
            today = instant.astimezone(ZoneInfo(church.timezone)).date()
        except ZoneInfoNotFoundError:
            notify_ministry(
                db,
                ministry.id,
                f"opening:{ministry.id}:invalid_timezone",
                "A abertura automática precisa de revisão: fuso horário inválido.",
                config,
            )
            continue
        services = db.scalars(
            select(WorshipService)
            .where(WorshipService.ministry_id == ministry.id, WorshipService.service_date >= today)
            .order_by(WorshipService.service_date, WorshipService.id)
        ).all()
        months = sorted({service.service_date.replace(day=1) for service in services})
        for month in months:
            previous = date(
                month.year - (month.month == 1), month.month - 1 or 12, policy.opening_day
            )
            try:
                due = local_to_utc(previous, policy.opening_time, church.timezone)
            except ValueError:
                notify_ministry(
                    db,
                    ministry.id,
                    f"opening:{ministry.id}:{month}:invalid_time",
                    "A abertura automática precisa de revisão: horário local inexistente.",
                    config,
                )
                continue
            if due > instant:
                continue
            # Ignore a month whose remaining services have all already begun.
            future = False
            for service in services:
                if service.service_date.replace(day=1) == month:
                    try:
                        future |= (
                            local_to_utc(service.service_date, service.start_time, church.timezone)
                            > instant
                        )
                    except ValueError:
                        continue
            if not future:
                continue
            schedule = _locked_schedule(db, ministry.id, month)
            if schedule.state != "draft":
                continue
            transition_schedule(schedule, "open")
            audit(
                db,
                ministry.church_id,
                "system",
                0,
                "schedule_auto_opened",
                "schedule",
                schedule.id,
                {"month": month.isoformat()},
            )
            notify_ministry(
                db,
                ministry.id,
                f"opening:{ministry.id}:{month}",
                f"Inscrições de {month:%m/%Y} abertas automaticamente para {ministry.name}.",
                config,
            )
            logger.info(
                "schedule_auto_opened",
                extra={"ministry_id": ministry.id, "month": month.isoformat()},
            )
            count += 1
    return count
