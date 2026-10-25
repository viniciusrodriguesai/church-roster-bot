"""Replacement discovery and decisions preserve one owner under competing transactions."""

import pytest
from sqlalchemy import func, select

from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.lifecycle import manual_replace
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, ReplacementRequest
from church_roster_bot.scheduling.offers import respond_offer
from church_roster_bot.scheduling.replacements import dispatch_replacements
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_automatic_replacements import CONFIG, dispatch, pending_offer, setup_replacement
from tests.test_automation_concurrency import run_race

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def test_competing_searches_create_only_one_candidate_offer(roster):
    engine, factory, _ = roster
    setup_replacement(roster)
    results = run_race(factory, lambda db, _: dispatch_replacements(db, engine, CONFIG))
    assert sorted(results) == [0, 1]
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1
        assert db.scalar(select(func.count(Assignment.id))) == 1


def test_concurrent_acceptances_replace_once_and_notify_original_once(roster):
    engine, factory, _ = roster
    members, _, _, request_id = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    results = run_race(
        factory, lambda db, _: respond_offer(db, engine, members[1], offer.id, True, CONFIG)
    )
    assert results[0] == results[1] and results[0].status == "accepted"
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 2
        assert db.get(ReplacementRequest, request_id).status == "fulfilled"
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.volunteer_id == members[0]
                )
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key.like(f"automation:{offer.id}:accepted:%")
                )
            )
            == 1
        )


def test_manual_replacement_racing_acceptance_has_one_winner(roster):
    engine, factory, _ = roster
    members, _, _, request_id = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)

    def operation(db, index):
        if index:
            try:
                return manual_replace(db, engine, request_id, members[2])
            except RosterError:
                return None
        return respond_offer(db, engine, members[1], offer.id, True, CONFIG)

    run_race(factory, operation)
    dispatch(roster)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 2
        assert (
            db.scalar(select(func.count(Assignment.id)).where(Assignment.status == "confirmed"))
            == 1
        )
        assert db.get(ReplacementRequest, request_id).status == "fulfilled"
        assert db.get(AutomationInvitation, offer.id).status in {"accepted", "cancelled"}


def test_decline_racing_search_eventually_invites_next_candidate_once(roster):
    engine, factory, _ = roster
    members, _, _, _ = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)

    def operation(db, index):
        if index:
            return dispatch_replacements(db, engine, CONFIG)
        return respond_offer(db, engine, members[1], offer.id, False, CONFIG)

    run_race(factory, operation)
    dispatch(roster)
    assert pending_offer(roster).volunteer_id == members[2]
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 2
