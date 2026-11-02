"""``list_profiles`` / ``_check_gateway_running`` must reuse the dashboard PID cache.

Desktop polls ``GET /api/profiles`` every few seconds for every profile. The
probe used to call ``get_running_pid`` uncached (and never unlink another
home's PID file). ``/api/status`` already had a 1s cache; the listing must
use that same cache so 43 profiles do not re-flock ``gateway.lock``.
"""

from __future__ import annotations

from pathlib import Path

import gateway.status as gw_status
from hermes_cli import profiles


def test_check_gateway_running_reuses_the_pid_cache(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "gateway.pid").write_text("1\n", encoding="utf-8")
    gw_status._clear_running_pid_cache()

    calls = []

    def _pid(path=None, *, cleanup_stale=True):
        calls.append((Path(path) if path is not None else None, cleanup_stale))

    monkeypatch.setattr(gw_status, "get_running_pid", _pid)
    monkeypatch.setattr(gw_status, "get_runtime_status_running_pid", lambda *a, **k: None)
    monkeypatch.setattr(gw_status, "multiplexer_liveness_for_profile", lambda *_a, **_k: None)

    assert profiles._check_gateway_running(home) is False
    assert profiles._check_gateway_running(home) is False

    assert calls == [(home / "gateway.pid", False)]
    gw_status._clear_running_pid_cache()
