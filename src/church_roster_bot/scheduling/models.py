from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
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
