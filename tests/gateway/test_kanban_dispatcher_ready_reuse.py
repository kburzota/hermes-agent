"""The embedded dispatcher probes spawnable-ready on the tick connection."""

from __future__ import annotations

from pathlib import Path

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def test_ready_nonempty_after_tick_does_not_open_another_connection(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    opens = {"n": 0}
    real = kbc.connect

    def counting(*args, **kwargs):
        opens["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(kbc, "connect", counting)
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    dispatcher = kwd._KanbanDispatcher(kb, settings)
    dispatcher.tick_once()
    after_tick = opens["n"]
    assert after_tick >= 1
    assert dispatcher.ready_nonempty() is False
    assert opens["n"] == after_tick

    standalone = kwd._KanbanDispatcher(kb, settings)
    standalone.ready_nonempty()
    assert opens["n"] > after_tick
