"""In-process receivers of the ``work`` delta (RFC fold-delta-push, PR d).

What is pinned here:

1. The shared fixtures (``website/src/test/fixtures/work-delta-cases.json``) are what
   :func:`fold_delta.diff_work` makes and :func:`fold_delta.apply_work` applies. The
   browser's twin runs the same file (``workDelta.test.ts``).
2. The bus applies the receiver rule for a baseline subscriber: a delta on the floor
   is handed over as it is, any other one as ``snapshot_needed`` (reason ``gap``), live
   and in the replay of events held during the baseline read. A delta fold's baseline is
   labelled with the revision last published, so the next delta applies on it.
3. ``conductor_wake`` records the same stamps and reports the same items from deltas as
   from whole values over the same real advances, and reads the board when an event
   carries neither.
4. ``DashboardFeed`` applies deltas, ends equal to every published value, and re-reads
   the cell off the fold worker when an event cannot be applied and carries no value.
5. The REST read labels a ``work`` snapshot with ``published_revision`` read BEFORE the
   fold. Metrics never sees a ``work`` delta: it subscribes to session folds only.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
from typing import Any

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 - autouse fixture
    WORK_SLOT,
    _isolated_home,
)
from test_crew_log_fold_delta import _publish_now, _Writer

from kiro_crew import conductor_wake
from kiro_crew.crew_log import bus, eager, fold_delta
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.testing.wait import wait_until

FIXTURES = (
    pathlib.Path(__file__).resolve().parent.parent
    / "website"
    / "src"
    / "test"
    / "fixtures"
    / "work-delta-cases.json"
)


@pytest.fixture(autouse=True)
def _fresh_bus():
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    conductor_wake.dispose_all()
    yield
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    conductor_wake.dispose_all()


def _cases() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURES.read_text())["cases"])


def _delta(revision: int, base: int, value: Any = None, ops: Any = None) -> bus.FoldAdvanced:
    return bus.FoldAdvanced(
        scope=bus.SCOPE_SLOT,
        key=WORK_SLOT,
        fold="work",
        revision=revision,
        value=value if value is not None else {},
        seq=1,
        kind=fold_delta.KIND_DELTA,
        base_revision=base,
        delta=ops if ops is not None else {"header": {"order": []}, "items": {}, "remove": []},
    )


# --------------------------------------------------------------------------- #
# 1. the shared fixtures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_the_shared_fixtures_apply_in_python(case):
    assert fold_delta.apply_work(case["before"], case["ops"]) == case["after"]
    if case["after"] is not None:
        assert fold_delta.diff_work(case["before"], case["after"]) == case["ops"]


def test_value_after_applies_on_its_base_and_falls_back_to_the_carried_value():
    case = next(c for c in _cases() if c["name"] == "one item reports")
    before, after, ops = case["before"], case["after"], case["ops"]
    on_base = _delta(6, 5, value=after, ops=ops)
    assert fold_delta.value_after(before, 5, on_base) == after
    # The carried value is NOT what was returned: a delta on its base is applied.
    bare = _delta(6, 5, value={}, ops=ops)
    assert fold_delta.value_after(before, 5, bare) == after
    # A gap: the carried value while there is one, else ``None`` (pull a snapshot).
    assert fold_delta.value_after(before, 4, on_base) == after
    assert fold_delta.value_after(before, 4, bare) is None
    snapshot = bare._replace(kind=fold_delta.KIND_SNAPSHOT_NEEDED, base_revision=0)
    assert fold_delta.value_after(before, 5, snapshot) is None


def test_chained_relabels_only_a_delta_off_the_held_base():
    event = _delta(6, 5, value={"items": []})
    assert fold_delta.chained(event, 5) is event
    gap = fold_delta.chained(event, 4)
    assert gap.kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert gap.delta == {"reason": fold_delta.REASON_GAP}
    assert gap.base_revision == 0 and gap.value == {"items": []}, "the value rides along"
    plain = bus.FoldAdvanced("slot", WORK_SLOT, "ledger", 3, {"x": 1}, 1)
    assert fold_delta.chained(plain, 0) is plain


# --------------------------------------------------------------------------- #
# 2. the bus: baseline label, live chain, replay of held events
# --------------------------------------------------------------------------- #


def test_a_baseline_is_labelled_with_the_published_revision_and_the_next_delta_applies():
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    writer.report("it_a", "progress", "half")
    _publish_now()
    published = eager.published_revision(WORK_SLOT, "work")
    assert published > 0
    got: list[bus.FoldAdvanced] = []
    bus.subscribe(
        bus.FOLD_ADVANCED, got.append, scope="slot", key=WORK_SLOT, fold="work", baseline=True
    )
    assert len(got) == 1 and got[0].revision == published
    writer.report("it_a", "done", "all")
    _publish_now()
    assert len(got) == 2
    delta = got[1]
    assert delta.kind == fold_delta.KIND_DELTA and delta.base_revision == published
    assert fold_delta.apply_work(got[0].value, delta.delta or {}) == dict(delta.value)


def test_a_live_delta_off_the_floor_reaches_a_baseline_subscriber_as_snapshot_needed():
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    got: list[bus.FoldAdvanced] = []
    bus.subscribe(
        bus.FOLD_ADVANCED, got.append, scope="slot", key=WORK_SLOT, fold="work", baseline=True
    )
    floor = got[0].revision
    bus.publish(bus.FOLD_ADVANCED, _delta(floor + 5, floor + 3))
    assert got[-1].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert got[-1].delta == {"reason": fold_delta.REASON_GAP}
    # The chain re-forms on what was delivered: the next delta on that revision applies.
    bus.publish(bus.FOLD_ADVANCED, _delta(floor + 6, floor + 5))
    assert got[-1].kind == fold_delta.KIND_DELTA


def test_an_unfiltered_subscriber_sees_deltas_exactly_as_published():
    got: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, got.append)
    event = _delta(9, 3)
    bus.publish(bus.FOLD_ADVANCED, event)
    assert got == [event]


def test_held_events_replay_on_the_baseline_by_the_base_rule():
    sub = bus._Subscriber(lambda e: seen.append(e), 1, bus.FOLD_ADVANCED, ("slot", "b", "work"))
    seen: list[Any] = []
    sub.pending = [_delta(4, 3), _delta(6, 5), _delta(7, 6), _delta(9, 8)]
    sub.floor = 0
    baseline = bus.FoldAdvanced("slot", "b", "work", 5, {"items": []}, 1)
    bus._release_baseline(sub, baseline)
    assert [(e.revision, e.kind) for e in seen] == [
        (5, "value"),
        (6, fold_delta.KIND_DELTA),
        (7, fold_delta.KIND_DELTA),
        (9, fold_delta.KIND_SNAPSHOT_NEEDED),
    ], "4 is below the baseline, 6 and 7 chain on it, 9's base 8 was never delivered"


# --------------------------------------------------------------------------- #
# 3. conductor_wake
# --------------------------------------------------------------------------- #


def _real_events() -> list[bus.FoldAdvanced]:
    """The ``work`` events a real board publishes over a run of conductor and worker
    writes, closes past the closed cap included, so ``remove`` ops appear."""
    events: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, events.append, scope="slot", key=WORK_SLOT, fold="work")
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    writer.create("it_b")
    writer.report("it_a", "progress", "p1")
    _publish_now()
    writer.report("it_a", "progress", "p2")
    _publish_now()
    writer.report("it_b", "done", "b done")
    _publish_now()
    writer.decide("it_b", "accept")
    writer.report("it_a", "question", "which")
    _publish_now()
    writer.report("it_a", "question", "which")  # identical report: no stamp move
    _publish_now()
    for n in range(34):
        writer.create(f"it_x{n}")
        writer.close(f"it_x{n}")
    _publish_now()
    for n in range(34, 40):
        writer.create(f"it_x{n}")
        writer.close(f"it_x{n}")
    writer.report("it_a", "done", "a done")
    _publish_now()
    return events


def test_conductor_wake_reports_the_same_items_from_deltas_as_from_whole_values():
    events = _real_events()
    kinds = {e.kind for e in events[1:]}
    assert kinds == {fold_delta.KIND_DELTA}, kinds
    assert any((e.delta or {}).get("remove") for e in events), "a remove op was exercised"
    whole = conductor_wake._Registry()
    delta = conductor_wake._Registry()
    for event in events:
        expected = whole.observe(WORK_SLOT, conductor_wake._board_items(dict(event.value)))
        items, order = conductor_wake._event_news(event)
        assert items is not None
        if order is None:
            got = delta.observe(WORK_SLOT, items)
        else:
            got = delta.observe_delta(WORK_SLOT, items, order)
        assert got == expected, event.revision
        assert delta.boards == whole.boards
    reported = [item for e in events[1:] for item in (e.delta or {}).get("items", {})]
    assert "it_a" in reported and "it_b" in reported


def test_conductor_wake_reads_the_board_when_an_event_carries_neither_delta_nor_value(
    monkeypatch,
):
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    bare = bus.FoldAdvanced(
        "slot", WORK_SLOT, "work", 99, {}, 1, kind=fold_delta.KIND_SNAPSHOT_NEEDED
    )
    assert conductor_wake._event_news(bare) == (None, None)
    # A ``value`` event keeps its old reading, even an empty one.
    plain = bus.FoldAdvanced("slot", WORK_SLOT, "work", 99, {}, 1)
    assert conductor_wake._event_news(plain) == ([], None)
    monkeypatch.setattr(conductor_wake, "work_ledger_loop_id", lambda _svc, _board: "loop")
    asyncio.run(conductor_wake._observe_board(object(), WORK_SLOT, None))
    assert set(conductor_wake._registry.boards[WORK_SLOT]) == {"it_a"}


# --------------------------------------------------------------------------- #
# 4. DashboardFeed
# --------------------------------------------------------------------------- #


def _feed() -> "Any":
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    changes: list[str] = []
    feed = DashboardFeed(WORK_SLOT, on_change=changes.append)
    feed._cells["work"] = _Cell()
    feed._disposers.append(
        bus.subscribe(
            bus.FOLD_ADVANCED,
            feed._make_callback("work"),
            scope="slot",
            key=WORK_SLOT,
            fold="work",
            baseline=True,
        )
    )
    return feed, changes


def test_dashboard_feed_applies_deltas_and_ends_equal_to_every_published_value(monkeypatch):
    applied: list[int] = []
    real = fold_delta.apply_work

    def _spy(value, ops):
        out = real(value, ops)
        applied.append(0 if out is None else 1)
        return out

    monkeypatch.setattr(fold_delta, "apply_work", _spy)
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    feed, changes = _feed()
    for n in range(4):
        writer.report("it_a", "progress", f"step {n}")
        _publish_now()
        published = eager._published[(WORK_SLOT, "work")]
        assert feed.fold_value("work") == published[1]
        assert feed._cells["work"].revision == published[0]
    assert applied == [1, 1, 1, 1], "each advance was a delta applied on the held value"
    assert changes == ["work"] * 5


def test_dashboard_feed_rereads_a_cell_it_cannot_apply_and_was_handed_no_value():
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    feed, changes = _feed()
    writer.report("it_a", "done", "d")
    _publish_now()
    held = feed._cells["work"].revision
    # As PR (e) will publish it: a gap and no whole value.
    feed._accept("work", _delta(held + 10, held + 7, value={}))
    wait_until(lambda: not feed._resyncing, describe=lambda: "the re-read finished")
    fresh = crew_log.read_slot_projection(WORK_SLOT, "work")
    assert feed.fold_value("work") == fresh.value
    assert feed._cells["work"].revision == eager.published_revision(WORK_SLOT, "work")
    assert changes[-1] == "work"


def test_dashboard_feed_reads_again_when_a_gap_lands_during_its_re_read(monkeypatch):
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    feed = DashboardFeed(WORK_SLOT)
    feed._cells["work"] = _Cell()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def _slow(_fold):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            release.wait(5)

    monkeypatch.setattr(feed, "_resync_once", _slow)
    feed._resync_soon("work")
    assert entered.wait(5)
    feed._resync_soon("work")
    release.set()
    wait_until(lambda: not feed._resyncing, describe=lambda: "both reads ran")
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# 5. REST label and metrics
# --------------------------------------------------------------------------- #


def test_the_rest_snapshot_reads_value_and_label_under_the_publish_lock(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_log as handler

    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    held: list[bool] = []
    real_read = crew_log.read_slot_projection

    def _read(*a, **k):
        held.append(eager._published_lock._is_owned())
        return real_read(*a, **k)

    monkeypatch.setattr(crew_log, "read_slot_projection", _read)
    monkeypatch.setattr(handler._crew_log(), "slot_of_session", lambda _unit: WORK_SLOT)
    result, published = handler._read_slot_fold_labelled("s-delta", "work")
    assert held == [True]
    assert published == result.revision == eager.published_revision(WORK_SLOT, "work")
    _ledger, none = handler._read_slot_fold_labelled("s-delta", "ledger")
    assert none is None, "a fold without deltas carries no label"


def test_metrics_subscribes_to_session_folds_only_so_never_sees_a_work_delta():
    from kiro_crew.metrics import crew_log_export

    seen: list[Any] = []
    bus.subscribe(bus.FOLD_ADVANCED, seen.append, scope=bus.SCOPE_SESSION)
    bus.publish(bus.FOLD_ADVANCED, _delta(5, 4))
    assert seen == []
    source = pathlib.Path(crew_log_export.__file__).read_text()
    assert "scope=crew_log_bus.SCOPE_SESSION" in source


# --------------------------------------------------------------------------- #
# 6. the hub: who gets which frame
# --------------------------------------------------------------------------- #


def test_the_hub_sends_slot_fold_only_to_watching_sockets_that_apply_deltas(tmp_path):
    from test_fold_watch_routing import _FakeWS, _make_state

    from kiro_crew.dashboard import websocket_hub as hub

    state = _make_state(tmp_path)

    def _open(*, watch: bool, apply: bool, capable: bool = True) -> _FakeWS:
        ws = _FakeWS()
        state.register_ws(ws, owner=True)
        state.set_fold_delta_capable(ws, capable)
        assert state.set_fold_apply_capable(ws, apply) is (apply and capable)
        if watch:
            state.set_fold_watch(ws, ["A"])
        return ws

    applies = _open(watch=True, apply=True)
    watches = _open(watch=True, apply=False)
    unwatched = _open(watch=False, apply=True)
    legacy = _open(watch=False, apply=False, capable=False)
    assert hub.fold_hello_payload(applies) == {"v": 1, "folds": ["work"], "delta": ["work"]}
    assert hub.fold_hello_payload(watches) == {"v": 1, "folds": ["work"]}
    assert hub.fold_hello_payload() == {"v": 1, "folds": ["work"]}

    legacy_data = {"slot": "A", "fold": "work", "revision": 7, "value": {"items": []}}
    delta_data = {"v": 1, "slot": "A", "fold": "work", "kind": "delta", "revision": 7}

    async def _run() -> None:
        state.broadcast_slot_fold("A", legacy_data, delta_data)
        for _ in range(6):
            await asyncio.sleep(0)

    asyncio.run(_run())
    assert applies.frames(hub.SLOT_DELTA_FRAME) == [delta_data]
    assert applies.frames(hub.SLOT_FOLD_FRAME) == []
    assert watches.frames(hub.SLOT_FOLD_FRAME) == [legacy_data]
    assert watches.frames(hub.SLOT_DELTA_FRAME) == []
    assert unwatched.sent == [], "a capable socket that watches nothing gets nothing"
    assert legacy.frames(hub.SLOT_FOLD_FRAME) == [legacy_data]


# --------------------------------------------------------------------------- #
# 7. the delta base is held only for boards a delta-applying tab watches
# --------------------------------------------------------------------------- #


def test_an_unwatched_board_holds_no_base_and_publishes_the_plain_value():
    writer = _Writer()
    seen: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, seen.append, scope="slot", key=WORK_SLOT, fold="work")
    bus.set_delta_boards(frozenset({"some-other-board"}))
    writer.create("it_a")
    _publish_now()
    writer.create("it_b")
    _publish_now()
    assert [e.kind for e in seen] == [fold_delta.KIND_VALUE] * 2
    assert all(e.delta is None and e.base_revision == 0 for e in seen)
    assert eager.published_revision(WORK_SLOT, "work") == 0, "no render is held"
    assert seen[-1].value == crew_log.read_slot_projection(WORK_SLOT, "work").value


def test_a_board_that_gains_a_watcher_starts_from_a_snapshot_then_chains():
    writer = _Writer()
    seen: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, seen.append, scope="slot", key=WORK_SLOT, fold="work")
    writer.create("it_a")
    _publish_now()
    assert eager.published_revision(WORK_SLOT, "work") > 0
    bus.set_delta_boards(frozenset())
    writer.create("it_b")
    _publish_now()
    assert seen[-1].kind == fold_delta.KIND_VALUE
    assert eager.published_revision(WORK_SLOT, "work") == 0, "losing the watcher drops the base"
    bus.set_delta_boards(frozenset({WORK_SLOT}))
    writer.create("it_c")
    _publish_now()
    writer.create("it_d")
    _publish_now()
    assert seen[-2].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert seen[-2].delta == {"reason": fold_delta.REASON_NO_BASE}
    assert seen[-1].kind == fold_delta.KIND_DELTA
    assert seen[-1].base_revision == seen[-2].revision


def test_the_exporter_narrows_delta_boards_to_what_applying_tabs_watch(tmp_path, monkeypatch):
    from test_fold_watch_routing import _FakeWS, _make_state

    from kiro_crew.crew_log import session_tree_projection
    from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher

    tree = {"A": ("A1",), "A1": (), "B": ()}
    monkeypatch.setattr(session_tree_projection, "created_children", lambda s: tree.get(s, ()))
    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: type("P", (), {"seeded_for_current_store": True})(),
    )
    state = _make_state(tmp_path)
    publisher = CrewLogPublisher(state)

    def _open(watch: str, *, apply: bool) -> _FakeWS:
        ws = _FakeWS()
        state.register_ws(ws, owner=True)
        state.set_fold_delta_capable(ws, True)
        state.set_fold_apply_capable(ws, apply)
        state.set_fold_watch(ws, [watch])
        return ws

    async def _run() -> None:
        publisher.bind(asyncio.get_running_loop(), state)
        publisher.subscribe_bus()
        assert not bus.delta_wanted("A"), "no tab applies deltas: no base anywhere"
        applies = _open("A", apply=True)
        _open("B", apply=False)
        assert bus.delta_wanted("A") and bus.delta_wanted("A1")
        assert bus.delta_wanted("dashboard:A")
        assert not bus.delta_wanted("B"), "a tab that cannot apply deltas needs no base"
        state.set_fold_watch(applies, [])
        assert not bus.delta_wanted("A")
        publisher.unsubscribe_bus()
        assert bus.delta_wanted("B"), "without the exporter every board holds a base"

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# 8. a snapshot taken while a publish is in flight
# --------------------------------------------------------------------------- #


def _late_publish_interleaving() -> "tuple[list[bus.FoldAdvanced], Any]":
    """The fold worker reads R1, an append moves the cell to R, a baseline subscriber
    reads, and only then does the worker publish P -> R1."""
    writer = _Writer()
    writer.create("it_a")
    _publish_now()  # P
    writer.create("it_b")
    in_flight = crew_log.read_slot_projection(WORK_SLOT, "work")  # the worker's R1
    writer.create("it_c")  # the cell moves to R on the next read
    seen: list[bus.FoldAdvanced] = []
    bus.subscribe(
        bus.FOLD_ADVANCED, seen.append, scope="slot", key=WORK_SLOT, fold="work", baseline=True
    )
    eager._publish_fold(WORK_SLOT, "work", in_flight)  # P -> R1, late
    return seen, crew_log.read_slot_projection(WORK_SLOT, "work")


def _held_after(events: "list[bus.FoldAdvanced]") -> "tuple[Any, int]":
    held: Any = None
    revision = 0
    for event in events:
        value = fold_delta.value_after(held, revision, event)
        if value is None:
            continue
        held, revision = value, event.revision
    return held, revision


def test_a_baseline_taken_during_a_late_publish_ends_at_the_current_board():
    seen, current = _late_publish_interleaving()
    held, revision = _held_after(seen)
    ids = [item["item_id"] for item in held["items"]]
    assert ids == [item["item_id"] for item in current.value["items"]], ids
    assert "it_c" in ids, "the newer item must not be lost to a stale delta"
    assert seen[0].revision == current.revision, "the baseline is labelled with its own render"


def test_a_late_publish_after_a_snapshot_is_never_a_delta_on_the_snapshot():
    seen, current = _late_publish_interleaving()
    for event in seen[1:]:
        assert not (
            event.kind == fold_delta.KIND_DELTA
            and event.base_revision == seen[0].revision
            and event.revision <= current.revision
            and event.value != current.value
        )
    assert eager.published_revision(WORK_SLOT, "work") == current.revision


def test_the_rest_snapshot_label_is_the_revision_of_its_own_value(monkeypatch):
    from kiro_crew.dashboard.handlers import crew_log as handler

    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    writer.create("it_b")  # read below folds past the last publish
    monkeypatch.setattr(handler._crew_log(), "slot_of_session", lambda _unit: WORK_SLOT)
    result, published = handler._read_slot_fold_labelled("s-delta", "work")
    assert published == result.revision
    assert eager.published_revision(WORK_SLOT, "work") == result.revision
