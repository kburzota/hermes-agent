"""A profile moved from the host gateway to its own gateway keeps its local sessions."""
import json
from pathlib import Path

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner


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
        conn.execute('UPDATE sessions SET session_key=? WHERE id=?', (old, sid))
        conn.execute("UPDATE gateway_routing SET session_key=?, entry_json=replace(entry_json,?,?) "
                     "WHERE scope='' AND session_key=?", (old, new, old, new))
    db._execute_write(write)
    return old


@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['own', 'foreign'])
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
        current = local_receipt(authority.db, ref.session_id)['route']
        name = Path(authority.profile_id).name
        old = _age(authority.db, ref.session_id, name, name if variant == 'own' else 'beta')
        authority.sessions.pop(ref.session_id, None)

        if variant == 'foreign':
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
        assert restore_local_session(authority, ref.session_id) == ref  # idempotent once healed
    finally:
        for owner in getattr(runner, 'session_authorities', ()):
            owner.db.close()
        for _, home in homes:
            process_ownership.release(home)
