"""Deletion must reach the same physical stores as the real readers/writers."""
import json
import sqlite3

import pytest

from test_f04_auth_boundaries import api as api
from test_f04_recovery import world as world


@pytest.fixture
def derived(world, tmp_path, monkeypatch):
    from ducky import utils
    from ducky.hot import legacy_helpers as legacy
    paths = {'facts': utils.FACTS_DB,
             'observations': str(tmp_path / 'observations.db'),
             'scenes': str(tmp_path / 'scenes.db')}
    for module in (utils, legacy):
        monkeypatch.setattr(module, 'OBS_DB', paths['observations'])
        monkeypatch.setattr(module, 'SCENES_DB', paths['scenes'])
    # Both real stores and historical co-located tables must remain covered.
    for name, path in paths.items():
        with sqlite3.connect(path) as conn:
            if name in ('facts', 'observations'):
                legacy._ensure_observations_table(conn)
                for owner in ('alice', 'bob', ''):
                    conn.execute('INSERT INTO observations(category,summary,content,user_id) VALUES(?,?,?,?)',
                                 ('general', owner + '-summary', owner + '-body', owner))
            if name in ('facts', 'scenes'):
                legacy._ensure_scenes_table(conn)
                for owner, bank in [('alice', 'work'), ('alice', 'private'), ('bob', 'work')]:
                    conn.execute('INSERT INTO scenes(category,summary,member_keys,user_id,bank_id) VALUES(?,?,?,?,?)',
                                 ('general', owner + '-summary', 'key1|key2', owner, bank))
    return paths


def test_delete_all_cleans_real_and_legacy_stores_without_cross_scope_loss(derived):
    from ducky.wal_engine import cascade_delete_all
    result = cascade_delete_all('alice', bank_id='work', confirm=True)
    assert result['status'] == 'committed', result
    assert result['details']['scenes_deleted'] == 2
    assert result['details']['observations_deleted'] == 2
    for name, path in derived.items():
        with sqlite3.connect(path) as conn:
            if name in ('facts', 'scenes'):
                assert set(conn.execute('SELECT user_id,bank_id FROM scenes')) == {('alice', 'private'), ('bob', 'work')}
            if name in ('facts', 'observations'):
                assert set(conn.execute('SELECT user_id,content FROM observations')) == {('bob', 'bob-body'), ('', '-body')}


@pytest.mark.parametrize('table', ['scenes', 'observations'])
def test_real_store_failure_keeps_repair_debt_and_retries(derived, table):
    from ducky.wal_engine import cascade_delete_all
    with sqlite3.connect(derived[table]) as conn:
        conn.execute(f"CREATE TRIGGER block_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    result = cascade_delete_all('alice', bank_id='work', confirm=True)
    assert result['status'] == 'partial', result
    assert table in {f['layer'] for f in result['failed_layers']}
    from ducky.wal_engine import WALEngine
    assert WALEngine.get_instance().get_pending_entries()
    with sqlite3.connect(derived[table]) as conn:
        assert conn.execute(f'SELECT COUNT(*) FROM {table} WHERE user_id=?', ('alice',)).fetchone()[0] > 0
        conn.execute('DROP TRIGGER block_delete')
    result = cascade_delete_all('alice', bank_id='work', confirm=True)
    assert result['status'] == 'committed', result
    with sqlite3.connect(derived[table]) as conn:
        where = "user_id='alice'" + (" AND bank_id='work'" if table == 'scenes' else '')
        assert conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}').fetchone()[0] == 0


@pytest.mark.parametrize('limit', [1, 2, 4])
def test_mcp_facts_limit_reaches_real_http_reader(api, monkeypatch, limit):
    import mcp_server
    client, _, connect = api
    with connect() as conn:
        for owner, bank in [('alice', 'work'), ('alice', 'private'), ('bob', 'work')]:
            for n in range(5):
                conn.execute('INSERT INTO facts(category,fact_key,fact_value,user_id,bank_id,trust_score) VALUES(?,?,?,?,?,?)',
                             ('general', 'limit-probe-' + str(n), owner + '-' + bank, owner, bank, .8))
    def actual_get(path, params):
        result = client.get(path, params={**params, 'caller_user_id': 'alice'})
        assert result.status_code == 200, result.text
        return result.json()
    monkeypatch.setattr(mcp_server, '_api_get', actual_get)
    result = json.loads(mcp_server.facts_search('limit-probe', limit=limit, user_id='alice', bank_id='work'))
    assert result['count'] == limit
    assert len(result['facts']) == limit
    assert all(r['fact_value'] == 'alice-work' for r in result['facts'])
