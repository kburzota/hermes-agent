"""Re-key a local receipt a host gateway minted for a profile that now runs its own gateway."""
import json
from pathlib import Path

from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError, _epoch, _json


def _rekeyed(doc, new):
    """*doc* (an entry or origin-bearing dict) under route *new*, its served profile dropped."""
    doc = json.loads(json.dumps(doc))
    if 'session_key' in doc:
        doc['session_key'] = new
    origin = doc.get('origin', doc)
    origin.pop('profile', None)
    return doc


def adopt_host_namespace(authority, sid, receipt, route):
    """Rewrite *receipt* from ``agent:<own profile>:`` to *route* (``agent:main:``) and return it.

    A profile first served by the host gateway keys its local sessions ``agent:<name>:...``; once it
    runs its own gateway (its launch profile) the same DM is ``agent:main:...`` and every restore
    refused with ``route mismatch`` (``storage_unavailable``). Only that exact rename is accepted:
    same home, same profile in the origin, same remainder of the key. The receipt, the sessions row
    and the routing entry move in one owner transaction; nothing else changes.
    """
    name = Path(receipt['profile_id']).name
    old = route.replace('agent:main:', f'agent:{name}:', 1)
    if (receipt['route'] != old or old == route
            or receipt['entry']['origin'].get('profile') != name):
        return receipt
    new_receipt = {**receipt, 'route': route, 'entry': _rekeyed(receipt['entry'], route)}
    key = POLICY_PREFIX + sid

    def write(conn):
        _epoch(conn, authority.epoch)
        stored = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if stored is None or json.loads(stored[0]).get('route') != old:
            raise RuntimeStoreError('storage_unavailable')
        row = conn.execute('SELECT origin_json FROM sessions WHERE id=? AND session_key=?', (sid, old)).fetchone()
        if row is None:
            raise RuntimeStoreError('storage_unavailable')
        conn.execute('UPDATE sessions SET session_key=?, origin_json=? WHERE id=?',
                     (route, _json(_rekeyed(json.loads(row[0]), route)), sid))
        routed = conn.execute("SELECT entry_json FROM gateway_routing WHERE scope='' AND session_key=?",
                              (old,)).fetchone()
        if routed is not None:
            conn.execute("UPDATE gateway_routing SET session_key=?, entry_json=? WHERE scope='' AND session_key=?",
                         (route, _json(_rekeyed(json.loads(routed[0]), route)), old))
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(new_receipt), key))

    authority.db._execute_write(write)
    return new_receipt
