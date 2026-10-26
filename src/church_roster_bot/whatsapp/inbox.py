"""Atomic durable ingestion; only a proven duplicate may be acknowledged twice."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from church_roster_bot.whatsapp.models import InboundEvent
from church_roster_bot.whatsapp.payloads import InboundDraft


def store_events(session_factory, drafts: list[InboundDraft]) -> int:
    accepted = 0
    with session_factory.begin() as db:
        # sqlite3 legacy transaction mode otherwise releases the first SAVEPOINT
        # outside BEGIN, committing it even if a later event rolls back the batch.
        if db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        for draft in drafts:
            try:
                with db.begin_nested():
                    db.add(
                        InboundEvent(
                            event_id=draft.event_id, kind=draft.kind, payload=draft.payload
                        )
                    )
                    db.flush()
                accepted += 1
            except IntegrityError:
                existing = db.scalar(
                    select(InboundEvent.id).where(InboundEvent.event_id == draft.event_id)
                )
                if existing is None:
                    raise
    return accepted
