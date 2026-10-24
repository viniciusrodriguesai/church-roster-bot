from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from church_roster_bot.core.database import Base


class Volunteer(Base):
    __tablename__ = "volunteers"
    __table_args__ = (UniqueConstraint("church_id", "phone_e164"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    invite_opt_in: Mapped[bool] = mapped_column(nullable=False, default=False)


class VolunteerRole(Base):
    __tablename__ = "volunteer_roles"
    __table_args__ = (UniqueConstraint("volunteer_id", "role_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    volunteer_id: Mapped[int] = mapped_column(ForeignKey("volunteers.id"), nullable=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)


class Coordinator(Base):
    __tablename__ = "coordinators"
    __table_args__ = (UniqueConstraint("church_id", "phone_e164"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
