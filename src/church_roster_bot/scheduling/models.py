from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from church_roster_bot.core.database import Base


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint("status IN ('confirmed', 'cancelled')", name="valid_assignment_status"),
        Index(
            "uq_confirmed_assignment_per_slot_volunteer",
            "slot_id",
            "volunteer_id",
            unique=True,
            postgresql_where=text("status = 'confirmed'"),
            sqlite_where=text("status = 'confirmed'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slots.id"), nullable=False)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="confirmed")
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
