"""Deletion must reach the same physical stores as the real readers/writers."""
import sqlite3

import pytest

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
                # f0.6+++ WP2：/observe 写入端已移除，建表器随之删除；删除链仍须
                # 清掉磁盘上的历史 observations 行，这里按存量 schema 手工建表。
                conn.execute('CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                             "category TEXT NOT NULL DEFAULT 'general', summary TEXT NOT NULL, "
                             "content TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '')")
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


