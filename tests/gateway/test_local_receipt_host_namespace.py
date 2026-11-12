"""A profile moved from the host gateway to its own gateway keeps its local sessions."""
import json
from pathlib import Path

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


def _compress(db, sid):
    """One compression step: a tip row under the same route; receipt and routing entry follow it."""
    from hermes_state_local import POLICY_PREFIX, local_receipt
    receipt = local_receipt(db, sid)
    tip = sid + '-tip'
    receipt['lineage'] = [sid, tip]
    receipt['entry']['session_id'] = tip

    def write(conn):
        conn.execute("UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id=?", (sid,))
        conn.execute("INSERT INTO sessions (id, parent_session_id, source, started_at, session_key, origin_json, "
                     "chat_id, user_id, model_config) SELECT ?, id, source, 2, session_key, origin_json, chat_id, "
                     "user_id, model_config FROM sessions WHERE id=?", (tip, sid))
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + sid))
        conn.execute("UPDATE gateway_routing SET entry_json=? WHERE scope='' AND session_key=?",
                     (json.dumps(receipt['entry']), receipt['route']))
    db._execute_write(write)
    return tip


def _age(db, sid, name, origin_profile):
    """Rewrite one local session the way the host gateway left it: ``agent:<name>:`` everywhere."""
    from hermes_state_local import POLICY_PREFIX, local_receipt
    receipt = local_receipt(db, sid)
    new = receipt['route']
    old = new.replace('agent:main:', f'agent:{name}:', 1)
    receipt = json.loads(json.dumps(receipt).replace(new, old))
    receipt['entry']['origin']['profile'] = origin_profile

    def write(conn):
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + sid))
        conn.execute('UPDATE sessions SET session_key=?, origin_json=replace(origin_json,?,?) '
                     'WHERE id IN (SELECT value FROM json_each(?))',
                     (old, new, old, json.dumps(receipt.get('lineage', [sid]))))
        conn.execute("UPDATE gateway_routing SET session_key=?, entry_json=replace(entry_json,?,?) "
                     "WHERE scope='' AND session_key=?", (old, new, old, new))
    db._execute_write(write)
    return old


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['own', 'foreign', 'compressed', 'taken'])
async def test_launch_profile_adopts_its_host_namespace_receipt(tmp_path, monkeypatch, variant):
    from gateway import run
    from gateway.run import _profile_runtime_scope
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_contract import Principal
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import restore_local_session
    from hermes_state_local import local_receipt
    from hermes_state_runtime import RuntimeStoreError

    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'platform_toolsets': {'cli': []}})
    root, homes = _reserve_homes(tmp_path, monkeypatch, names=())
    process_ownership.reserve([home for _, home in homes])
    runner = _runner(root, homes)
    try:
        await initialize_gateway_runtime(runner)
        authority = runner.session_authorities.launch
        actor = Principal('owner', authority.profile_id,
                          frozenset({'session:create', 'session:read', 'session:control'}), 'socket')
        with _profile_runtime_scope(root, {}):
            ref = create_local_session(authority, actor, {'request_id': 'bot-chat', 'source': 'gui',
                'cwd': str(tmp_path), 'model': 'frozen', 'toolsets': []})
        tip = _compress(authority.db, ref.session_id) if variant == 'compressed' else None
        current = local_receipt(authority.db, ref.session_id)['route']
        name = Path(authority.profile_id).name
        old = _age(authority.db, ref.session_id, name, 'beta' if variant == 'foreign' else name)
        authority.sessions.pop(ref.session_id, None)

        if variant == 'taken':  # a stray route already sits under the new key
            def squat(conn):
                conn.execute("INSERT INTO gateway_routing (scope, session_key, entry_json) "
                             "SELECT '', ?, entry_json FROM gateway_routing WHERE scope='' AND session_key=?",
                             (current, old))
            authority.db._execute_write(squat)
        if variant in ('foreign', 'taken'):
            with pytest.raises(RuntimeStoreError):
                restore_local_session(authority, ref.session_id)
            assert local_receipt(authority.db, ref.session_id)['route'] == old
            return
        assert restore_local_session(authority, ref.session_id) == ref
        receipt = local_receipt(authority.db, ref.session_id)
        assert receipt['route'] == receipt['entry']['session_key'] == current
        assert 'profile' not in receipt['entry']['origin']
        with authority.db._read_ctx() as conn:
            assert conn.execute('SELECT session_key FROM sessions WHERE id=?', (ref.session_id,)).fetchone()[0] == current
            keys = [r[0] for r in conn.execute("SELECT session_key FROM gateway_routing WHERE scope=''")]
        assert current in keys and old not in keys
        if tip:
            with authority.db._read_ctx() as conn:
                tip_row = conn.execute('SELECT session_key, origin_json FROM sessions WHERE id=?', (tip,)).fetchone()
            assert tip_row[0] == current and old not in tip_row[1]
        assert restore_local_session(authority, ref.session_id) == ref  # idempotent once healed
    finally:
        for owner in getattr(runner, 'session_authorities', ()):
            owner.db.close()
        for _, home in homes:
            process_ownership.release(home)
