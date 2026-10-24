from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from church_roster_bot.core.database import Base


class Unavailability(Base):
    __tablename__ = "unavailabilities"
    __table_args__ = (CheckConstraint("start_date <= end_date", name="valid_unavailability_range"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
