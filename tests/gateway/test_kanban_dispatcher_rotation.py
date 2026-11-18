"""tick_once rotates the board start order so no board is starved by the host cap."""

from gateway import kanban_watchers_dispatcher as kwd


def test_tick_once_rotates_start_board(monkeypatch):
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    d = kwd._KanbanDispatcher(object(), settings)
    monkeypatch.setattr(d, "_board_slugs", lambda: ["a", "b", "c"])
    monkeypatch.setattr(d, "tick_once_for_board", lambda slug, probe_ready=False: None)
    firsts = [d.tick_once()[0][0] for _ in range(4)]
    assert firsts == ["a", "b", "c", "a"]
    assert [s for s, _ in d.tick_once()] == ["b", "c", "a"]
