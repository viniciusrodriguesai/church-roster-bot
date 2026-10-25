from datetime import UTC, date, datetime, time

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from church_roster_bot.core.database import Base


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_confirmation', 'confirmed', 'replacement_requested', "
            "'replaced', 'cancelled', 'completed', 'absent', 'declined')",
            name="valid_assignment_status",
        ),
        Index(
            "uq_confirmed_assignment_per_slot_volunteer",
            "slot_id",
            "volunteer_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending_confirmation', 'confirmed', "
                "'replacement_requested', 'completed')"
            ),
            sqlite_where=text(
                "status IN ('pending_confirmation', 'confirmed', "
                "'replacement_requested', 'completed')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="confirmed")
    exception_id: Mapped[int | None] = mapped_column(
        ForeignKey("third_day_requests.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ThirdDayRequest(Base):
    __tablename__ = "third_day_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'declined')", name="valid_exception_status"
        ),
        CheckConstraint("length(trim(justification)) > 0", name="nonempty_exception_justification"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    coordinator_id: Mapped[int] = mapped_column(ForeignKey("coordinators.id"), nullable=False)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    justification: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MonthlySchedule(Base):
    __tablename__ = "monthly_schedules"
    __table_args__ = (
        CheckConstraint("month BETWEEN 1 AND 12", name="valid_schedule_month"),
        CheckConstraint(
            "state IN ('draft', 'open', 'under_review', 'published', 'closed', 'cancelled')",
            name="valid_schedule_state",
        ),
        Index("uq_ministry_year_month", "ministry_id", "year", "month", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ministry_id: Mapped[int] = mapped_column(ForeignKey("ministries.id"), nullable=False)
    year: Mapped[int] = mapped_column(nullable=False)
    month: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(nullable=False, default=0)


class ReplacementRequest(Base):
    __tablename__ = "replacement_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'fulfilled', 'cancelled')", name="valid_replacement_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    replacement_assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class RosterProposal(Base):
    __tablename__ = "roster_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'cancelled')", name="valid_proposal_status"
        ),
        CheckConstraint("created_by_type IN ('admin', 'coordinator')", name="valid_proposal_actor"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ministry_id: Mapped[int] = mapped_column(ForeignKey("ministries.id"), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_by_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_id: Mapped[int] = mapped_column(nullable=False)
    unfilled: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RosterProposalItem(Base):
    __tablename__ = "roster_proposal_items"
    __table_args__ = (
        UniqueConstraint("proposal_id", "position"),
        UniqueConstraint("proposal_id", "slot_id", "volunteer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("roster_proposals.id"), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    score: Mapped[dict] = mapped_column(JSON, nullable=False)


class AutomationInvitation(Base):
    __tablename__ = "automation_invitations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('distribution', 'waitlist', 'replacement', 'emergency')",
            name="valid_automation_invitation_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'expired', 'cancelled')",
            name="valid_automation_invitation_status",
        ),
        CheckConstraint("expires_at > created_at", name="valid_automation_invitation_expiry"),
        Index(
            "uq_pending_automation_pair",
            "volunteer_id",
            "slot_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    proposal_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("roster_proposal_items.id"), unique=True
    )
    replacement_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("replacement_requests.id")
    )
    assignment_id: Mapped[int | None] = mapped_column(ForeignKey("assignments.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    service_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AutomationPolicy(Base):
    __tablename__ = "automation_policies"
    __table_args__ = (CheckConstraint("opening_day BETWEEN 1 AND 28", name="valid_opening_day"),)

    ministry_id: Mapped[int] = mapped_column(ForeignKey("ministries.id"), primary_key=True)
    opening_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    opening_day: Mapped[int] = mapped_column(nullable=False, default=25)
    opening_time: Mapped[time] = mapped_column(
        Time(timezone=False), nullable=False, default=time(9)
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'offered', 'accepted', 'declined', "
            "'expired', 'cancelled', 'ineligible')",
            name="valid_waitlist_status",
        ),
        Index(
            "uq_active_waitlist_pair",
            "volunteer_id",
            "slot_id",
            unique=True,
            sqlite_where=text("status IN ('waiting', 'offered')"),
            postgresql_where=text("status IN ('waiting', 'offered')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")
    service_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_invitations.id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
