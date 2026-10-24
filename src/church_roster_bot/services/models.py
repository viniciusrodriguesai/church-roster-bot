from datetime import date, time

from sqlalchemy import CheckConstraint, Date, ForeignKey, Time, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from church_roster_bot.core.database import Base


class WorshipService(Base):
    __tablename__ = "worship_services"
    __table_args__ = (UniqueConstraint("ministry_id", "service_date", "start_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ministry_id: Mapped[int] = mapped_column(ForeignKey("ministries.id"), nullable=False)
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (
        UniqueConstraint("service_id", "role_id"),
        CheckConstraint("capacity > 0", name="positive_slot_capacity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("worship_services.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    capacity: Mapped[int] = mapped_column(nullable=False)
