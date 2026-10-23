"""Unit tests for the decision logic: presence, heat rules, escalation."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.escalation import ACKNOWLEDGED, CHECKIN, CONTACT, RESOLVED, UNANSWERED, Escalator
from app.presence import AWAY, DOOR, HOME, PresenceTracker
from app.rules import EXTREME, HEAT, NONE, assess_heat, in_quiet_hours, inactivity_reference, threshold_for
from app.settings import Settings

TZ = ZoneInfo("Europe/Berlin")


def t(hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2022, 7, 20, hh, mm, ss, tzinfo=TZ)


# ------------------------------------------------------------------ presence


def test_leaving_is_door_then_outside_only():
    p = PresenceTracker()
    p.start(t(8))
    p.observe("door_open", t(9, 30))
    p.observe("outdoor_person", t(9, 30, 20))
    p.observe("door_close", t(9, 30, 40))
    assert p.status == DOOR
    assert p.tick(t(9, 35)) == []  # still inside the resolve window
    changes = p.tick(t(9, 41))
    assert p.status == AWAY and p.left_today
    assert changes[0].title == "Left the house"


def test_coming_home_is_door_then_inside_movement():
    p = PresenceTracker()
    p.start(t(8), AWAY)
    p.observe("outdoor_person", t(10, 25))  # before the door: ignored for direction
    p.observe("door_open", t(10, 25, 30))
    p.observe("door_close", t(10, 25, 50))
    changes = p.observe("indoor_activity", t(10, 26), "Living Room")
    assert p.status == HOME
    assert p.last_return == t(10, 26)
    assert changes[0].title == "Came home"


def test_door_used_nobody_seen_leaving_means_still_home():
    p = PresenceTracker()
    p.start(t(8))
    p.observe("door_open", t(12))
    p.observe("door_close", t(12, 1))
    p.tick(t(12, 12))
    assert p.status == HOME


def test_open_door_never_resolves_while_open():
    p = PresenceTracker()
    p.start(t(8))
    p.observe("door_open", t(12))
    p.tick(t(13))
    assert p.status == DOOR


def test_garden_activity_counts_only_when_home():
    p = PresenceTracker()
    p.start(t(8), AWAY)
    p.observe("garden_activity", t(9), "Backyard")
    assert p.last_activity is None
    p.start(t(8), HOME)
    p.observe("garden_activity", t(9), "Backyard")
    assert p.last_activity == t(9)


# ------------------------------------------------------------------ rules


def test_heat_levels_follow_feels_like_thresholds():
    s = Settings()
    assert assess_heat(s, 25, False).level == NONE
    assert assess_heat(s, 32, False).level == HEAT
    assert assess_heat(s, 38.5, False).level == EXTREME
    indoor = assess_heat(s, 20, True, "Indoor above 30 °C")
    assert indoor.level == HEAT and "Indoor above 30 °C" in indoor.reasons


def test_thresholds_shrink_with_heat():
    s = Settings()
    assert threshold_for(s, NONE) == timedelta(hours=4)
    assert threshold_for(s, HEAT) == timedelta(hours=2)
    assert threshold_for(s, EXTREME) == timedelta(minutes=90)


def test_quiet_hours_cross_midnight():
    s = Settings()  # 22:00-07:00
    assert in_quiet_hours(s, t(23))
    assert in_quiet_hours(s, t(3))
    assert not in_quiet_hours(s, t(7))
    assert not in_quiet_hours(s, t(21, 59))


def test_inactivity_counts_from_end_of_night_rest():
    s = Settings()
    ref = inactivity_reference(
        s, t(9), last_activity=t(21, 30) - timedelta(days=1), home_since=None, monitoring_since=t(0) - timedelta(days=2)
    )
    assert ref == t(7)  # sleeping in is fine until the threshold passes after 07:00


def test_inactivity_counts_from_last_movement():
    s = Settings()
    ref = inactivity_reference(s, t(13), last_activity=t(11, 5), home_since=t(10, 26), monitoring_since=t(8))
    assert ref == t(11, 5)


# ------------------------------------------------------------------ escalation


def test_checkin_answered_alerts_nobody():
    s = Settings()
    e = Escalator()
    actions = e.open(t(13, 5), "No movement for 2 h", HEAT, 120, s)
    assert [a.kind for a in actions] == ["chime"]
    assert [a.kind for a in e.tick(t(13, 8), s)] == ["chime"]  # repeated at half window
    assert e.on_activity(t(13, 9), "Living Room", s) == []
    assert e.active is None
    inc = e.history[-1]
    assert inc.stage == RESOLVED and inc.notified == []


def test_full_escalation_path():
    s = Settings()  # 5 min check-in window, 15 min per contact
    e = Escalator()
    e.open(t(13, 5), "No movement for 2 h", HEAT, 120, s)
    e.tick(t(13, 8), s)
    first = e.tick(t(13, 10), s)
    assert e.active.stage == CONTACT
    assert [(a.kind, a.contact_id) for a in first] == [("notify", "c1"), ("snapshot", "c1")]
    second = e.tick(t(13, 25), s)
    assert [(a.kind, a.contact_id) for a in second] == [("notify", "c2")]
    assert "Anna did not respond" in second[0].body and "key" in second[0].body
    final = e.tick(t(13, 40), s)
    assert e.active.stage == UNANSWERED
    assert {a.contact_id for a in final} == {"c1", "c2"} and "112" in final[0].body
    clear = e.on_activity(t(13, 45), "Living Room", s)
    assert {a.contact_id for a in clear} == {"c1", "c2"}
    assert all(a.category == "all_clear" for a in clear)


def test_acknowledge_pauses_escalation_until_resolved():
    s = Settings()
    e = Escalator()
    e.open(t(13, 5), "No movement", HEAT, 120, s)
    e.tick(t(13, 8), s)
    e.tick(t(13, 10), s)
    e.acknowledge("c1", t(13, 12), s)
    assert e.active.stage == ACKNOWLEDGED
    assert e.tick(t(13, 30), s) == []  # within the 30 min follow-up window nobody else is bothered
    e.resolve_by_contact("c1", t(13, 35), s)
    assert e.active is None and e.history[-1].resolution.startswith("Confirmed OK by Anna")


def test_unconfirmed_acknowledgement_is_followed_up_then_escalated():
    s = Settings()  # follow-up 30 min, then 15 min to answer
    e = Escalator()
    e.open(t(13, 5), "No movement", HEAT, 120, s)
    e.tick(t(13, 8), s)
    e.tick(t(13, 10), s)  # Anna alerted
    e.acknowledge("c1", t(13, 12), s)
    reminder = e.tick(t(13, 42), s)
    assert [(a.kind, a.contact_id) for a in reminder] == [("notify", "c1")]
    assert "Mr. Weber" in reminder[0].body  # tells Anna who comes next
    assert e.tick(t(13, 50), s) == []
    moved_on = e.tick(t(13, 57), s)
    assert e.active.stage == CONTACT and e.active.acknowledged_by is None
    assert [(a.kind, a.contact_id) for a in moved_on] == [("notify", "c2")]
    assert "has not confirmed" in moved_on[0].body


def test_still_on_it_resets_the_follow_up():
    s = Settings()
    e = Escalator()
    e.open(t(13, 5), "No movement", HEAT, 120, s)
    e.tick(t(13, 8), s)
    e.tick(t(13, 10), s)
    e.acknowledge("c1", t(13, 12), s)
    e.tick(t(13, 42), s)  # reminder
    assert e.acknowledge("c1", t(13, 45), s) == []  # "still on it": no repeat notification to others
    assert e.tick(t(14, 0), s) == []  # window restarted at 13:45
    assert e.active.stage == ACKNOWLEDGED


def test_late_acknowledgement_by_second_contact_skips_ahead():
    s = Settings()
    e = Escalator()
    e.open(t(13, 5), "No movement", HEAT, 120, s)
    e.tick(t(13, 8), s)
    e.tick(t(13, 10), s)  # Anna alerted
    e.acknowledge("c2", t(13, 11), s)  # neighbour saw it first
    e.tick(t(13, 41), s)  # reminder to Mr. Weber
    e.tick(t(13, 56), s)  # no answer: nobody left after him -> everyone
    assert e.active.stage == UNANSWERED


def test_chime_offline_alerts_first_contact_directly():
    s = Settings()
    e = Escalator()
    actions = e.open(t(11, 20), "No movement for 2 h", HEAT, 120, s, chime_available=False)
    assert e.active.stage == CONTACT and e.active.checkin_skipped
    assert [(a.kind, a.contact_id) for a in actions] == [("notify", "c1"), ("snapshot", "c1")]
    assert "chime is offline" in actions[0].body


def test_escalator_state_round_trip():
    from app import persist

    s = Settings()
    e = Escalator()
    e.open(t(13, 5), "No movement", HEAT, 120, s)
    e.tick(t(13, 8), s)
    e.tick(t(13, 10), s)
    raw = persist.decode(__import__("json").loads(__import__("json").dumps(persist.encode(e.to_state()))))
    back = Escalator.from_state(raw)
    assert back.active is not None and back.active.stage == CONTACT
    assert back.active.opened_at == t(13, 5) and back.active.chime_plays == [t(13, 5), t(13, 8)]
    assert back.tick(t(13, 25), s)[0].contact_id == "c2"  # escalation continues after restore


def test_cannot_acknowledge_during_checkin():
    s = Settings()
    e = Escalator()
    e.open(t(13, 5), "No movement", HEAT, 120, s)
    assert e.acknowledge("c1", t(13, 6), s) == []
    assert e.active.stage == CHECKIN
