"""Real shell/SQLite/process proofs; no product API or production configuration."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

from scripts import restore_bundle as bundle

ROOT = Path(__file__).resolve().parents[1]


def run(script, *args, **extra):
    env = dict(os.environ, AIDUMEM_PYTHON=sys.executable, **extra)
    return subprocess.run(['bash', str(ROOT / 'scripts' / script), *map(str, args)],
                          env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)


def assert_failed(result):
    assert result.returncode != 0, result.stdout
    assert 'PASS' not in result.stdout and 'RESTORED_ISOLATED' not in result.stdout


def rewrite_sums(path):
    lines = []
    for entry in sorted(path.rglob('*')):
        if entry.is_file() and entry.name != bundle.CHECKSUMS:
            lines.append(hashlib.sha256(entry.read_bytes()).hexdigest() + '  ' + entry.relative_to(path).as_posix())
    (path / bundle.CHECKSUMS).write_text('\n'.join(lines) + '\n')


@pytest.fixture
def snapshot(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    conn = sqlite3.connect(source / 'facts.db')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.execute('CREATE TABLE historical(id TEXT PRIMARY KEY, body TEXT)')
    conn.execute('INSERT INTO historical VALUES (?,?)', ('before-backup', 'historical synthetic record'))
    conn.commit()
    (source / 'wal').mkdir()
    # This is intentionally opaque business WAL data: preserve exact bytes,
    # without invoking startup recovery during a restore or read-only drill.
    (source / 'wal' / 'mem_mutations.wal').write_bytes(b'synthetic durable intent\n')
    (source / 'empty-directory').mkdir()
    (source / '.ui-state').write_text('synthetic local config\n')
    (source / '.api-process.lock').touch()
    home = Path(os.environ.get('AIDUMEI_TEST_BACKUP_HOME', Path.home()))
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.aidumei-restore-fixture-', dir=home) as scratch:
        try:
            created = run('backup_gate.sh', 'create', 'restore', AIDUMEM_DATA_DIR=str(source), AIDUMEM_BACKUP_ROOT=scratch)
            assert created.returncode == 0, created.stderr
            backup = next(Path(scratch).glob('pre-restore-*'))
            yield source, backup
        finally:
            conn.close()


def apply(backup, target, **kwargs):
    return run('restore_gate.sh', '--isolated', backup, AIDUMEM_DATA_DIR=str(target),
               RESTORE_GATE_ALLOW_APPLY='1', **kwargs)


def test_fresh_isolated_restore_preserves_full_validated_bundle(snapshot, tmp_path):
    source, backup = snapshot
    target = tmp_path / 'target'
    result = apply(backup, target, AIDUMEM_API_BASE='http://127.0.0.1:1/must-not-connect')
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['status'] == 'RESTORED_ISOLATED' and receipt['service_tested'] is False
    assert receipt['live_qdrant_restored'] is False
    assert receipt['data_dir'] == str(target.resolve())
    assert receipt['snapshot_id'] == bundle.verify(backup)['snapshot_id']
    assert (target / 'empty-directory').is_dir()
    assert (target / 'wal/mem_mutations.wal').read_bytes() == (source / 'wal/mem_mutations.wal').read_bytes()
    assert not list(target.glob('*.db-*'))
    for name in bundle.verify(backup)['checksums']:
        assert (target / name).read_bytes() == (backup / name).read_bytes()
    with sqlite3.connect(target / 'facts.db') as conn:
        assert conn.execute('SELECT body FROM historical').fetchall() == [('historical synthetic record',)]
    assert not (target / bundle.INCOMPLETE).exists()


def test_live_target_refused_without_modifying_any_file(snapshot, tmp_path):
    _, backup = snapshot
    target = tmp_path / 'active'; target.mkdir()
    code = '''import sys, sqlite3
from ducky.process_lock import acquire_api_process_lock
acquire_api_process_lock(sys.argv[1])
c=sqlite3.connect(sys.argv[1]+'/facts.db')
c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE live(body TEXT)');c.execute("INSERT INTO live VALUES ('keep')");c.commit()
print('held',flush=True);sys.stdin.read()
'''
    proc = subprocess.Popen([sys.executable, '-c', code, str(target)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=dict(os.environ, PYTHONPATH=str(ROOT)))
    try:
        assert proc.stdout.readline().strip() == 'held'
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        assert_failed(apply(backup, target))
        assert before == {p.name: p.read_bytes() for p in target.iterdir()}
    finally:
        proc.kill(); proc.communicate(timeout=5)


@pytest.mark.parametrize('shape', ['empty-directory', 'symlink', 'nonempty', 'file'])
def test_any_existing_or_linked_target_is_rejected(snapshot, tmp_path, shape):
    _, backup = snapshot
    target = tmp_path / 'target'
    if shape == 'symlink':
        other = tmp_path / 'other'; other.mkdir(); target.symlink_to(other, target_is_directory=True)
    elif shape == 'file':
        target.touch()
    else:
        target.mkdir()
        if shape == 'nonempty':
            (target / 'keep').write_text('not a restore target')
    assert_failed(apply(backup, target))


@pytest.mark.parametrize('extra', ['unlisted.sqlite3', 'spare.txt', 'facts.db-wal', 'extra-empty-dir'])
def test_verify_and_restore_reject_unlisted_members(snapshot, tmp_path, extra):
    _, backup = snapshot
    entry = backup / extra
    if extra.endswith('dir'):
        entry.mkdir()
    elif extra.endswith('sqlite3'):
        with sqlite3.connect(entry) as conn:
            conn.execute('CREATE TABLE forged(x)')
    else:
        entry.write_bytes(b'unlisted')
    assert_failed(run('backup_gate.sh', 'verify', backup))
    assert_failed(run('restore_gate.sh', '--dry-run', backup))
    target = tmp_path / 'target'
    assert_failed(apply(backup, target)); assert not target.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory-instead-of-file'])
def test_ambiguous_file_types_are_rejected_before_restore(snapshot, tmp_path, kind):
    _, backup = snapshot
    member = backup / '.ui-state'; member.unlink()
    external = tmp_path / 'external'; external.write_text('keep outside untouched')
    if kind == 'symlink':
        member.symlink_to(external)
    elif kind == 'hardlink':
        os.link(external, member)
    elif kind == 'fifo':
        os.mkfifo(member)
    else:
        member.mkdir()
    target = tmp_path / 'target'
    assert_failed(apply(backup, target)); assert not target.exists()
    assert external.read_text() == 'keep outside untouched'


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'a//b', '././facts.db', 'a/../facts.db', 'a\\b', 'facts.db ', 'x:y'])
def test_checksum_path_ambiguity_fails_closed(snapshot, tmp_path, path):
    _, backup = snapshot
    sums = backup / bundle.CHECKSUMS
    sums.write_text('0' * 64 + '  ' + path + '\n')
    assert_failed(apply(backup, tmp_path / 'target'))


@pytest.mark.parametrize('change', ['missing', 'duplicate-checksum', 'duplicate-json', 'sqlite-type', 'size', 'schema-bool'])
def test_valid_hashes_do_not_excuse_invalid_manifest(snapshot, tmp_path, change):
    _, backup = snapshot
    manifest = backup / bundle.MANIFEST
    value = json.loads(manifest.read_text())
    if change == 'missing':
        (backup / 'wal/mem_mutations.wal').unlink()
    elif change == 'duplicate-checksum':
        sums = backup / bundle.CHECKSUMS
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + '\n')
    elif change == 'duplicate-json':
        manifest.write_text(manifest.read_text().replace('"schema": 1', '"schema": 1, "schema": 1'))
        rewrite_sums(backup)
    else:
        row = next(r for r in value['files'] if r['path'] == 'facts.db')
        if change == 'sqlite-type': row['type'] = 'file'
        if change == 'size': row['size'] += 1
        if change == 'schema-bool': value['schema'] = True
        manifest.write_text(json.dumps(value)); rewrite_sums(backup)
    assert_failed(apply(backup, tmp_path / 'target'))


def test_parent_symlink_member_is_rejected(snapshot, tmp_path):
    _, backup = snapshot
    (backup / 'wal/mem_mutations.wal').unlink(); (backup / 'wal').rmdir()
    external = tmp_path / 'external'; external.mkdir(); (external / 'mem_mutations.wal').write_text('outside')
    (backup / 'wal').symlink_to(external, target_is_directory=True)
    assert_failed(apply(backup, tmp_path / 'target'))


def test_create_refuses_links_nested_roots_and_invalid_label(snapshot, tmp_path):
    source, _ = snapshot
    outside = tmp_path / 'outside'; outside.write_text('outside')
    (source / 'ambiguous').symlink_to(outside)
    assert_failed(run('backup_gate.sh', 'create', 'bad', AIDUMEM_DATA_DIR=str(source), AIDUMEM_BACKUP_ROOT=str(tmp_path / 'new-backups')))
    assert_failed(run('backup_gate.sh', 'create', '../bad', AIDUMEM_DATA_DIR=str(source), AIDUMEM_BACKUP_ROOT=str(tmp_path / 'new-backups')))
    assert_failed(run('backup_gate.sh', 'create', 'bad', AIDUMEM_DATA_DIR=str(source), AIDUMEM_BACKUP_ROOT=str(source / 'backups')))


def test_restore_holds_api_lock_through_copy_and_retains_failed_target(snapshot, tmp_path, monkeypatch):
    _, backup = snapshot
    target = tmp_path / 'target'
    def fail_after_lock(*args, **kwargs):
        code = 'from ducky.process_lock import acquire_api_process_lock; import sys; acquire_api_process_lock(sys.argv[1])'
        denied = subprocess.run([sys.executable, '-c', code, str(target)], env=dict(os.environ, PYTHONPATH=str(ROOT)),
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        assert denied.returncode != 0 and 'another API process' in denied.stderr
        raise OSError('synthetic interrupted copy')
    monkeypatch.setattr(bundle, '_copy', fail_after_lock)
    with pytest.raises(OSError, match='interrupted'):
        bundle.restore(backup, target)
    assert (target / bundle.INCOMPLETE).exists() and (target / bundle.API_LOCK).exists()
    assert not (target / bundle.RECEIPT).exists()
    assert_failed(apply(backup, target))


def fixture_spec(path):
    spec = {'database': 'facts.db', 'table': 'historical', 'key_column': 'id', 'key': 'before-backup',
            'value_column': 'body', 'expected_sha256': hashlib.sha256(b'historical synthetic record').hexdigest()}
    path.write_text(json.dumps(spec))
    return spec


def test_managed_drill_binds_directory_snapshot_instance_and_historical_row(snapshot, tmp_path):
    _, backup = snapshot
    target = tmp_path / 'target'
    receipt = json.loads(apply(backup, target).stdout)
    fixture = tmp_path / 'fixture.json'; fixture_spec(fixture)
    result = run('restore_gate.sh', '--drill', target, receipt['snapshot_id'], fixture)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof['status'] == 'PASS' and proof['proof'] == 'managed_local_sqlite_readback_only'
    assert proof['snapshot_id'] == receipt['snapshot_id'] and proof['restore_id'] == receipt['restore_id']
    assert proof['data_dir'] == str(target.resolve()) and proof['instance_id'] and proof['pid'] != os.getpid()
    assert proof['historical_fixture']['found'] is True and proof['service_tested'] is False
    assert 'historical synthetic record' not in result.stdout


@pytest.mark.parametrize('change', ['snapshot', 'copied-target', 'fixture-hash', 'missing-row', 'modified-db', 'extra-file'])
def test_drill_rejects_changed_identity_or_historical_evidence(snapshot, tmp_path, change):
    _, backup = snapshot
    target = tmp_path / 'target'
    receipt = json.loads(apply(backup, target).stdout)
    fixture = tmp_path / 'fixture.json'; spec = fixture_spec(fixture)
    snapshot_id = receipt['snapshot_id']
    if change == 'snapshot': snapshot_id = '0' * 64
    if change == 'copied-target':
        copied = tmp_path / 'copied'; shutil.copytree(target, copied); target = copied
    if change == 'fixture-hash': spec['expected_sha256'] = '0' * 64
    if change == 'missing-row': spec['key'] = 'not-in-snapshot'
    if change == 'modified-db':
        with sqlite3.connect(target / 'facts.db') as conn:
            conn.execute("UPDATE historical SET body='newly-written-after-restore'")
    if change == 'extra-file': (target / 'untracked.db').write_bytes(b'extra')
    fixture.write_text(json.dumps(spec))
    assert_failed(run('restore_gate.sh', '--drill', target, snapshot_id, fixture))


def test_legacy_verify_remains_supported_but_apply_requires_typed_bundle(tmp_path):
    backup = tmp_path / 'legacy'; backup.mkdir()
    with sqlite3.connect(backup / 'facts.db') as conn:
        conn.execute('CREATE TABLE old(x)')
    (backup / bundle.MARKER).write_text('ok\n')
    digest = hashlib.sha256((backup / 'facts.db').read_bytes()).hexdigest()
    (backup / bundle.CHECKSUMS).write_text(digest + '  ./facts.db\n')
    assert run('restore_gate.sh', '--dry-run', backup).returncode == 0
    assert_failed(apply(backup, tmp_path / 'target'))


def test_apply_is_disabled_without_explicit_opt_in(snapshot, tmp_path):
    _, backup = snapshot
    result = run('restore_gate.sh', backup, AIDUMEM_DATA_DIR=str(tmp_path / 'target'), RESTORE_GATE_ALLOW_APPLY='0')
    assert result.returncode == 4
    assert not (tmp_path / 'target').exists()


def test_legacy_extra_empty_directory_is_not_proven(tmp_path):
    backup = tmp_path / 'legacy'; backup.mkdir()
    (backup / 'state.json').write_text('{}')
    (backup / bundle.MARKER).write_text('ok')
    (backup / bundle.CHECKSUMS).write_text(hashlib.sha256(b'{}').hexdigest() + '  state.json\n')
    (backup / 'extra-empty').mkdir()
    assert_failed(run('restore_gate.sh', '--dry-run', backup))


def test_changed_backup_during_copy_is_quarantined(snapshot, tmp_path, monkeypatch):
    _, backup = snapshot
    target = tmp_path / 'target'
    original = bundle._copy
    def change_then_copy(root, name, destination, expected=None):
        if name == '.ui-state':
            (root / name).write_text('changed after verification')
        return original(root, name, destination, expected)
    monkeypatch.setattr(bundle, '_copy', change_then_copy)
    with pytest.raises(bundle.BundleError, match='source changed'):
        bundle.restore(backup, target)
    assert (target / bundle.INCOMPLETE).exists()
    assert not (target / bundle.RECEIPT).exists()


def test_changed_backup_membership_during_copy_is_quarantined(snapshot, tmp_path, monkeypatch):
    _, backup = snapshot
    target = tmp_path / 'target'
    original = bundle._copy
    def add_then_copy(root, name, destination, expected=None):
        if name == '.ui-state':
            (root / 'unlisted-late-file').write_text('added after verification')
        return original(root, name, destination, expected)
    monkeypatch.setattr(bundle, '_copy', add_then_copy)
    with pytest.raises(bundle.BundleError, match='membership changed'):
        bundle.restore(backup, target)
    assert (target / bundle.INCOMPLETE).exists()
    assert not (target / bundle.RECEIPT).exists()


def test_failed_durability_sync_never_removes_incomplete_marker(snapshot, tmp_path, monkeypatch):
    _, backup = snapshot
    target = tmp_path / 'target'
    def fail_sync(_):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(bundle, '_sync_tree', fail_sync)
    with pytest.raises(OSError, match='fsync failure'):
        bundle.restore(backup, target)
    assert (target / bundle.INCOMPLETE).exists()


def test_incomplete_restore_cannot_be_repackaged_as_verified_backup(snapshot):
    source, backup = snapshot
    (source / bundle.INCOMPLETE).write_text('failed earlier restore')
    result = run('backup_gate.sh', 'create', 'launder', AIDUMEM_DATA_DIR=str(source), AIDUMEM_BACKUP_ROOT=str(backup.parent))
    assert_failed(result)
    assert not list(backup.parent.glob('pre-launder-*'))


@pytest.mark.parametrize('database', ['archive.DB', 'archive.bin'])
def test_header_detected_sqlite_also_merges_its_sidecars(snapshot, database):
    source, backup = snapshot
    conn = sqlite3.connect(source / database)
    try:
        conn.execute('PRAGMA journal_mode=WAL'); conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('CREATE TABLE kept(body TEXT)'); conn.execute("INSERT INTO kept VALUES ('uncheckpointed')")
        conn.commit()
        assert (source / (database + '-wal')).exists()
        created = run('backup_gate.sh', 'create', 'generic', AIDUMEM_DATA_DIR=str(source), AIDUMEM_BACKUP_ROOT=str(backup.parent))
        assert created.returncode == 0, created.stderr
        additional = next(backup.parent.glob('pre-generic-*'))
        assert not (additional / (database + '-wal')).exists()
        assert not (additional / (database + '-shm')).exists()
        with sqlite3.connect(additional / database) as restored:
            assert restored.execute('SELECT body FROM kept').fetchall() == [('uncheckpointed',)]
    finally:
        conn.close()


def add_journal_pair(source):
    identity = 'a' * 32
    with sqlite3.connect(source / bundle.JOURNAL_DB) as conn:
        conn.execute('CREATE TABLE journal_identity(identity TEXT NOT NULL PRIMARY KEY)')
        conn.execute('INSERT INTO journal_identity VALUES (?)', (identity,))
    marker = source / bundle.JOURNAL_IDENTITY
    marker.write_text(json.dumps({'format': 1, 'identity': identity}))
    return marker


def test_journal_identity_pair_is_manifest_bound_and_restored(snapshot, tmp_path):
    source, backup = snapshot
    marker = add_journal_pair(source)
    created = bundle.create(source, backup.parent, 'paired')
    manifest = bundle.verify(created)['manifest']
    assert manifest['journal_identity_pairs'] == [{
        'database': bundle.JOURNAL_DB, 'marker': bundle.JOURNAL_IDENTITY, 'identity': 'a' * 32}]
    assert bundle.JOURNAL_IDENTITY not in manifest['excluded_source_members']
    assert bundle.JOURNAL_IDENTITY in {row['path'] for row in manifest['files']}
    target = tmp_path / 'paired-target'
    assert apply(created, target).returncode == 0
    assert (target / bundle.JOURNAL_IDENTITY).read_bytes() == marker.read_bytes()
    with sqlite3.connect((target / bundle.JOURNAL_DB).as_uri() + '?mode=ro&immutable=1', uri=True) as conn:
        assert conn.execute('SELECT identity FROM journal_identity').fetchall() == [('a' * 32,)]


@pytest.mark.parametrize('damage', ['missing-marker', 'missing-db', 'mismatch', 'malformed'])
def test_broken_journal_identity_pair_cannot_be_sealed(snapshot, damage):
    source, backup = snapshot
    marker = add_journal_pair(source)
    if damage == 'missing-marker': marker.unlink()
    elif damage == 'missing-db': (source / bundle.JOURNAL_DB).unlink()
    elif damage == 'mismatch': marker.write_text(json.dumps({'format': 1, 'identity': 'b' * 32}))
    else: marker.write_text('null')
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    with pytest.raises(bundle.BundleError, match='journal'):
        bundle.create(source, backup.parent, 'bad-pair')
    assert before == {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    assert not list(backup.parent.glob('pre-bad-pair-*'))


def test_manifest_cannot_drop_pair_binding_even_with_recomputed_checksums(snapshot):
    source, backup = snapshot
    add_journal_pair(source)
    created = bundle.create(source, backup.parent, 'paired')
    manifest = created / bundle.MANIFEST
    value = json.loads(manifest.read_text())
    value['journal_identity_pairs'] = []
    manifest.write_text(json.dumps(value))
    rewrite_sums(created)
    with pytest.raises(bundle.BundleError, match='pairing mismatch'):
        bundle.verify(created)
