"""Exclusively fictitious historical data covering every persistent table."""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import create_engine, text

from alembic import command
from church_roster_bot.core.database import Base
from church_roster_bot.database.schema import migration_config


def historical_sqlite(path):
    engine = create_engine("sqlite:///" + path.as_posix())
    now = datetime(2030, 12, 1, 22, 30, 12, 123456, tzinfo=UTC)
    records = {
        "churches": [{"name": "Igreja Fictícia", "timezone": "America/Sao_Paulo"}],
        "ministries": [{"church_id": 17, "name": "Ministério Fictício"}],
        "general_admins": [{"church_id": 17, "phone_e164": "+15550100099"}],
        "coordinators": [
            {"church_id": 17, "name": "Coordenação Fictícia", "phone_e164": "+15550100000"}
        ],
        "coordinator_ministries": [{"coordinator_id": 17, "ministry_id": 17}],
        "volunteers": [
            {
                "church_id": 17,
                "name": "Voluntário Fictício 1",
                "phone_e164": "+15550100001",
                "invite_opt_in": True,
            },
            {
                "church_id": 17,
                "name": "Voluntário Fictício 2",
                "phone_e164": "+15550100002",
                "invite_opt_in": False,
            },
            {
                "church_id": 17,
                "name": "Voluntário Fictício 3",
                "phone_e164": "+15550100003",
                "active": False,
            },
        ],
        "roles": [{"ministry_id": 17, "name": "Abrir"}, {"ministry_id": 17, "name": "Ofertas"}],
        "role_conflicts": [{"role_a_id": 17, "role_b_id": 18}],
        "volunteer_roles": [
            {"volunteer_id": 17, "role_id": 17},
            {"volunteer_id": 18, "role_id": 17},
        ],
        "worship_services": [
            {
                "ministry_id": 17,
                "service_date": date(2030, 12, 8),
                "start_time": time(19, 30, 12, 456789),
            }
        ],
        "slots": [
            {"service_id": 17, "role_id": 17, "capacity": 1},
            {"service_id": 17, "role_id": 18, "capacity": 1},
        ],
        "third_day_requests": [
            {
                "coordinator_id": 17,
                "volunteer_id": 17,
                "slot_id": 17,
                "justification": "Necessidade fictícia documentada",
                "status": "accepted",
                "responded_at": now,
                "service_fingerprint": "a" * 64,
            }
        ],
        "assignments": [
            {
                "slot_id": 17,
                "volunteer_id": 17,
                "exception_id": 17,
                "status": "replacement_requested",
            },
            {
                "slot_id": 17,
                "volunteer_id": 17,
                "status": "cancelled",
                "created_at": now - timedelta(days=3) + timedelta(hours=1),
            },
        ],
        "replacement_requests": [{"assignment_id": 17, "status": "pending"}],
        "monthly_schedules": [
            {"ministry_id": 17, "year": 2030, "month": 12, "state": "published", "version": 1}
        ],
        "roster_proposals": [
            {
                "ministry_id": 17,
                "month": date(2030, 12, 1),
                "policy_version": "fictitious-v1",
                "created_by_type": "coordinator",
                "created_by_id": 17,
                "unfilled": [{"slot_id": 18}],
            }
        ],
        "roster_proposal_items": [
            {
                "proposal_id": 17,
                "position": 1,
                "slot_id": 17,
                "volunteer_id": 18,
                "score": {"participations": 0},
            }
        ],
        "automation_invitations": [
            {
                "kind": "replacement",
                "source_key": "fictitious-17",
                "slot_id": 17,
                "volunteer_id": 18,
                "replacement_request_id": 17,
                "status": "pending",
                "reason": "Troca fictícia",
                "service_fingerprint": "a" * 64,
                "expires_at": now + timedelta(hours=24),
            },
            {
                "kind": "emergency",
                "source_key": "fictitious-18",
                "slot_id": 17,
                "volunteer_id": 17,
                "assignment_id": 18,
                "status": "accepted",
                "reason": "Convite histórico aceito antes do cancelamento da participação",
                "service_fingerprint": "a" * 64,
                "created_at": now - timedelta(days=3),
                "expires_at": now - timedelta(days=2),
                "responded_at": now - timedelta(days=3) + timedelta(hours=1),
            },
            {
                "kind": "emergency",
                "source_key": "fictitious-19",
                "slot_id": 17,
                "volunteer_id": 17,
                "status": "declined",
                "reason": "Convite histórico recusado",
                "service_fingerprint": "a" * 64,
                "created_at": now - timedelta(days=2),
                "expires_at": now - timedelta(days=1),
                "responded_at": now - timedelta(days=2) + timedelta(hours=2),
            },
            {
                "kind": "emergency",
                "source_key": "fictitious-20",
                "slot_id": 17,
                "volunteer_id": 17,
                "status": "expired",
                "reason": "Convite histórico expirado sem resposta",
                "service_fingerprint": "a" * 64,
                "created_at": now - timedelta(days=2),
                "expires_at": now - timedelta(days=1),
                "responded_at": now - timedelta(days=1),
            },
            {
                "kind": "emergency",
                "source_key": "fictitious-21",
                "slot_id": 17,
                "volunteer_id": 17,
                "status": "cancelled",
                "reason": "Convite histórico cancelado",
                "service_fingerprint": "a" * 64,
                "created_at": now - timedelta(days=2),
                "expires_at": now - timedelta(days=1),
                "responded_at": now - timedelta(days=2) + timedelta(hours=3),
            },
        ],
        "waitlist_entries": [
            {
                "volunteer_id": 19,
                "slot_id": 17,
                "status": "waiting",
                "service_fingerprint": "a" * 64,
            }
        ],
        "automation_policies": [
            {"ministry_id": 17, "opening_enabled": False, "opening_time": time(9, 15, 12, 123456)}
        ],
        "emergency_policies": [{"ministry_id": 17, "enabled": False}],
        "reminder_policies": [
            {"ministry_id": 17, "enabled": False, "local_time": time(9, 30, 12, 123456)}
        ],
        "unavailabilities": [
            {"volunteer_id": 19, "start_date": date(2030, 12, 1), "end_date": date(2030, 12, 31)}
        ],
        "conversation_sessions": [
            {
                "phone_e164": "+15550100001",
                "state": "menu",
                "data": {"ministry_id": 17},
                "expires_at": now + timedelta(hours=24),
                "last_inbound_at": now,
                "rate_window_at": now,
            }
        ],
        "inbound_events": [
            {
                "event_id": "fictitious-inbound-17",
                "kind": "message",
                "payload": {"fictitious": True},
                "status": "processed",
            }
        ],
        "outbound_messages": [
            {
                "recipient_e164": "+15550100002",
                "volunteer_id": 18,
                "automation_invitation_id": 17,
                "status": "unknown",
                "attempts": 1,
                "error_code": "network_ambiguous",
                "dedupe_key": "fictitious-notice-17",
                "payload": {"body": "Convite fictício"},
            },
            {
                "recipient_e164": "+15550100001",
                "volunteer_id": 17,
                "status": "pending",
                "dedupe_key": "fictitious-notice-18",
                "payload": {"body": "Aviso fictício pendente"},
            },
            {
                "recipient_e164": "+15550100001",
                "volunteer_id": 17,
                "automation_invitation_id": 18,
                "status": "sent",
                "attempts": 1,
                "meta_message_id": "wamid.fictitious-sent-19",
                "dedupe_key": "fictitious-notice-19",
                "payload": {"body": "Convite fictício enviado"},
                "created_at": now - timedelta(days=3),
            },
            {
                "recipient_e164": "+15550100001",
                "volunteer_id": 17,
                "status": "failed",
                "attempts": 1,
                "error_code": "credentials_missing",
                "dedupe_key": "fictitious-notice-20",
                "payload": {"body": "Aviso fictício com falha"},
                "created_at": now - timedelta(days=2),
            },
            {
                "recipient_e164": "+15550100002",
                "volunteer_id": 18,
                "status": "blocked_consent",
                "error_code": "consent_required",
                "dedupe_key": "fictitious-notice-21",
                "payload": {"body": "Aviso fictício sem consentimento"},
            },
            {
                "recipient_e164": "+15550100001",
                "volunteer_id": 17,
                "status": "blocked_window",
                "error_code": "template_required",
                "dedupe_key": "fictitious-notice-22",
                "payload": {"body": "Aviso fictício fora da janela"},
                "created_at": now - timedelta(days=2),
            },
        ],
        "audit_events": [
            {
                "church_id": 17,
                "ministry_id": 17,
                "actor_type": "coordinator",
                "actor_id": 17,
                "action": "fictitious.audit",
                "entity_type": "schedule",
                "entity_id": 17,
                "details": {"preserve": True},
            }
        ],
        "schedule_versions": [
            {
                "schedule_id": 17,
                "version": 1,
                "content": "Escala histórica fictícia",
                "fingerprint": "b" * 64,
            }
        ],
    }
    assert set(records) == set(Base.metadata.tables), "Every table needs explicit historical data"
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            db.exec_driver_sql("PRAGMA foreign_keys=ON")
            for table in Base.metadata.sorted_tables:
                for offset, values in enumerate(records[table.name]):
                    values = dict(values)
                    if "id" in table.c:
                        values["id"] = 17 + offset
                    for name in ("created_at", "updated_at"):
                        if name in table.c:
                            values.setdefault(name, now)
                    db.execute(table.insert().values(**values))
            # The raw SQLite representation must retain precision before either
            # SQLAlchemy's usual JSON float parser or PostgreSQL sees the value.
            db.execute(
                text("UPDATE audit_events SET details=:value"),
                {"value": '{"precise":0.123456789012345678901234567890,"null":null}'},
            )
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
    return path
