from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from church_roster_bot.core.database import Base


class Church(Base):
    __tablename__ = "churches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Sao_Paulo")


class Ministry(Base):
    __tablename__ = "ministries"
    __table_args__ = (UniqueConstraint("church_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    church_id: Mapped[int] = mapped_column(ForeignKey("churches.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("ministry_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ministry_id: Mapped[int] = mapped_column(ForeignKey("ministries.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)


class RoleConflict(Base):
    __tablename__ = "role_conflicts"
    __table_args__ = (
        CheckConstraint("role_a_id < role_b_id", name="ordered_role_pair"),
        UniqueConstraint("role_a_id", "role_b_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    role_a_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
    role_b_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False)
