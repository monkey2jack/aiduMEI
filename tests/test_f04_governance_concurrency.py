"""用真实 SQLite 交错提交，覆盖慢评估器与人审/事实更新的竞争。"""
import pytest

import ducky.governance as gov
import ducky.utils as utils


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, 'FACTS_DB', str(tmp_path / 'facts.db'))
    monkeypatch.setattr(utils, 'TEXT_FTS_DB', str(tmp_path / 'fts.db'))
    conn = utils.get_facts_conn()
    conn.execute('''CREATE TABLE facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, fact_key TEXT,
        fact_value TEXT, user_id TEXT, bank_id TEXT, source TEXT,
        trust_score REAL DEFAULT 0.5, archived INTEGER DEFAULT 0,
        archived_at TEXT)''')
    conn.execute("INSERT INTO facts(category,fact_key,fact_value,user_id,bank_id,source) "
                 "VALUES('preference','coffee','喜欢热拿铁','alice','work','writer')")
    conn.commit()
    gov.ensure_governance_schema()
    from ducky.tombstone import ensure_tombstone_schema
    ensure_tombstone_schema()
    result = gov.govern_fact_write(conn, 1, 'preference', 'coffee', '喜欢热拿铁', 'writer')
    conn.commit()
    yield conn, result['candidate_id']
    conn.close()


@pytest.mark.parametrize('verdict', ['reject', 'approve', None])
@pytest.mark.parametrize('human', ['approve', 'reject'])
def test_late_evaluator_cannot_overwrite_human_decision(store, verdict, human):
    conn, cid = store
    expected = 'committed' if human == 'approve' else 'rejected'

    def slow_evaluator(*args):
        decided = gov.review_candidate(cid, human, 'human wins', user_id='alice', bank_id='work')
        assert decided['status'] == expected
        return None if verdict is None else {'verdict': verdict, 'confidence': .99, 'reason': 'late model'}

    result = gov.evaluate_candidate(cid, evaluator=slow_evaluator)
    assert result['status'] == expected
    assert result['route'] == 'already_decided'
    candidate = conn.execute('SELECT * FROM candidate_facts WHERE candidate_id=?', (cid,)).fetchone()
    assert candidate['status'] == expected
    assert candidate['review_reason'] == 'human wins'
    assert candidate['eval_reason'] == ''
    assert conn.execute('SELECT archived FROM facts WHERE id=1').fetchone()[0] == (human == 'reject')
    assert conn.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0] == (human == 'reject')
    assert not conn.in_transaction


@pytest.mark.parametrize('change', ['new_value', 'deleted', 'new_candidate_same_value', 'new_scope'])
@pytest.mark.parametrize('route', ['evaluator', 'human'])
def test_stale_candidate_cannot_modify_replaced_fact(store, change, route):
    conn, cid = store

    def change_fact():
        if change == 'new_value':
            conn.execute("UPDATE facts SET fact_value='现在喜欢绿茶',trust_score=.7 WHERE id=1")
        elif change == 'deleted':
            conn.execute('DELETE FROM facts WHERE id=1')
        elif change == 'new_scope':
            conn.execute("UPDATE facts SET user_id='bob',bank_id='home',trust_score=.7 WHERE id=1")
        else:
            gov.govern_fact_write(conn, 1, 'preference', 'coffee', '喜欢热拿铁', 'writer')
        conn.commit()

    if route == 'human':
        change_fact()
        result = gov.review_candidate(cid, 'reject', user_id='alice', bank_id='work')
    else:
        def slow_evaluator(*args):
            change_fact()
            return {'verdict': 'reject', 'confidence': .99, 'reason': 'stale'}
        result = gov.evaluate_candidate(cid, evaluator=slow_evaluator)
    assert result['status'] == 'superseded'
    candidate = conn.execute('SELECT * FROM candidate_facts WHERE candidate_id=?', (cid,)).fetchone()
    assert candidate['status'] == 'superseded'
    assert conn.execute('SELECT COUNT(*) FROM facts WHERE archived=1').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0] == 0
    assert not conn.in_transaction


def test_two_evaluators_cannot_issue_conflicting_decisions(store):
    conn, cid = store

    def late(*args):
        result = gov.evaluate_candidate(cid, evaluator=lambda *a: {
            'verdict': 'approve', 'confidence': .99, 'reason': 'first'})
        assert result['status'] == 'committed'
        return {'verdict': 'reject', 'confidence': .99, 'reason': 'late'}

    result = gov.evaluate_candidate(cid, evaluator=late)
    assert result['status'] == 'committed'
    assert result['route'] == 'already_decided'
    assert conn.execute('SELECT archived FROM facts WHERE id=1').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0] == 0
    assert not conn.in_transaction


def test_failed_decision_rolls_back_fact_snapshot_and_candidate(store):
    conn, cid = store
    conn.execute("""CREATE TRIGGER stop_decision BEFORE UPDATE OF status ON candidate_facts
        WHEN NEW.status='rejected' BEGIN SELECT RAISE(ABORT, 'isolated commit failure'); END""")
    conn.commit()
    result = gov.evaluate_candidate(cid, evaluator=lambda *a: {
        'verdict': 'reject', 'confidence': .99, 'reason': 'synthetic'})
    assert result['route'] == 'error'
    assert not conn.in_transaction
    assert conn.execute('SELECT archived FROM facts WHERE id=1').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM tombstones').fetchone()[0] == 0
    row = conn.execute('SELECT * FROM candidate_facts WHERE candidate_id=?', (cid,)).fetchone()
    assert row['status'] == 'pending' and row['eval_verdict'] == ''


@pytest.mark.parametrize('initializer', ['governance', 'tombstone', 'bank', 'ledger'])
def test_lazy_schema_does_not_commit_callers_transaction(store, initializer):
    from ducky.tombstone import ensure_tombstone_schema
    from ducky.bank_contract import ensure_memory_banks_schema
    from ducky.event_ledger import ensure_ledger_schema
    conn, _ = store
    conn.execute("UPDATE facts SET fact_value='未提交修改' WHERE id=1")
    {'governance': gov.ensure_governance_schema, 'tombstone': ensure_tombstone_schema,
     'bank': ensure_memory_banks_schema, 'ledger': ensure_ledger_schema}[initializer]()
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute('SELECT fact_value FROM facts WHERE id=1').fetchone()[0] == '喜欢热拿铁'


def test_human_and_slow_evaluator_on_separate_connections(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    conn, cid = store
    entered, released = Event(), Event()
    def evaluator(*args):
        entered.set()
        assert released.wait(5)
        return {'verdict': 'reject', 'confidence': .99, 'reason': 'late'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gov.evaluate_candidate, cid, evaluator)
        try:
            assert entered.wait(5)
            assert gov.review_candidate(cid, 'approve', user_id='alice', bank_id='work')['status'] == 'committed'
        finally:
            released.set()
        assert future.result(timeout=5)['route'] == 'already_decided'
    assert conn.execute('SELECT archived FROM facts WHERE id=1').fetchone()[0] == 0
