"""Park-on-loop: dispatcher must not reclaim a card after ``block_loop_detected``.

Production loop (2026-11-02): ``block_task`` routed the breaker to ``triage``,
auto-decompose specified the card back to ``ready``, and the dispatcher
respawned it about once a minute. Default policy parks as sticky ``blocked``
until an explicit unblock; an optional named-agent support card is minted
once. Transient and single blocks stay on the old path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_block_loop as loop
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="deploy", assignee="worker"):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer=assignee) is not None
    return tid


def _make_running_again(conn, tid, claimer="worker"):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer=claimer) is not None


def _drive_to_loop(conn, tid, *, reason="need pr-agent to rebase PR #9", kind="capability"):
    assert kb.block_task(conn, tid, reason=reason, kind=kind)
    assert kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    assert kb.block_task(conn, tid, reason=reason, kind=kind)
    return kb.get_task(conn, tid)


def _park_comments(conn, tid):
    return [
        c for c in kb.list_comments(conn, tid)
        if (c.body or "").startswith(loop._PARK_COMMENT_PREFIX)
    ]


def test_park_block_loop_defaults_are_on() -> None:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    kanban = DEFAULT_CONFIG["kanban"]
    assert kanban.get("park_block_loop") is True
    assert kanban.get("block_loop_unblock_card") is True


def test_named_unblock_assignee_requires_explicit_mention() -> None:
    known = ["pr-agent", "alice", "test"]
    assert loop.named_unblock_assignee(
        "Need @pr-agent to rebase PR #9", known_profiles=known, exclude="worker",
    ) == "pr-agent"
    assert loop.named_unblock_assignee(
        "profile pr-agent should rebase", known_profiles=known,
    ) == "pr-agent"
    assert loop.named_unblock_assignee(
        "assignee: alice must approve", known_profiles=known,
    ) == "alice"
    assert loop.named_unblock_assignee(
        "assigned to alice before deploy", known_profiles=known,
    ) == "alice"
    # Bare words — including a profile named ``test`` in a slot wait — mint nothing.
    assert loop.named_unblock_assignee(
        "Need pr-agent to rebase PR #9", known_profiles=known,
    ) is None
    assert loop.named_unblock_assignee(
        "waiting for a test slot", known_profiles=known,
    ) is None
    assert loop.named_unblock_assignee("foo@alice.com", known_profiles=known) is None


def test_named_unblock_assignee_ambiguous_or_self() -> None:
    known = ["alice", "bob", "worker", "default"]
    assert loop.named_unblock_assignee("@alice and @bob must both act", known_profiles=known) is None
    assert loop.named_unblock_assignee("@worker is stuck", known_profiles=known, exclude="worker") is None
    assert loop.named_unblock_assignee("use the default profile", known_profiles=known) is None
    assert loop.named_unblock_assignee("Need @alice-ops", known_profiles=["alice"]) is None


def test_loop_parks_sticky_blocked_and_dispatch_does_not_reclaim(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _running_task(conn)
        parked = _drive_to_loop(conn, tid, reason="waiting on a rebase")
        assert parked.status == "blocked"
        assert parked.block_recurrences >= kb.BLOCK_RECURRENCE_LIMIT
        events = [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]
        assert events and events[-1].payload.get("parked") == "blocked"
        comments = _park_comments(conn, tid)
        assert comments, "loop park must leave an audit comment"
        assert "explicitly unblocked" in comments[0].body

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert tid not in spawns
        assert kb.get_task(conn, tid).status == "blocked"


def test_explicit_unblock_after_park_returns_to_dispatch_pool(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _running_task(conn)
        _drive_to_loop(conn, tid, reason="waiting on a rebase")
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status in ("ready", "todo")
        result = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        spawned_ids = [row[0] for row in result.spawned]
        assert tid in spawned_ids or tid in spawns


def test_transient_and_single_block_are_unchanged(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        transient = _running_task(conn, title="slot-wait")
        assert kb.block_task(conn, transient, reason="waiting for a free test slot", kind="transient")
        parked = kb.get_task(conn, transient)
        assert (parked.status, parked.block_kind, parked.block_recurrences) == ("blocked", "transient", 1)
        assert not [e for e in kb.list_events(conn, transient) if e.kind == "block_loop_detected"]
        assert not _park_comments(conn, transient)

        once = _running_task(conn, title="one-block")
        assert kb.block_task(conn, once, reason="need a key", kind="needs_input")
        one = kb.get_task(conn, once)
        assert (one.status, one.block_recurrences) == ("blocked", 1)
        assert not [e for e in kb.list_events(conn, once) if e.kind == "block_loop_detected"]


def test_named_agent_unblock_card_is_idempotent(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop, "_known_profile_names", lambda: {"pr-agent", "alice"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "pr-agent")
    reason = "Need @pr-agent to rebase PR #9 before deploy"

    with kbc.connect() as conn:
        tid = _running_task(conn)
        _drive_to_loop(conn, tid, reason=reason)
        key = f"{loop._UNBLOCK_KEY_PREFIX}{tid}"
        rows = conn.execute(
            "SELECT id, assignee FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
            (key,),
        ).fetchall()
        assert len(rows) == 1
        card_id = rows[0]["id"]
        assert rows[0]["assignee"] == "pr-agent"
        assert card_id in kb.parent_ids(conn, tid)
        assert tid not in kb.parent_ids(conn, card_id)
        comments = _park_comments(conn, tid)
        assert comments and card_id in comments[0].body

        # Support card is an open parent, so unblock lands in todo. Force a
        # re-block the same way the original loop re-entered ``block_task``.
        assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        assert kb.block_task(conn, tid, reason=reason, kind="capability")
        rows_again = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
            (key,),
        ).fetchall()
        assert [r["id"] for r in rows_again] == [card_id]
        assert len(_park_comments(conn, tid)) == 1


def test_completing_support_card_leaves_parked_card_blocked(
    kanban_home: Path, all_assignees_spawnable, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The support card finishing does not ready the parked card; only unblock does."""
    monkeypatch.setattr(loop, "_known_profile_names", lambda: {"pr-agent"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "pr-agent")
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = _running_task(conn)
        parked = _drive_to_loop(conn, tid, reason="Need @pr-agent to rebase PR #9")
        assert parked.status == "blocked"
        key = f"{loop._UNBLOCK_KEY_PREFIX}{tid}"
        card_id = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
            (key,),
        ).fetchone()["id"]
        assert kb.get_task(conn, card_id).status in ("ready", "todo")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (card_id,))
        assert kb.complete_task(conn, card_id, result="rebased")
        still = kb.get_task(conn, tid)
        assert still.status == "blocked"
        assert kb.recompute_ready(conn) == 0
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert tid not in spawns
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status in ("ready", "todo")


def test_common_word_profile_does_not_mint_unblock_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loop, "_known_profile_names", lambda: {"test"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "test")
    with kbc.connect() as conn:
        tid = _running_task(conn)
        _drive_to_loop(conn, tid, reason="waiting for a test slot", kind="transient")
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            (f"{loop._UNBLOCK_KEY_PREFIX}{tid}",),
        ).fetchall()
        assert rows == []
        assert kb.parent_ids(conn, tid) == []


def test_two_named_profiles_do_not_mint_unblock_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loop, "_known_profile_names", lambda: {"alice", "bob"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"alice", "bob"})
    with kbc.connect() as conn:
        tid = _running_task(conn)
        _drive_to_loop(conn, tid, reason="@alice and @bob must both rebase")
        kids = kb.child_ids(conn, tid)
        parents = kb.parent_ids(conn, tid)
        assert kids == []
        assert parents == []


def test_park_block_loop_false_lands_in_triage_and_skips_auto_decompose(
    kanban_home: Path,
) -> None:
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  park_block_loop: false\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        looping = _running_task(conn, title="legacy-loop")
        ordinary = kb.create_task(conn, title="new idea", triage=True)
        parked = _drive_to_loop(conn, looping, reason="waiting on a rebase")
        assert parked.status == "triage"
        loop_events = [e for e in kb.list_events(conn, looping) if e.kind == "block_loop_detected"]
        assert loop_events[-1].payload.get("parked") == "triage"
        assert looping not in decomp.list_triage_ids()
        assert looping not in spec.list_triage_ids()
        assert ordinary in decomp.list_triage_ids()
        assert ordinary in spec.list_triage_ids()


def test_block_loop_unblock_card_false_skips_support_card(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  block_loop_unblock_card: false\n", encoding="utf-8",
    )
    monkeypatch.setattr(loop, "_known_profile_names", lambda: {"pr-agent"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "pr-agent")
    with kbc.connect() as conn:
        tid = _running_task(conn)
        parked = _drive_to_loop(conn, tid, reason="Need @pr-agent to rebase")
        assert parked.status == "blocked"
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            (f"{loop._UNBLOCK_KEY_PREFIX}{tid}",),
        ).fetchall()
        assert rows == []
        assert _park_comments(conn, tid)


def test_cli_block_reports_park_on_loop(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    with kbc.connect() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="x", kind="capability")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None

    args = argparse.Namespace(task_id=tid, ids=None, reason=["x"], kind="capability")
    assert kanban_cli._cmd_block(args) == 0
    out = capsys.readouterr().out
    assert "unblock loop detected" in out
    assert "parked until explicit unblock" in out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_route_block_pure_function_park_vs_triage() -> None:
    _status, event, _sql, params, payload = loop.route_block(
        "capability", "x", "ready", prev_kind="capability", prev_recurrences=1,
        loop_landing="blocked",
    )
    assert (_status, event) == ("blocked", "block_loop_detected")
    assert payload["parked"] == "blocked"
    assert params[1] == kb.BLOCK_RECURRENCE_LIMIT

    status_t, event_t, _, _, payload_t = loop.route_block(
        "capability", "x", "ready", prev_kind="capability", prev_recurrences=1,
        loop_landing="triage",
    )
    assert (status_t, event_t, payload_t["parked"]) == ("triage", "block_loop_detected", "triage")

    status_once, event_once, _, params_once, _ = loop.route_block(
        "transient", "slot", "ready", prev_kind=None, prev_recurrences=0,
    )
    assert (status_once, event_once, params_once[1]) == ("blocked", "blocked", 1)
