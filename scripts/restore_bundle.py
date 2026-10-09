"""Validated local snapshots and isolated restoration; never an online restore.

Only stdlib is used. The separate managed drill proves an existing SQLite row
in this exact restored bundle, not API availability or live Qdrant recovery.
Per-file online snapshots do not claim a cross-store transaction boundary.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
import uuid

MANIFEST = 'BACKUP_MANIFEST.json'
CHECKSUMS = 'SHA256SUMS'
MARKER = '.backup_verified'
RECEIPT = '.restore-receipt.json'
INCOMPLETE = '.restore-incomplete'
API_LOCK = '.api-process.lock'
CONTROL = {MANIFEST, CHECKSUMS, MARKER}
RESERVED = CONTROL | {RECEIPT, INCOMPLETE, API_LOCK}
# Durable Noether identity evidence is payload, NEVER transient metadata.
JOURNAL_DB = 'mutation_journal.sqlite3'
JOURNAL_IDENTITY = 'mutation_journal.identity.json'
POLICY = 'isolated-bundle-v1'
MAX_METADATA = 8 * 1024 * 1024
_API_LOCK_MODULE = None


class BundleError(ValueError):
    """Fail closed without modifying an existing target."""


def _canonical(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise BundleError('root symlink is ambiguous')
    return path.resolve()


def _name(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BundleError('invalid member path')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts):
        raise BundleError('non-canonical member path')
    if '\\' in value or ':' in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BundleError('ambiguous member path')
    if unicodedata.normalize('NFC', value) != value:
        raise BundleError('non-normalized member path')
    return value


def _inventory(root):
    """No links, aliases, devices or implicit type coercions in a bundle."""
    found = {}
    aliases = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        for entry in sorted(directory.iterdir()):
            name = _name(entry.relative_to(root).as_posix())
            key = name.casefold()
            if key in aliases:
                raise BundleError('case-insensitive member collision')
            aliases.add(key)
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                found[name] = 'directory'
                pending.append(entry)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                found[name] = 'file'
            else:
                raise BundleError('links or special file types are forbidden')
            if len(found) > 100000:
                raise BundleError('too many backup members')
    return found


@contextmanager
def _member(root, name):
    """Open relative to directory descriptors; no followed parent/leaf links."""
    parts = _name(name).split('/')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BundleError('member must be a regular file with exactly one hard link')
            yield stream
    finally:
        os.close(directory)


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(root, name, *, destination=None, maximum=None):
    digest = hashlib.sha256()
    size = 0
    collected = []
    with _member(root, name) as stream:
        before = _signature(os.fstat(stream.fileno()))
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            if maximum is not None and size > maximum:
                raise BundleError('metadata limit exceeded')
            digest.update(chunk)
            if destination is not None:
                destination.write(chunk)
            if maximum is not None:
                collected.append(chunk)
        if _signature(os.fstat(stream.fileno())) != before:
            raise BundleError('member changed during read')
    return digest.hexdigest(), size, b''.join(collected)


def _metadata(root, name):
    return _read(root, name, maximum=MAX_METADATA)[2]


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise BundleError('duplicate JSON key')
        value[key] = item
    return value


def _json(raw):
    if len(raw) > MAX_METADATA:
        raise BundleError('metadata limit exceeded')
    return json.loads(raw, object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(BundleError('nonfinite JSON')))


def _write_json(path, value):
    with path.open('x', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
        out.write('\n')
        out.flush()
        os.fsync(out.fileno())
    path.chmod(0o600)


def _checksums(raw):
    result = {}
    for line in raw.decode('utf-8').splitlines():
        match = re.fullmatch(r'([a-fA-F0-9]{64}) [ *](.+)', line)
        if not match:
            raise BundleError('malformed checksum line')
        name = match[2]
        if name.startswith('./'):
            name = name[2:]  # Historical find/shasum format, exactly once.
        name = _name(name)
        if name in result:
            raise BundleError('duplicate checksum member')
        result[name] = match[1].lower()
    if not result:
        raise BundleError('empty checksum list')
    return result


def _sqlite(root, name):
    if PurePosixPath(name).suffix in {'.db', '.sqlite', '.sqlite3'}:
        return True
    with _member(root, name) as stream:
        return stream.read(16) == b'SQLite format 3\0'


def _quick_check(path):
    # Immutable snapshots only: never create WAL/shm or checkpoint a backup.
    with sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True) as conn:
        rows = conn.execute('PRAGMA quick_check').fetchall()
        if rows != [('ok',)]:
            raise BundleError('SQLite integrity check failed')


def _journal_pairs(root, inventory):
    """Read sealed copies only; preserve and bind the independent DB identity.

    Pre-identity journals may be archived, but require explicit adoption before
    startup. No initialization, adoption or application import happens here.
    """
    pairs = []
    names = {n for n in inventory if PurePosixPath(n).name in (JOURNAL_DB, JOURNAL_IDENTITY)}
    parents = sorted({PurePosixPath(n).parent for n in names})
    for parent in parents:
        database, marker = str(parent / JOURNAL_DB), str(parent / JOURNAL_IDENTITY)
        if inventory.get(database) != 'file':
            raise BundleError('journal identity lacks its database')
        with sqlite3.connect((root / database).as_uri() + '?mode=ro&immutable=1', uri=True) as conn:
            established = conn.execute("SELECT 1 FROM sqlite_master WHERE name='journal_identity'").fetchone()
            if not established and marker not in inventory:
                continue  # Historical archive only, not proof of startup readiness.
            if not established or inventory.get(marker) != 'file':
                raise BundleError('established journal requires its identity pair')
            value = _json(_metadata(root, marker))
            if (not isinstance(value, dict) or set(value) != {'format', 'identity'}
                    or type(value['format']) is not int or value['format'] != 1
                    or not isinstance(value['identity'], str) or len(value['identity']) != 32):
                raise BundleError('invalid journal identity marker')
            if conn.execute('SELECT identity FROM journal_identity').fetchall() != [(value['identity'],)]:
                raise BundleError('journal identity mismatch')
        pairs.append({'database': database, 'marker': marker, 'identity': value['identity']})
    return pairs


def _typed_manifest(raw, inventory, digests):
    value = _json(raw)
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value.get('schema') != 1 or value.get('policy') != POLICY:
        raise BundleError('unsupported backup manifest')
    records = value.get('files')
    directories = value.get('directories')
    if not isinstance(records, list) or not isinstance(directories, list):
        raise BundleError('invalid manifest members')
    expected = {}
    for row in records:
        if not isinstance(row, dict) or set(row) != {'path', 'type', 'size', 'sha256'}:
            raise BundleError('invalid manifest file record')
        name = _name(row['path'])
        if name in expected or name in RESERVED or row['type'] not in ('file', 'sqlite'):
            raise BundleError('duplicate, reserved or unknown member')
        if type(row['size']) is not int or row['size'] < 0 or row['sha256'] != digests.get(name):
            raise BundleError('invalid manifest size or digest')
        expected[name] = row
    actual_files = {n for n, kind in inventory.items() if kind == 'file'} - CONTROL
    if set(expected) != actual_files:
        raise BundleError('manifest file set differs from bundle')
    if any(not isinstance(n, str) for n in directories) or len(set(directories)) != len(directories):
        raise BundleError('invalid manifest directories')
    if {_name(n) for n in directories} != {n for n, k in inventory.items() if k == 'directory'}:
        raise BundleError('manifest directory set differs from bundle')
    return value, expected


def verify(backup, *, require_manifest=False):
    root = _canonical(backup)
    if not root.is_dir():
        raise BundleError('backup directory not found')
    inventory = _inventory(root)
    if not {CHECKSUMS, MARKER} <= inventory.keys():
        raise BundleError('backup lacks checksum or verification marker')
    sums_raw = _metadata(root, CHECKSUMS)
    sums = _checksums(sums_raw)
    modern = MANIFEST in inventory
    files = {n for n, kind in inventory.items() if kind == 'file'}
    if any(re.search(r'\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$', n, re.I) for n in files):
        raise BundleError('SQLite sidecars are forbidden in a sealed snapshot')
    # Legacy verification remains supported, but never admits unlisted payload.
    exempt = {CHECKSUMS} if modern else {CHECKSUMS, MARKER}
    if set(sums) != files - exempt:
        raise BundleError('checksum file set differs from backup (extra or missing member)')
    if not modern:
        implied = {str(p) for n in sums for p in PurePosixPath(n).parents if str(p) != '.'}
        if implied != {n for n, kind in inventory.items() if kind == 'directory'}:
            raise BundleError('legacy checksum list cannot prove extra directories')
    if require_manifest and not modern:
        raise BundleError('isolated restore requires a typed manifest; legacy backups are verify-only')
    metadata = _metadata(root, MANIFEST) if modern else None
    manifest, records = _typed_manifest(metadata, inventory, sums) if modern else ({}, {})
    databases = set()
    for name, expected in sums.items():
        actual, size, _ = _read(root, name)
        if actual != expected:
            raise BundleError('sha256 mismatch')
        is_db = _sqlite(root, name) if name not in CONTROL else False
        if modern and name in records:
            row = records[name]
            if size != row['size'] or (row['type'] == 'sqlite') != is_db:
                raise BundleError('manifest type or size mismatch')
        if is_db:
            _quick_check(root / name)
            databases.add(name)
    if any(name + suffix in files for name in databases for suffix in ('-wal', '-shm', '-journal')):
        raise BundleError('SQLite sidecars are forbidden in a sealed snapshot')
    pairs = _journal_pairs(root, inventory)
    if modern and manifest.get('journal_identity_pairs', []) != pairs:
        raise BundleError('manifest journal identity pairing mismatch')
    if _inventory(root) != inventory or _metadata(root, CHECKSUMS) != sums_raw:
        raise BundleError('backup changed during verification')
    return {'root': root, 'inventory': inventory, 'checksums': sums, 'manifest': manifest,
            'snapshot_id': hashlib.sha256(metadata or sums_raw).hexdigest(), 'db_count': len(databases),
            'checksum_id': hashlib.sha256(sums_raw).hexdigest(), 'typed': modern}


def _copy(root, name, target, expected=None):
    with target.open('xb') as out:
        digest, _, _ = _read(root, name, destination=out)
        out.flush()
        os.fsync(out.fileno())
    target.chmod(0o600)
    if expected is not None and digest != expected:
        raise BundleError('source changed during copy')


def _snapshot_sqlite(root, name, target):
    # _member checks every component before SQLite opens the canonical path.
    # Source data directories are trusted operator-owned; links are never valid.
    with _member(root, name):
        source = sqlite3.connect((root / name).as_uri() + '?mode=ro', uri=True, timeout=10)
        dest = sqlite3.connect(target)
        try:
            deadline = time.monotonic() + 30
            def progress(_status, _remaining, _total):
                if time.monotonic() >= deadline:
                    raise BundleError('SQLite snapshot deadline exceeded')
            source.backup(dest, pages=256, progress=progress, sleep=0.05)
            dest.execute('PRAGMA journal_mode=DELETE')
        finally:
            dest.close()
            source.close()
    target.chmod(0o600)
    _quick_check(target)


def _payload(root, inventory):
    sqlite_names = {n for n, kind in inventory.items() if kind == 'file' and n not in RESERVED and _sqlite(root, n)}
    omitted = {}
    for name, kind in inventory.items():
        if name in RESERVED:
            omitted[name] = 'runtime_or_previous_bundle_metadata'
        elif kind == 'file':
            base, separator, suffix = name.rpartition('-')
            if separator and suffix in ('wal', 'shm', 'journal') and base in sqlite_names:
                omitted[name] = 'merged_into_sqlite_snapshot'
            elif re.search(r'\.(?:db|sqlite|sqlite3)-(?:wal|shm|journal)$', name, re.I):
                raise BundleError('orphan or ambiguous SQLite sidecar')
    return sqlite_names, omitted


def _persistent_root(parent):
    if any(parent == p or p in parent.parents for p in (Path('/tmp').resolve(), Path('/var/tmp').resolve())):
        raise BundleError('backup root in /tmp 系 — 铁律拒绝')


def require(backup_root):
    root = _canonical(backup_root)
    _persistent_root(root)
    for candidate in sorted(root.glob('pre-*')):
        try:
            verify(candidate)
        except (ValueError, OSError, sqlite3.Error):
            continue
        print('硬门禁放行：存在已验证备份 ' + str(candidate))
        return
    raise BundleError('没有通过完整验证的备份——拒绝迁移')


def create(data, backup_root, label):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', label):
        raise BundleError('invalid backup label')
    source, parent = _canonical(data), _canonical(backup_root)
    if not source.is_dir():
        raise BundleError('data directory not found')
    if parent == source or source in parent.parents:
        raise BundleError('backup root must be outside the data directory')
    _persistent_root(parent)
    inventory = _inventory(source)
    if INCOMPLETE in inventory:
        raise BundleError('incomplete restore cannot be used as a backup source')
    sqlite_names, omitted = _payload(source, inventory)
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / ('pre-' + label + '-' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    dest.mkdir(mode=0o700)  # Never merge or replace another snapshot.
    try:
        _create_payload(source, dest, inventory, sqlite_names, omitted)
        _seal(dest, label, omitted)
        verify(dest, require_manifest=True)
        _sync_tree(dest)
        return dest
    except BaseException:
        shutil.rmtree(dest)
        raise


def _create_payload(source, dest, inventory, sqlite_names, omitted):
    for name, kind in sorted(inventory.items()):
        if name in omitted:
            continue
        target = dest / name
        if kind == 'directory':
            target.mkdir(mode=0o700)
        elif name in sqlite_names:
            _snapshot_sqlite(source, name, target)
        else:
            _copy(source, name, target)


def _seal(root, label, omitted):
    inventory = _inventory(root)
    rows = []
    for name, kind in inventory.items():
        if kind == 'file':
            digest, size, _ = _read(root, name)
            rows.append({'path': name, 'type': 'sqlite' if _sqlite(root, name) else 'file',
                         'size': size, 'sha256': digest})
    _write_json(root / MANIFEST, {'schema': 1, 'policy': POLICY, 'label': label,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'consistency': 'per_file; cross_store_atomicity_not_proven',
                'live_qdrant_server_snapshot': 'external_operations_required',
                'journal_identity_pairs': _journal_pairs(root, inventory),
                'excluded_source_members': omitted, 'files': rows,
                'directories': [n for n, kind in inventory.items() if kind == 'directory']})
    (root / MARKER).write_text('manifest=' + MANIFEST + '\n', encoding='utf-8')
    with (root / CHECKSUMS).open('x', encoding='utf-8') as out:
        for name, kind in sorted(_inventory(root).items()):
            if kind == 'file' and name != CHECKSUMS:
                out.write(_read(root, name)[0] + '  ' + name + '\n')
        out.flush()
        os.fsync(out.fileno())


@contextmanager
def _maintenance_lock(target):
    import fcntl
    identity = hashlib.sha256(str(target).encode()).hexdigest()[:24]
    path = target.parent / ('.aidumei-restore-' + identity + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BundleError('ambiguous maintenance lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BundleError('another restore owns this target') from exc
        yield
    finally:
        os.close(fd)  # Never unlink the stable lock inode.


def _api_lock(target):
    # Importing ducky.__init__ eagerly initializes stores. Load the SAME pure
    # lock implementation by filename so offline restoration has no such writes.
    global _API_LOCK_MODULE
    if _API_LOCK_MODULE is None:
        path = Path(__file__).resolve().parents[1] / 'ducky/process_lock.py'
        spec = importlib.util.spec_from_file_location('_restore_api_process_lock', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _API_LOCK_MODULE = module
    _API_LOCK_MODULE.acquire_api_process_lock(target)


def _directory_identity(root):
    info = root.stat()
    return {'data_dir': str(root), 'device': info.st_dev, 'inode': info.st_ino}


def restore(backup, target):
    bundle = verify(backup, require_manifest=True)
    target = _canonical(target)
    source = bundle['root']
    if target == source or source in target.parents or target in source.parents:
        raise BundleError('restore target overlaps the backup')
    if not target.parent.is_dir():
        raise BundleError('restore parent must already exist')
    with _maintenance_lock(target):
        if target.exists():
            raise BundleError('restore requires a nonexistent isolated target; live overlay is forbidden')
        target.mkdir(mode=0o700)
        _api_lock(target)
        (target / INCOMPLETE).write_text('Do not start a service from this incomplete restore.\n')
        # Failure retains this new quarantined directory, never deleting a lock
        # inode or touching the caller's existing data. A fresh target is required.
        for name, kind in sorted(bundle['inventory'].items()):
            if kind == 'directory':
                (target / name).mkdir(mode=0o700)
            else:
                expected = bundle['checksums'].get(name, bundle['checksum_id'])
                _copy(source, name, target / name, expected)
        _verify_restored(target, bundle)
        if _inventory(source) != bundle['inventory']:
            raise BundleError('backup membership changed during restore')
        receipt = {'schema': 1, 'policy': POLICY, 'status': 'RESTORED_ISOLATED',
                   'snapshot_id': bundle['snapshot_id'], 'checksum_id': bundle['checksum_id'],
                   'restore_id': uuid.uuid4().hex, **_directory_identity(target),
                   'restored_files': len(bundle['checksums']) + 1, 'db_count': bundle['db_count'],
                   'service_tested': False, 'live_qdrant_restored': False,
                   'created_at': datetime.now(timezone.utc).isoformat()}
        _write_json(target / RECEIPT, receipt)
        _sync_tree(target)
        # Publish completion only after payload and receipt are durable. A crash
        # before this final directory update persists may conservatively leave
        # the incomplete marker, but cannot bless an unsynced restore.
        (target / INCOMPLETE).unlink()
    return receipt


def _sync_tree(root):
    directories = [root / n for n, kind in _inventory(root).items() if kind == 'directory']
    for path in sorted(directories, reverse=True) + [root, root.parent]:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _verify_restored(target, bundle):
    expected = dict(bundle['inventory'], **{API_LOCK: 'file', INCOMPLETE: 'file'})
    if _inventory(target) != expected:
        raise BundleError('restored file set changed')
    for name, digest in bundle['checksums'].items():
        if _read(target, name)[0] != digest:
            raise BundleError('restored digest mismatch')
    for row in bundle['manifest']['files']:
        if row['type'] == 'sqlite':
            _quick_check(target / row['path'])


def _fixture_read(root, fixture, records):
    required = {'database', 'table', 'key_column', 'key', 'value_column', 'expected_sha256'}
    if not isinstance(fixture, dict) or set(fixture) != required:
        raise BundleError('invalid historical fixture specification')
    name = _name(fixture['database'])
    if records.get(name, {}).get('type') != 'sqlite':
        raise BundleError('fixture database is not in snapshot')
    identifiers = [fixture[k] for k in ('table', 'key_column', 'value_column')]
    if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for v in identifiers):
        raise BundleError('invalid fixture SQL identifier')
    table, key_column, value_column = identifiers
    with sqlite3.connect((root / name).as_uri() + '?mode=ro&immutable=1', uri=True) as conn:
        rows = conn.execute(f'SELECT "{value_column}" FROM "{table}" WHERE "{key_column}"=? LIMIT 2',
                            (fixture['key'],)).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise BundleError('historical fixture missing or ambiguous')
    digest = hashlib.sha256(rows[0][0].encode()).hexdigest()
    if digest != fixture['expected_sha256']:
        raise BundleError('historical fixture content mismatch')
    return {'database': name, 'value_sha256': digest, 'found': True}


def drill_worker(target, snapshot_id, challenge, fixture):
    root = _canonical(target)
    if not root.is_dir():
        raise BundleError('restored directory not found')
    _inventory(root)  # Reject links before any lock file is opened.
    with _maintenance_lock(root):
        _api_lock(root)
        if (root / INCOMPLETE).exists():
            raise BundleError('incomplete restore cannot be drilled')
        receipt = _json(_metadata(root, RECEIPT))
        if not isinstance(receipt, dict) or type(receipt.get('schema')) is not int or receipt.get('schema') != 1 or receipt.get('policy') != POLICY:
            raise BundleError('invalid restore receipt')
        if any(receipt.get(k) != v for k, v in _directory_identity(root).items()):
            raise BundleError('restored directory identity mismatch')
        if receipt.get('snapshot_id') != snapshot_id or receipt.get('status') != 'RESTORED_ISOLATED':
            raise BundleError('snapshot identity mismatch')
        # Re-validate all archived data without treating runtime lock/receipt as
        # archived members. No target mutation, checkpoint, or API call occurs.
        inventory = _inventory(root)
        payload_inventory = {n: kind for n, kind in inventory.items() if n not in {API_LOCK, RECEIPT}}
        sums_raw = _metadata(root, CHECKSUMS)
        sums = _checksums(sums_raw)
        manifest_raw = _metadata(root, MANIFEST)
        if hashlib.sha256(manifest_raw).hexdigest() != snapshot_id or hashlib.sha256(sums_raw).hexdigest() != receipt['checksum_id']:
            raise BundleError('restored snapshot metadata changed')
        _, records = _typed_manifest(manifest_raw, payload_inventory, sums)
        if set(sums) != {n for n, k in payload_inventory.items() if k == 'file'} - {CHECKSUMS}:
            raise BundleError('restored checksum coverage mismatch')
        for name, digest in sums.items():
            if _read(root, name)[0] != digest:
                raise BundleError('restored data changed since restore')
        observed = _fixture_read(root, fixture, records)
        return {'schema': 1, 'status': 'PASS', 'proof': 'managed_local_sqlite_readback_only',
                'snapshot_id': snapshot_id, 'restore_id': receipt['restore_id'],
                'instance_id': challenge, 'pid': os.getpid(), **_directory_identity(root),
                'historical_fixture': observed, 'service_tested': False, 'live_qdrant_restored': False}


def drill(target, snapshot_id, fixture_path):
    fixture_path = Path(fixture_path).absolute()
    fixture = _json(_metadata(fixture_path.parent, fixture_path.name))
    challenge = uuid.uuid4().hex
    command = [sys.executable, str(Path(__file__).resolve()), '_drill-worker', str(target), snapshot_id, challenge]
    child = subprocess.run(command, input=json.dumps(fixture), text=True, capture_output=True, timeout=30)
    if child.returncode:
        raise BundleError('managed readback failed: ' + child.stderr.strip())
    result = _json(child.stdout)
    if result.get('instance_id') != challenge or result.get('snapshot_id') != snapshot_id:
        raise BundleError('managed instance identity mismatch')
    if result.get('data_dir') != str(_canonical(target)) or result.get('service_tested') is not False:
        raise BundleError('managed proof scope mismatch')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('create', 'verify', 'require', 'restore', 'drill', '_drill-worker'))
    parser.add_argument('paths', nargs='+')
    args = parser.parse_args()
    try:
        result = _run(args.action, args.paths)
        if result is not None:
            print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0
    except (ValueError, OSError, sqlite3.Error, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1 if args.action in ('create', 'require') else 3


def _run(action, paths):
    expected = {'create': 3, 'verify': 1, 'require': 1, 'restore': 2, 'drill': 3, '_drill-worker': 3}
    if len(paths) != expected[action]:
        raise BundleError('wrong number of arguments')
    if action == 'create':
        dest = create(*paths)
        print('备份完成并通过校验: ' + str(dest))
        print(dest)
    elif action == 'verify':
        value = verify(paths[0])
        print('PASS: backup verification dry-run ok; sha256 全部匹配')
        print('db_count=' + str(value['db_count']))
        print('snapshot_id=' + value['snapshot_id'])
    elif action == 'require':
        require(paths[0])
    elif action == 'restore':
        return restore(*paths)
    elif action == 'drill':
        return drill(*paths)
    else:
        return drill_worker(*paths, _json(sys.stdin.read(MAX_METADATA + 1)))


if __name__ == '__main__':
    raise SystemExit(main())
