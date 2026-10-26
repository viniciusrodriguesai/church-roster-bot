"""Current grants protect global identities shared by multiple ministries."""

import pytest
from sqlalchemy import select

from church_roster_bot.administration.service import edit_volunteer
from church_roster_bot.members.models import (
    Coordinator,
    CoordinatorMinistry,
    GeneralAdmin,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.ministries.models import Ministry, Role
from church_roster_bot.scheduling.errors import NotAuthorized
from church_roster_bot.scripts.simulate import simulate
from tests.test_phase2 import COORD, FIRST, bodies


def shared_member(roster):
    engine, factory, ids = roster
    with factory.begin() as db:
        other = Ministry(church_id=ids["church"], name="Private fictitious ministry")
        db.add(other)
        db.flush()
        role = Role(ministry_id=other.id, name="Private fictitious role")
        admin = GeneralAdmin(church_id=ids["church"], phone_e164="+15550909999")
        db.add_all([role, admin])
        db.flush()
        other_id, role_id, admin_id = other.id, role.id, admin.id
    member = engine.register_volunteer(
        ids["church"],
        VolunteerCreate(
            name="Shared Fictitious Member", phone_e164=FIRST, role_ids={ids["offer"], role_id}
        ),
    )
    return member, other_id, role_id, admin_id


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
class TestAuthorizationHardening:
    def test_changed_phone_requires_consent_from_new_number(self, roster):
        member, other, _, admin = shared_member(roster)
        _, factory, ids = roster
        simulate(FIRST, "INICIAR", factory=factory)
        with factory.begin() as db:
            edit_volunteer(db, member, "phone", FIRST, ids["ministry"], "admin", admin)
            assert db.get(Volunteer, member).invite_opt_in
            db.add(CoordinatorMinistry(coordinator_id=ids["coordinator"], ministry_id=other))
        for command in [
            "MENU",
            str(ids["ministry"]),
            "1",
            f"EDITAR {member}",
            "TELEFONE +15550908888",
        ]:
            simulate(COORD, command, factory=factory)
        assert "INICIAR" in bodies(simulate(COORD, "SIM", factory=factory))
        with factory() as db:
            assert not db.get(Volunteer, member).invite_opt_in
        simulate("+15550908888", "INICIAR", factory=factory)
        with factory() as db:
            assert db.get(Volunteer, member).invite_opt_in

    @pytest.mark.parametrize(
        "field,value", [("phone", COORD), ("name", "Changed"), ("active", "false")]
    )
    def test_partial_coordinator_cannot_modify_global_shared_fields(self, roster, field, value):
        member, _, _, _ = shared_member(roster)
        with roster[1].begin() as db:
            with pytest.raises(NotAuthorized):
                edit_volunteer(
                    db,
                    member,
                    field,
                    value,
                    roster[2]["ministry"],
                    "coordinator",
                    roster[2]["coordinator"],
                )
        with roster[1]() as db:
            current = db.get(Volunteer, member)
            assert (current.phone_e164, current.name, current.active) == (
                FIRST,
                "Shared Fictitious Member",
                True,
            )

    def test_phone_takeover_is_blocked_in_real_coordinator_conversation(self, roster):
        member, _, _, _ = shared_member(roster)
        factory = roster[1]
        for command in ["MENU", "1", f"EDITAR {member}", f"TELEFONE {COORD}"]:
            simulate(COORD, command, factory=factory)
        reply = bodies(simulate(COORD, "SIM", factory=factory))
        assert "Cadastro atualizado" not in reply
        with factory() as db:
            assert db.get(Volunteer, member).phone_e164 == FIRST

    def test_local_role_edit_preserves_other_ministry_permissions(self, roster):
        member, _, other_role, _ = shared_member(roster)
        _, factory, ids = roster
        with factory.begin() as db:
            edit_volunteer(
                db,
                member,
                "roles",
                str(ids["open"]),
                ids["ministry"],
                "coordinator",
                ids["coordinator"],
            )
        with factory() as db:
            assert set(
                db.scalars(
                    select(VolunteerRole.role_id).where(VolunteerRole.volunteer_id == member)
                )
            ) == {ids["open"], other_role}

    @pytest.mark.parametrize("actor", ["admin", "all_ministries_coordinator"])
    def test_global_edits_require_all_current_grants_or_general_admin(self, roster, actor):
        member, other, _, admin = shared_member(roster)
        _, factory, ids = roster
        with factory.begin() as db:
            if actor == "all_ministries_coordinator":
                db.add(CoordinatorMinistry(coordinator_id=ids["coordinator"], ministry_id=other))
                db.flush()
            edit_volunteer(
                db,
                member,
                "phone",
                "+15550908888",
                ids["ministry"],
                "admin" if actor == "admin" else "coordinator",
                admin if actor == "admin" else ids["coordinator"],
            )
        with factory() as db:
            assert db.get(Volunteer, member).phone_e164 == "+15550908888"

    @pytest.mark.parametrize("actor", ["revoked", "volunteer", "other_ministry"])
    def test_service_boundary_rejects_invalid_or_revoked_authority(self, roster, actor):
        member, other, _, _ = shared_member(roster)
        _, factory, ids = roster
        if actor == "revoked":
            with factory.begin() as db:
                db.get(Coordinator, ids["coordinator"]).active = False
        with factory.begin() as db:
            with pytest.raises(NotAuthorized):
                edit_volunteer(
                    db,
                    member,
                    "roles",
                    str(ids["offer"]),
                    other if actor == "other_ministry" else ids["ministry"],
                    "volunteer" if actor == "volunteer" else "coordinator",
                    member if actor == "volunteer" else ids["coordinator"],
                )
