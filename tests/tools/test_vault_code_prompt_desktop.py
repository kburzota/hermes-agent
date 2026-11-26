"""enter_code must reach the user in a Desktop chat (no can_prompt_here patching, real wiring, tool worker thread)."""
import json
import threading
from unittest.mock import patch

import pytest

from agent.vault_backends import unlock as unlock_mod
from tools import browser_vault_tool
from tools.thread_context import propagate_context_to_thread

_BOX = [{"index": 0, "type": "text", "name": "otp", "label": "Code", "autocomplete": "one-time-code"}]


def _enter_code_in_worker():
    out = []

    def run():
        with patch.object(browser_vault_tool, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(browser_vault_tool, "_eval_js", side_effect=lambda t, e: {
                 "success": True, "result": json.dumps(_BOX) if "querySelectorAll" in e else "https://acme.test/2fa"}), \
             patch.object(browser_vault_tool, "_eval_js_secret", return_value={"success": True, "result": json.dumps({"filled": 1})}):
            out.append(json.loads(browser_vault_tool.browser_vault_enter_code(task_id="t")))

    t = threading.Thread(target=propagate_context_to_thread(run))
    t.start()
    t.join()
    return out[0]


@pytest.fixture(autouse=True)
def _clean():
    yield
    for setter in (unlock_mod.set_code_prompt_callback, unlock_mod.set_unlock_prompt_callback,
                   unlock_mod.set_save_login_prompt_callback):
        setter(None)


def test_code_prompt_alone_is_enough_to_ask_the_user():
    """Only the code callback is installed (the unlock prompt is for password managers): enter_code must still ask."""
    unlock_mod.set_code_prompt_callback(lambda site, hint: "123456")
    assert _enter_code_in_worker()["success"] is True


def test_headless_still_refuses():
    out = _enter_code_in_worker()
    assert out["error_type"] == "prompt_unavailable"
