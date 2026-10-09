#!/usr/bin/env python3
"""Export the CURRENT v2 WAL for f0.3++; never restore historical data.

Default: export evidence without replacing the live WAL. --apply additionally
requires --writers-stopped and zero unresolved WAL/journal work. Stop ALL
writers, including CLI/embedded callers, before apply. The API lifetime lock
detects a live API, but cannot discover arbitrary scripts bypassing that lock.

Archive files are write-once, 0400 and content-addressed. They are not a WORM
device: a privileged owner can still alter the filesystem. Application requires
an operator review of the report and the rest of the downgrade migration.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

# Direct script invocation, as well as python -m scripts.wal_rollback_compat.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RollbackBlocked(RuntimeError):
    def __init__(self, message, report=None):
        self.report = report or {}
        super().__init__(message)


def _service_metadata(fd, original):
    # chown before chmod: POSIX chown can clear permission bits. Always check
    # the actual inode after both calls; root-created staging may not keep root
    # ownership when the daemon later returns as aidumem.
    os.fchown(fd, original.st_uid, original.st_gid)
    os.fchmod(fd, stat.S_IMODE(original.st_mode))
    actual = os.fstat(fd)
    if (actual.st_uid, actual.st_gid, stat.S_IMODE(actual.st_mode)) != (
            original.st_uid, original.st_gid, stat.S_IMODE(original.st_mode)):
        raise RollbackBlocked('cannot preserve original WAL uid/gid/mode')


def _ensure_service_lock(path, original):
    if path.is_symlink():
        raise RollbackBlocked('lock file must not be a symbolic link')
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        return  # Never replace/chmod an existing owner's lock inode.
    try:
        os.fchown(fd, original.st_uid, original.st_gid)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _offline_owner(directory, original):
    import fcntl
    _ensure_service_lock(directory / '.api-process.lock', original)
    fd = os.open(directory / '.api-process.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RollbackBlocked('API process owns data directory or exclusive locking failed') from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _write_once(path, data):
    """Publish a complete fsynced read-only artifact, never overwrite one."""
    from ducky.wal_engine import _fsync_directory
    if path.is_symlink():
        raise RollbackBlocked('archive path must not be a symbolic link')
    if path.exists():
        if path.read_bytes() != data or path.stat().st_mode & 0o222:
            raise RollbackBlocked('existing archive differs or is writable')
        return
    fd, temporary = tempfile.mkstemp(prefix='.archive-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o400)
            os.fsync(handle.fileno())
        os.link(temporary, path)  # O_EXCL-style publication, never os.replace.
        _fsync_directory(path.parent)
    finally:
        os.unlink(temporary)


def _journal_state(directory):
    from ducky.mutation_journal import _read_connection, journal_identity_path
    path = directory / 'mutation_journal.sqlite3'
    if not path.exists():
        if journal_identity_path(path).exists():
            raise RollbackBlocked('established mutation journal missing')
        return {}, []
    if path.is_symlink():
        raise RollbackBlocked('mutation journal must not be a symbolic link')
    conn = _read_connection(path, deep=True)
    try:
        counts = dict(conn.execute('SELECT state,count(*) FROM mutations GROUP BY state'))
        known = {'queued', 'started', 'repair_required', 'acknowledged', 'not_applied', 'forgotten'}
        reasons = []
        if counts.keys() - known:
            reasons.append('journal_unknown_state')
        if any(counts.get(state) for state in ('queued', 'started', 'repair_required')):
            reasons.append('journal_unresolved')
        return counts, reasons
    finally:
        conn.close()


def _verify_legacy_bytes(data, expected):
    """The old dataclass accepts these exact fields, with no envelope keys."""
    from ducky.wal_engine import WALEntry
    keys = {'wal_id', 'timestamp', 'user_id', 'bank_id', 'operation', 'payload', 'status', 'error'}
    records = []
    for line in data.decode('utf-8').splitlines():
        raw = json.loads(line)
        if not isinstance(raw, dict) or set(raw) != keys:
            raise RollbackBlocked('legacy record shape is unreadable')
        entry = WALEntry.from_json(line)
        if entry is None:
            raise RollbackBlocked('legacy record does not round-trip')
        records.append(asdict(entry))
    if records != expected:
        raise RollbackBlocked('conversion lost or changed current records')


def prepare_legacy_wal(data_dir, *, apply=False, writers_stopped=False):
    """Export all materialized current intents; only apply a settled ledger.

    No compaction/retention, no state rewrites, no SDK/backend or database
    restore. The raw archive also preserves the complete pre-conversion event
    history. Apply refuses pending AND failed because old startup semantics
    cannot safely repair them. Returns paths/counts/hashes, never record bodies.
    """
    from ducky.wal_engine import WALEngine, _file_lock, _fsync_directory, mutation_ownership
    if apply and not writers_stopped:
        raise ValueError('apply requires explicit writers_stopped=True')
    directory = Path(data_dir).resolve(strict=True)
    wal_path = directory / 'wal' / 'mem_mutations.wal'
    if not directory.is_dir() or not wal_path.is_file() or wal_path.is_symlink() or wal_path.parent.is_symlink():
        raise RollbackBlocked('requires an existing regular current WAL in this data directory')
    wal = WALEngine(str(wal_path.parent))
    original_stat = wal.wal_file.stat()
    # No scope lock is acquired here; ALL external writers must be stopped.
    # These stable locks serialize API, startup, direct delete and WAL callers.
    # Pre-create missing lock files with service ownership even if this tool is
    # run by root; do not strand the next service startup on root-owned 0600.
    for path in (wal.wal_dir / 'startup.replay.lock', wal.wal_dir / 'recovery.owner.lock', wal.lock_file):
        _ensure_service_lock(path, original_stat)
    with _offline_owner(directory, original_stat), _file_lock(wal.wal_dir / 'startup.replay.lock', exclusive=True), \
            mutation_ownership(wal), wal._write_lock, _file_lock(wal.lock_file, exclusive=True):
        original_stat = wal.wal_file.stat()
        entries, _, _ = wal._read_locked()  # complete chain/schema validation
        current = [asdict(entry) for entry in entries.values()]
        before = wal.wal_file.read_bytes()
        legacy = ''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in current).encode('utf-8')
        _verify_legacy_bytes(legacy, current)
        counts = {state: sum(row['status'] == state for row in current) for state in ('pending', 'failed', 'committed')}
        journal_counts, reasons = _journal_state(directory)
        if counts['pending'] or counts['failed']:
            reasons.append('wal_unresolved')
        if not writers_stopped:
            reasons.append('writers_not_asserted_stopped')
        source_hash, legacy_hash = _digest(before), _digest(legacy)
        archive_dir = wal.wal_dir / 'rollback-archive'
        if archive_dir.is_symlink():
            raise RollbackBlocked('archive directory must not be a symbolic link')
        archive_dir.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(wal.wal_dir)
        archive = archive_dir / (source_hash + '.current.wal')
        export = archive_dir / (source_hash + '.legacy-evidence.jsonl')
        _write_once(archive, before)
        _write_once(export, legacy)
        # Stable manifest describes only bytes, not mutable journal state.
        manifest = {'format': 1, 'source_sha256': source_hash, 'legacy_sha256': legacy_hash,
                    'counts': counts, 'records': len(current),
                    'legacy_reader_commit': 'd317d07860ae25ecb841f0a9c964fdd63b877899'}
        _write_once(archive_dir / (source_hash + '.manifest.json'),
                    (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode())
        report = {**manifest, 'archive': str(archive), 'legacy_export': str(export),
                  'journal_counts': journal_counts, 'blocked_reasons': reasons,
                  'compatibility_scope': 'wal_reader_only', 'requires_main_review': True,
                  'quiescence_asserted': bool(writers_stopped),
                  'wal_metadata': {'uid': original_stat.st_uid, 'gid': original_stat.st_gid,
                                   'mode': stat.S_IMODE(original_stat.st_mode)},
                  'wal_rollback_ready': not reasons, 'applied': False}
        if not apply:
            return report
        if reasons:
            raise RollbackBlocked('current work must be repaired before starting f0.3++', report)
        fd, temporary = tempfile.mkstemp(prefix='.legacy-wal-', dir=wal.wal_dir)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(legacy)
                handle.flush()
                _service_metadata(handle.fileno(), original_stat)
                os.fsync(handle.fileno())
            staged = Path(temporary).read_bytes()
            _verify_legacy_bytes(staged, current)
            if _digest(staged) != legacy_hash or archive.read_bytes() != before:
                raise RollbackBlocked('staged conversion/archive verification failed')
            os.replace(temporary, wal.wal_file)
            _fsync_directory(wal.wal_dir)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        report['applied'] = True
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--writers-stopped', action='store_true',
                        help='assert ALL API, CLI, cron and sync writers are stopped for the whole conversion')
    args = parser.parse_args(argv)
    try:
        report = prepare_legacy_wal(args.data_dir, apply=args.apply, writers_stopped=args.writers_stopped)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report['wal_rollback_ready'] else 2
    except RollbackBlocked as exc:
        print(json.dumps({'error': str(exc), **exc.report}, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'applied': None, 'inspection_required': True}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
