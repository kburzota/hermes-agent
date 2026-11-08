"""agent-kit#87: ``active_pr`` looks the PR up instead of failing closed on any URL,
and ``create_task(supersedes=...)`` replaces a stranded card in one step."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

URL = "https://github.com/acme/repo/pull/7"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def gh(monkeypatch):
    """Fake ``gh pr view``: set ``gh.pr`` to the dict to return or an exception to raise."""
    class Fake:
        pr: object = None
        calls = 0

        def __call__(self, url):
            self.calls += 1
            if isinstance(self.pr, BaseException):
                raise self.pr
            return self.pr
    fake = Fake()
    monkeypatch.setattr(kbd, "_gh_pr_view", fake)
    monkeypatch.setattr(kbd, "_pr_lookup_cache", {})  # self-contained: the re-apply check runs this file alone
    monkeypatch.setattr(kbd, "_active_pr_detail", {})
    return fake


def _card(conn, *, assignee="dev", author="dev", body=f"Opened {URL}"):
    tid = kb.create_task(conn, title="t", assignee=assignee)
    kb.add_comment(conn, tid, author=author, body=body)
    return tid


def _pr(state="OPEN", login="kburzota", head="dev/fix"):
    return {"state": state, "author": {"login": login}, "headRefName": head}


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_merged_or_closed_pr_does_not_guard(kanban_home, gh, state):
    gh.pr = _pr(state=state)
    with kbc.connect() as conn:
        assert kbd.check_respawn_guard(conn, _card(conn)) is None


def test_other_agents_open_pr_does_not_guard(kanban_home, gh):
    # Every agent's PR is authored by the shared gh login; the branch names the agent.
    gh.pr = _pr(head="rick/other-work")
    with kbc.connect() as conn:
        assert kbd.check_respawn_guard(conn, _card(conn, author="default")) is None


@pytest.mark.parametrize("pr,author", [
    (_pr(head="dev/fix"), "default"),        # on the assignee's branch
    (_pr(head="qa/cut-a296f6e"), "dev"),      # linked by the assignee itself
    (_pr(head="fix", login="dev"), "default"),  # authored by the assignee
])
def test_owners_open_pr_still_guards_with_detail(kanban_home, gh, all_assignees_spawnable, pr, author):
    gh.pr = pr
    with kbc.connect() as conn:
        tid = _card(conn, author=author)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        res = kbd.dispatch_once(conn, dry_run=False)
        assert dict(res.respawn_guarded).get(tid) == "active_pr"
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "respawn_guarded"][-1]
        assert ev.payload == {"reason": "active_pr", "detail": f"open PR owned by this card: {URL}"}


def test_pr_on_the_cards_task_id_branch_guards(kanban_home, gh):
    with kbc.connect() as conn:
        tid = _card(conn, author="default")
        gh.pr = _pr(head=f"baseline/{tid}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_gh_failure_stays_guarded_and_says_why(kanban_home, gh, all_assignees_spawnable):
    gh.pr = subprocess.CalledProcessError(1, "gh", stderr="HTTP 401: Bad credentials\n")
    with kbc.connect() as conn:
        tid = _card(conn)
        kbd.dispatch_once(conn, dry_run=False)
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "respawn_guarded"][-1]
        assert ev.payload == {"reason": "active_pr",
                              "detail": f"gh lookup failed for {URL}: HTTP 401: Bad credentials"}
    gh.pr = subprocess.TimeoutExpired("gh", 15)
    kbd._pr_lookup_cache.clear()
    with kbc.connect() as conn:
        assert kbd.check_respawn_guard(conn, _card(conn)) == "active_pr"


def test_lookup_is_cached(kanban_home, gh):
    gh.pr = _pr()
    with kbc.connect() as conn:
        tid = _card(conn)
        for _ in range(3):
            assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    assert gh.calls == 1


def test_unblock_after_the_pr_comment_lifts_the_guard(kanban_home, gh):
    """Nov 7: both stranded cards were unblocked with their own PR still open."""
    gh.pr = _pr()
    with kbc.connect() as conn:
        tid = _card(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = created_at - 60 WHERE task_id = ?", (tid,))
        assert kb.block_task(conn, tid, reason="clone timed out")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        assert kb.unblock_task(conn, tid)
        assert kbd.check_respawn_guard(conn, tid) is None
        # A newer PR comment (the resumed run's own) guards again.
        kb.add_comment(conn, tid, author="dev", body=f"Pushed {URL}")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_supersedes_moves_dependents_and_archives_old(kanban_home):
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", assignee="dev")
        child = kb.create_task(conn, title="child", assignee="qa", parents=[old])
        assert kb.block_task(conn, old, reason="stuck")
        new = kb.create_task(conn, title="new", assignee="dev", supersedes=old)

        assert kb.get_task(conn, old).status == "archived"
        assert kb.parent_ids(conn, child) == [new]
        assert kb.get_task(conn, child).status == "todo"  # still waits, now on the new card
        archived = [e for e in kb.list_events(conn, old) if e.kind == "archived"][-1]
        assert archived.payload == {"superseded_by": new, "moved_children": [child]}
        assert [e.payload for e in kb.list_events(conn, new) if e.kind == "supersedes"] == [
            {"task": old, "moved_children": [child]}]

        kb.complete_task(conn, new, summary="done")
        assert kb.get_task(conn, child).status == "ready"


@pytest.mark.parametrize("old_status", ["running", "done", "archived"])
def test_supersedes_refuses_and_creates_nothing(kanban_home, old_status):
    with kbc.connect() as conn:
        old = kb.create_task(conn, title="old", assignee="dev")
        child = kb.create_task(conn, title="child", assignee="qa", parents=[old])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (old_status, old))
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match=f"it is {old_status}"):
            kb.create_task(conn, title="new", assignee="dev", supersedes=old)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
        assert kb.parent_ids(conn, child) == [old]


def test_supersedes_unknown_card_creates_nothing(kanban_home):
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match="unknown task"):
            kb.create_task(conn, title="new", assignee="dev", supersedes="t_nope")
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
