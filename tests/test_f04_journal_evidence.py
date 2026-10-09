"""OPS-R1: real persisted debt must never disappear behind a healthy probe."""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from ducky import mutation_journal as journal
pytest_plugins = ["test_f04_recovery"]


def debt():
    jid = journal.accept_job({'user_id': 'alice', 'bank_id': 'work', 'messages': 'private debt'})
    journal.startup_recover()
    assert journal.journal_health()['repair_required'] == 1
    return jid


def test_uninitialized_health_is_read_only_and_unknown(world):
    path = journal.journal_path()
    assert not path.exists()
    before = set(path.parent.iterdir())
    report = journal.journal_health(deep=True)
    assert report['status'] == 'degraded'
    assert report['integrity'] == 'unknown'
    assert 'counts' not in report and 'repair_required' not in report
    assert set(path.parent.iterdir()) == before


@pytest.mark.parametrize('damage', ['unlink', 'drop_table', 'corrupt', 'replace', 'identity_table', 'marker'])
def test_lost_established_evidence_blocks_health_startup_and_writers(world, damage, tmp_path):
    debt()
    path = journal.journal_path()
    if damage == 'unlink':
        path.unlink()
    elif damage == 'corrupt':
        path.write_bytes(b'preserve these corrupted bytes')
    elif damage == 'marker':
        journal.journal_identity_path().unlink()
    elif damage == 'replace':
        replacement = tmp_path / 'replacement.sqlite3'
        with sqlite3.connect(replacement) as conn:
            conn.execute('CREATE TABLE innocent(body TEXT)')
        replacement.replace(path)
    else:
        with sqlite3.connect(path) as conn:
            conn.execute('DROP TABLE ' + ('mutations' if damage == 'drop_table' else 'journal_identity'))
    before = path.read_bytes() if path.exists() else None
    report = journal.journal_health(deep=True)
    assert report['status'] == 'degraded'
    assert report['integrity'] == 'unknown'
    assert 'repair_required' not in report and 'counts' not in report
    with pytest.raises((RuntimeError, sqlite3.Error, OSError)):
        journal.startup_recover()
    with pytest.raises((RuntimeError, sqlite3.Error, OSError)):
        journal.perform_mutation('mem0_add', 'alice', 'work', {}, lambda: pytest.fail('SDK dispatched'))
    assert (path.read_bytes() if path.exists() else None) == before


def test_read_only_health_does_not_change_healthy_database(world):
    debt()
    path = journal.journal_path()
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert journal.journal_health(deep=True)['repair_required'] == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_same_schema_replacement_has_wrong_identity(world):
    debt()
    with sqlite3.connect(journal.journal_path()) as conn:
        conn.execute("UPDATE journal_identity SET identity='different-established-store'")
    assert journal.journal_health()['integrity'] == 'unknown'
    with pytest.raises(RuntimeError):
        journal.startup_recover()


def test_explicit_legacy_adoption_preserves_debt_and_never_reinitializes(world):
    jid = debt()
    with sqlite3.connect(journal.journal_path()) as conn:
        conn.execute('DROP TABLE journal_identity')
    journal.journal_identity_path().unlink()
    original = journal.journal_path().read_bytes()
    assert journal.journal_health()['status'] == 'degraded'
    with pytest.raises(RuntimeError):
        journal.startup_recover()
    assert journal.journal_path().read_bytes() == original
    journal.initialize_journal(adopt_existing=True)
    assert journal.journal_health()['repair_required'] == 1
    assert journal.inspect_mutation(jid, 'alice', 'work', include_payload=True)['input']['messages'] == 'private debt'
    marker = json.loads(journal.journal_identity_path().read_text())
    journal.initialize_journal(adopt_existing=True)
    assert json.loads(journal.journal_identity_path().read_text()) == marker


@pytest.mark.parametrize('damage', ['unknown_state', 'version', 'marker_json', 'identity_empty', 'column'])
def test_invalid_schema_or_marker_preserves_debt_before_startup(world, damage):
    jid = debt()
    path = journal.journal_path()
    if damage == 'marker_json':
        journal.journal_identity_path().write_text('{invalid')
    else:
        with sqlite3.connect(path) as conn:
            if damage == 'unknown_state':
                conn.execute('PRAGMA ignore_check_constraints=ON')
                conn.execute("UPDATE mutations SET state='invented' WHERE id=?", (jid,))
            elif damage == 'version':
                conn.execute('PRAGMA user_version=90')
            elif damage == 'identity_empty':
                conn.execute('DELETE FROM journal_identity')
            else:
                conn.execute('ALTER TABLE mutations RENAME COLUMN input_json TO missing_body')
    before = path.read_bytes()
    assert journal.journal_health()['integrity'] == 'unknown'
    with pytest.raises((RuntimeError, sqlite3.Error)):
        journal.startup_recover()
    assert path.read_bytes() == before


@pytest.mark.parametrize('fault', ['marker_fsync', 'schema'])
def test_failed_establishment_is_never_retried_as_empty_store(world, monkeypatch, fault):
    def broken(*args, **kwargs):
        raise OSError('injected establishment failure')
    with monkeypatch.context() as m:
        if fault == 'marker_fsync':
            m.setattr(journal.os, 'fsync', broken)
        else:
            m.setattr(journal, '_create_schema', broken)
        with pytest.raises(OSError):
            journal.initialize_journal()
    assert journal.journal_identity_path().exists()
    path = journal.journal_path()
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((RuntimeError, sqlite3.Error)):
        journal.startup_recover()
    with pytest.raises((RuntimeError, sqlite3.Error)):
        journal.accept_job({'user_id': 'alice', 'bank_id': 'work'})
    assert (path.read_bytes() if path.exists() else None) == before


def test_explicit_adoption_cannot_mask_lost_identity_of_established_database(world):
    debt()
    journal.journal_identity_path().unlink()
    before = journal.journal_path().read_bytes()
    with pytest.raises(journal.JournalEvidenceError):
        journal.initialize_journal(adopt_existing=True)
    assert journal.journal_path().read_bytes() == before
    assert not journal.journal_identity_path().exists()


def test_health_opens_readonly_even_for_valid_db(world, monkeypatch):
    debt()
    real = sqlite3.connect
    calls = []
    def inspect(path, *args, **kwargs):
        calls.append((path, kwargs))
        return real(path, *args, **kwargs)
    monkeypatch.setattr(journal.sqlite3, 'connect', inspect)
    assert journal.journal_health()['repair_required'] == 1
    assert calls and all(str(path).endswith('?mode=ro') and kw['uri'] for path, kw in calls)


def test_two_processes_initialize_one_identity_and_preserve_both_intents(world):
    script = '''
import sys
from ducky import utils, mutation_journal as journal
utils.FACTS_DB = sys.argv[1]
journal.accept_job({'user_id': sys.argv[2], 'bank_id': 'work', 'messages': sys.argv[2]})
'''
    jobs = [subprocess.Popen([sys.executable, '-c', script, str(journal.journal_path().parent / 'facts.db'), owner],
                             env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for owner in ('first-process', 'second-process')]
    for process in jobs:
        out, err = process.communicate(timeout=30)
        assert process.returncode == 0, out + err
    assert journal.journal_health(deep=True)['counts'] == {'queued': 2}
    assert journal.startup_recover()['held_for_repair'] == 2
    with sqlite3.connect(journal.journal_path()) as conn:
        assert {row[0] for row in conn.execute('SELECT user_id FROM mutations')} == {'first-process', 'second-process'}


def test_process_death_after_marker_blocks_subsequent_startup(world):
    script = '''
import os, sys
from ducky import utils, mutation_journal as journal
utils.FACTS_DB = sys.argv[1]
journal._create_schema = lambda conn: os._exit(76)
journal.initialize_journal()
'''
    result = subprocess.run([sys.executable, '-c', script, str(journal.journal_path().parent / 'facts.db')],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 76, result.stderr
    assert journal.journal_identity_path().exists()
    before = journal.journal_path().read_bytes()
    assert journal.journal_health()['integrity'] == 'unknown'
    with pytest.raises(RuntimeError):
        journal.startup_recover()
    assert journal.journal_path().read_bytes() == before


@pytest.mark.parametrize('damage', ['unlink', 'drop_table', 'marker', 'mismatch'])
def test_rollback_refuses_unknown_journal_even_with_clean_wal(world, damage):
    from scripts.wal_rollback_compat import prepare_legacy_wal
    from ducky import wal_engine as we
    _, wal = world
    debt()
    wal.append(we.WALEntry(operation='add', status='committed'))
    before = wal.wal_file.read_bytes()
    if damage == 'unlink':
        journal.journal_path().unlink()
    elif damage == 'marker':
        journal.journal_identity_path().unlink()
    else:
        with sqlite3.connect(journal.journal_path()) as conn:
            if damage == 'drop_table':
                conn.execute('DROP TABLE mutations')
            else:
                conn.execute("UPDATE journal_identity SET identity='wrong'")
    with pytest.raises((RuntimeError, sqlite3.Error)):
        prepare_legacy_wal(journal.journal_path().parent, apply=True, writers_stopped=True)
    assert wal.wal_file.read_bytes() == before


def test_writer_waits_for_visible_uncommitted_identity(world):
    """A real second process sees the marker while the initializer is paused."""
    facts = str(journal.journal_path().parent / 'facts.db')
    first_script = '''
import sys
from ducky import utils, mutation_journal as journal
utils.FACTS_DB = sys.argv[1]
original = journal._publish_identity
def pause(path, identity):
    original(path, identity)
    print('marker-published', flush=True)
    assert sys.stdin.readline().strip() == 'continue'
journal._publish_identity = pause
journal.initialize_journal()
'''
    second_script = '''
import sys
from ducky import utils, mutation_journal as journal
utils.FACTS_DB = sys.argv[1]
print('writer-starting', flush=True)
journal.accept_job({'user_id':'second', 'bank_id':'work', 'messages':'retained input'})
'''
    first = subprocess.Popen([sys.executable, '-c', first_script, facts], env=os.environ.copy(),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = None
    try:
        assert first.stdout.readline().strip() == 'marker-published'
        assert journal.journal_identity_path().is_file()
        second = subprocess.Popen([sys.executable, '-c', second_script, facts], env=os.environ.copy(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert second.stdout.readline().strip() == 'writer-starting'
        with pytest.raises(subprocess.TimeoutExpired):
            second.wait(timeout=0.3)
        out, err = first.communicate(input='continue\n', timeout=15)
        assert first.returncode == 0, out + err
        out, err = second.communicate(timeout=15)
        assert second.returncode == 0, out + err
        assert journal.journal_health(deep=True)['counts'] == {'queued': 1}
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
