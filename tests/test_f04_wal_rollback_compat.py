"""Current-state conversion, not restoring an old database or old WAL."""
from dataclasses import asdict
import importlib.util
from pathlib import Path

import pytest

from ducky import mutation_journal as journal
from ducky import wal_engine as we
from scripts import wal_rollback_compat as compat


def old_reader(wal_file):
    spec = importlib.util.spec_from_file_location('f03pp_reader', Path(__file__).parent / 'fixtures/f03pp_wal_reader.py')
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules.
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    reader = module.WALEngine()
    reader.wal_file = Path(wal_file)
    reader.lock_file = reader.wal_file.with_suffix('.test-reader.lock')
    return module, reader


@pytest.fixture
def ledger(tmp_path):
    wal = we.WALEngine(str(tmp_path / 'wal'))
    for wid, state in [('unfinished', 'pending'), ('needs-repair', 'failed'), ('finished', 'committed')]:
        wal.append(we.WALEntry(wal_id=wid, user_id='synthetic-owner', bank_id='work',
                   timestamp=1, operation='delete', payload={'memory_id': wid, 'nested': {'body': wid}}))
        if state != 'pending':
            wal.mark_status(wid, state, error='synthetic error' if state == 'failed' else '')
    return wal


def test_export_retains_all_current_states_and_real_old_reader_can_read(ledger):
    before = ledger.wal_file.read_bytes()
    original = {e.wal_id: asdict(e) for e in ledger.entries()}
    _, old = old_reader(ledger.wal_file)
    with pytest.raises(we.WALIntegrityError):
        old.get_pending_entries()  # v2 really IS unreadable to f0.3++
    report = compat.prepare_legacy_wal(ledger.wal_dir.parent)
    assert report['applied'] is False and report['wal_rollback_ready'] is False
    assert report['counts'] == {'pending': 1, 'failed': 1, 'committed': 1}
    module, reader = old_reader(report['legacy_export'])
    entries = [module.WALEntry.from_json(line) for line in Path(report['legacy_export']).read_text().splitlines()]
    assert all(entries)
    assert {e.wal_id: asdict(e) for e in entries} == original
    assert [e.wal_id for e in reader.get_pending_entries()] == ['unfinished']
    # Old reader does not account for failed: conversion may not authorize boot.
    assert ledger.wal_file.read_bytes() == before
    archive = Path(report['archive'])
    assert archive.read_bytes() == before and archive.stat().st_mode & 0o222 == 0
    assert compat.prepare_legacy_wal(ledger.wal_dir.parent)['archive'] == str(archive)
    with pytest.raises(compat.RollbackBlocked):
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    assert ledger.wal_file.read_bytes() == before


def test_apply_preserves_current_committed_records_and_never_restores_old_data(ledger):
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    before = ledger.wal_file.read_bytes()
    current = {e.wal_id: asdict(e) for e in ledger.entries()}
    # There is intentionally an obsolete historical backup. Never read it.
    ledger.wal_file.with_name('mem_mutations.wal.pre-f04').write_bytes(b'obsolete bad backup')
    report = compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    assert report['applied'] is True and report['wal_rollback_ready'] is True
    assert Path(report['archive']).read_bytes() == before
    module, reader = old_reader(ledger.wal_file)
    assert reader.get_pending_entries() == []
    rows = [module.WALEntry.from_json(line) for line in ledger.wal_file.read_text().splitlines()]
    assert {e.wal_id: asdict(e) for e in rows} == current
    assert {e.wal_id: asdict(e) for e in ledger.entries()} == current


def test_apply_requires_explicit_stopped_writer_assertion(ledger):
    before = ledger.wal_file.read_bytes()
    with pytest.raises(ValueError, match='writers_stopped'):
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True)
    assert ledger.wal_file.read_bytes() == before


def test_corrupt_chain_cannot_be_exported_or_applied(ledger):
    ledger.wal_file.write_bytes(ledger.wal_file.read_bytes().replace(b'needs-repair', b'tampered-row'))
    before = ledger.wal_file.read_bytes()
    with pytest.raises(we.WALIntegrityError):
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    assert ledger.wal_file.read_bytes() == before
    assert not (ledger.wal_dir / 'rollback-archive').exists()


def test_live_api_lock_prevents_conversion(ledger):
    import fcntl
    with (ledger.wal_dir.parent / '.api-process.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(compat.RollbackBlocked, match='API'):
            compat.prepare_legacy_wal(ledger.wal_dir.parent)


@pytest.mark.parametrize('state', ['queued', 'started', 'repair_required'])
def test_pending_journal_blocks_old_runtime_even_when_wal_is_clean(ledger, monkeypatch, state):
    from ducky import utils
    monkeypatch.setattr(utils, 'FACTS_DB', str(ledger.wal_dir.parent / 'facts.db'))
    monkeypatch.setattr(we.WALEngine, '_instance', ledger)
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    jid = journal.accept_job({'user_id': 'synthetic-owner', 'bank_id': 'work', 'messages': 'new current input'})
    if state != 'queued':
        with journal._db() as conn:
            conn.execute('UPDATE mutations SET state=? WHERE id=?', (state, jid))
    before = ledger.wal_file.read_bytes()
    with pytest.raises(compat.RollbackBlocked) as err:
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    assert 'journal_unresolved' in err.value.report['blocked_reasons']
    assert ledger.wal_file.read_bytes() == before
    assert journal.job_record(jid)['status'] == ('running' if state == 'started' else state)


@pytest.mark.parametrize('fault', ['archive_fsync', 'target_replace'])
def test_io_failure_preserves_readable_current_ledger(ledger, monkeypatch, fault):
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    before = ledger.wal_file.read_bytes()
    def broken(*a, **kw):
        raise OSError('injected conversion I/O failure')
    if fault == 'archive_fsync':
        monkeypatch.setattr(compat.os, 'fsync', broken)
    else:
        monkeypatch.setattr(compat.os, 'replace', broken)
    with pytest.raises((OSError, we.WALIntegrityError)):
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    assert ledger.wal_file.read_bytes() == before
    assert len(ledger.entries()) == 3


def test_directory_sync_failure_after_replace_never_leaves_unreadable_wal(ledger, monkeypatch):
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    before = ledger.wal_file.read_bytes()
    real_replace = compat.os.replace
    replaced = []
    real_sync = we._fsync_directory
    def replace(*args):
        real_replace(*args)
        replaced.append(True)
    def sync(path):
        if replaced:
            raise OSError('injected target directory fsync failure after rename')
        real_sync(path)
    monkeypatch.setattr(compat.os, 'replace', replace)
    monkeypatch.setattr(we, '_fsync_directory', sync)
    with pytest.raises(we.WALIntegrityError):
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    _, reader = old_reader(ledger.wal_file)
    assert reader.get_pending_entries() == []
    assert len(ledger.entries()) == 3
    assert any(p.read_bytes() == before for p in (ledger.wal_dir / 'rollback-archive').glob('*.current.wal'))


def test_existing_archive_tampering_is_rejected_without_overwrite(ledger):
    report = compat.prepare_legacy_wal(ledger.wal_dir.parent)
    archive = Path(report['archive'])
    archive.chmod(0o600)
    archive.write_bytes(b'synthetic tampering')
    archive.chmod(0o400)
    before = ledger.wal_file.read_bytes()
    with pytest.raises(compat.RollbackBlocked, match='archive differs'):
        compat.prepare_legacy_wal(ledger.wal_dir.parent)
    assert archive.read_bytes() == b'synthetic tampering'
    assert ledger.wal_file.read_bytes() == before


def test_conversion_preserves_service_uid_gid_mode_and_new_lock_ownership(ledger):
    import stat
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    ledger.wal_file.chmod(0o640)
    before = ledger.wal_file.stat()
    report = compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    actual = ledger.wal_file.stat()
    assert (actual.st_uid, actual.st_gid, stat.S_IMODE(actual.st_mode)) == (before.st_uid, before.st_gid, 0o640)
    assert report['wal_metadata'] == {'uid': before.st_uid, 'gid': before.st_gid, 'mode': 0o640}
    for path in [ledger.wal_dir.parent / '.api-process.lock', ledger.wal_dir / 'startup.replay.lock', ledger.wal_dir / 'recovery.owner.lock']:
        attrs = path.stat()
        assert (attrs.st_uid, attrs.st_gid, stat.S_IMODE(attrs.st_mode)) == (before.st_uid, before.st_gid, 0o600)
    # The actual current service user can still append after replacement.
    with ledger.wal_file.open('ab') as handle:
        handle.write(b'\n')
    _, reader = old_reader(ledger.wal_file)
    assert reader.get_pending_entries() == []


def test_ownership_copy_failure_refuses_before_replace(ledger, monkeypatch):
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    compat.prepare_legacy_wal(ledger.wal_dir.parent, writers_stopped=True)
    before = ledger.wal_file.read_bytes()
    def forbidden(*args):
        raise PermissionError('synthetic chown denial')
    monkeypatch.setattr(compat.os, 'fchown', forbidden)
    with pytest.raises(we.WALIntegrityError):
        compat.prepare_legacy_wal(ledger.wal_dir.parent, apply=True, writers_stopped=True)
    assert ledger.wal_file.read_bytes() == before


def test_clean_export_does_not_claim_quiescence(ledger):
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    report = compat.prepare_legacy_wal(ledger.wal_dir.parent)
    assert report['wal_rollback_ready'] is False
    assert report['quiescence_asserted'] is False
    assert report['blocked_reasons'] == ['writers_not_asserted_stopped']


def test_cli_exports_then_applies_only_explicit_quiet_current_state(ledger):
    import json
    import subprocess
    import sys
    for entry in ledger.get_pending_entries():
        ledger.mark_status(entry.wal_id, 'committed')
    command = [sys.executable, str(Path(compat.__file__)), '--data-dir', str(ledger.wal_dir.parent)]
    before = ledger.wal_file.read_bytes()
    exported = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert exported.returncode == 2, exported.stderr
    assert json.loads(exported.stdout)['blocked_reasons'] == ['writers_not_asserted_stopped']
    assert ledger.wal_file.read_bytes() == before
    applied = subprocess.run(command + ['--apply', '--writers-stopped'], capture_output=True, text=True, timeout=10)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)['applied'] is True
    _, old = old_reader(ledger.wal_file)
    assert old.get_pending_entries() == []
