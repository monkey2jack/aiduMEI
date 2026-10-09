"""Durable evidence for non-transactional SDK writes and accepted jobs.

This is NOT an exactly-once SDK or an automatic replay queue. An interrupted
write is ambiguous: preserve the input, block further writes in that scope,
and require evidence before an operator resolves it. SQL-only mutations keep
using their own atomic transactions. The database is deliberately separate
from facts.db, uses FULL synchronization and no persistent SQLite WAL, and
contains private input until acknowledgement/erasure.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import weakref

from ducky import utils
from ducky.bank_contract import make_scope
from ducky.scope_sql import scope_clause

_STATES = ('queued', 'started', 'acknowledged', 'repair_required', 'not_applied', 'forgotten')
_registry_lock = threading.Lock()
_scope_locks = weakref.WeakValueDictionary()
_held = threading.local()


class MutationUncertain(RuntimeError):
    """No safe automatic retry; the durable record must be reconciled first."""
    def __init__(self, mutation_id: str, reason: str = 'repair_required'):
        self.mutation_id = mutation_id
        self.reason = reason
        super().__init__(f'mutation {mutation_id}: {reason}; automatic replay disabled')


class MutationForgotten(MutationUncertain):
    pass


def _json(value):
    # Reject unsupported/non-finite payloads BEFORE the SDK side effect. A str()
    # fallback silently destroys exactly the evidence needed after a crash.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def journal_path() -> Path:
    return Path(utils.FACTS_DB).parent / 'mutation_journal.sqlite3'


class JournalEvidenceError(MutationUncertain):
    """Established evidence is missing/unknown; do not dispatch or recreate it."""
    def __init__(self, reason='journal_evidence_unknown'):
        super().__init__('journal:integrity', reason)
        self.repair_source = 'journal_store'


def journal_identity_path(path=None):
    return (path or journal_path()).with_suffix('.identity.json')


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _identity(path):
    try:
        marker = json.loads(journal_identity_path(path).read_text())
        if set(marker) != {'format', 'identity'} or marker['format'] != 1:
            raise ValueError('marker schema')
        if not isinstance(marker['identity'], str) or len(marker['identity']) != 32:
            raise ValueError('marker identity')
        return marker['identity']
    except (OSError, ValueError, TypeError) as exc:
        raise JournalEvidenceError('journal_identity_unknown') from exc


def _validate_schema(conn, *, deep=False):
    if conn.execute('PRAGMA user_version').fetchone()[0] != 1:
        raise JournalEvidenceError('journal_schema_unknown')
    if conn.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
        raise JournalEvidenceError('journal_mode_unknown')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(mutations)')}
    required = {'id', 'kind', 'user_id', 'bank_id', 'target_id', 'state', 'input_json',
                'result_json', 'target_ids_json', 'request_key', 'fingerprint',
                'reason', 'created_at', 'updated_at'}
    if columns != required:
        raise JournalEvidenceError('journal_schema_unknown')
    states = {row[0] for row in conn.execute('SELECT DISTINCT state FROM mutations')}
    if states - set(_STATES):
        raise JournalEvidenceError('journal_state_unknown')
    if deep and [row[0] for row in conn.execute('PRAGMA quick_check')] != ['ok']:
        raise JournalEvidenceError('journal_integrity_unknown')


def _validate_identity(conn, path):
    identity = _identity(path)
    rows = conn.execute('SELECT identity FROM journal_identity').fetchall()
    if len(rows) != 1 or rows[0][0] != identity:
        raise JournalEvidenceError('journal_identity_mismatch')


def _read_connection(path=None, *, deep=False, legacy=False):
    """Read-only inspection; never CREATE, migrate, or recover damaged evidence."""
    path = path or journal_path()
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        _validate_schema(conn, deep=deep)
        if not legacy:
            _validate_identity(conn, path)
        return conn
    except BaseException:
        conn.close()
        raise


def _create_schema(conn):
    conn.execute('''CREATE TABLE mutations (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        user_id TEXT NOT NULL, bank_id TEXT NOT NULL,
        target_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL CHECK(state IN
          ('queued','started','acknowledged','repair_required','not_applied','forgotten')),
        input_json TEXT, result_json TEXT, target_ids_json TEXT NOT NULL DEFAULT '[]',
        request_key TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
    )''')
    conn.execute('CREATE INDEX mutations_scope ON mutations(user_id,bank_id,state)')
    conn.execute('CREATE INDEX mutations_key ON mutations(user_id,bank_id,request_key)')
    conn.execute('PRAGMA user_version=1')


def _publish_identity(path, identity):
    # Write/fsync the independent marker BEFORE creating/modifying the DB.
    # A crash leaves a visible incomplete establishment; never an empty retry.
    fd = os.open(journal_identity_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(_json({'format': 1, 'identity': identity}))
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _establish(path, *, existing):
    identity = uuid.uuid4().hex
    _publish_identity(path, identity)
    if not existing:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
    try:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA secure_delete=ON')
        if not existing:
            _create_schema(conn)
        conn.execute('CREATE TABLE journal_identity (identity TEXT NOT NULL PRIMARY KEY)')
        conn.execute('INSERT INTO journal_identity(identity) VALUES (?)', (identity,))
        conn.commit()
        _sync_directory(path.parent)
    finally:
        conn.close()


_establishment_lock = threading.RLock()


@contextmanager
def _initialization_guard(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _establishment_lock:
        fd = os.open(path.parent / '.mutation-journal-init.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def initialize_journal(*, adopt_existing=False):
    """Establish fresh evidence, or explicitly enroll a validated v1 journal.

    Existing established stores are only checked, never recreated/migrated.
    Legacy adoption requires all writers stopped and independent verification
    that this really is pre-identity data. Preserve the DB + identity as a pair.
    Loss of BOTH artifacts needs an external deployment/backup inventory to
    distinguish it from first installation; their absence alone cannot prove it.
    """
    path = journal_path()
    with _initialization_guard(path):
        _initialize_locked(path, adopt_existing=adopt_existing)


def _initialize_locked(path, *, adopt_existing):
    if journal_identity_path(path).exists():
        _read_connection(path, deep=True).close()
        return
    existing = path.exists()
    if existing:
        if not adopt_existing:
            raise JournalEvidenceError('journal_legacy_adoption_required')
        conn = _read_connection(path, deep=True, legacy=True)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='journal_identity'").fetchone():
                raise JournalEvidenceError('journal_identity_missing')
        finally:
            conn.close()
    _establish(path, existing=existing)


def _connect():
    path = journal_path()
    # The marker becomes visible before the schema commits. Every writer must
    # wait for an in-flight initializer, including one that already sees files.
    # Once its lock is released, incomplete evidence still fails closed.
    with _initialization_guard(path):
        return _connect_initialized(path)


def _connect_initialized(path):
    if not path.exists() and not journal_identity_path(path).exists():
        _initialize_locked(path, adopt_existing=False)
    # Validate read-only first: even corrupt bytes or a missing table must not
    # be altered by SQLite pragmas, auto-creation or a schema repair attempt.
    _read_connection(path).close()
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _validate_schema(conn)
        _validate_identity(conn, path)
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA secure_delete=ON')
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def _db():
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def scope_lock(user_id: str, bank_id: str):
    """Reentrant thread + POSIX process exclusion, shared with privacy deletion.

    Hold for the *whole* deletion/execution, not only the journal SQL leg.
    Linux/macOS supported. Each scope serializes non-transactional writes.
    """
    scope = make_scope(user_id, bank_id)
    identity = str(journal_path()) + '\0' + _json([scope.user_id, scope.bank_id])
    with _registry_lock:
        lock = _scope_locks.get(identity)
        if lock is None:
            lock = threading.RLock()
            _scope_locks[identity] = lock
    with lock:
        held = getattr(_held, 'locks', None)
        if held is None:
            held = _held.locks = {}
        if identity in held:
            yield
            return
        directory = journal_path().parent / 'mutation_locks'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held[identity] = fd
            try:
                yield
            finally:
                held.pop(identity, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def serialized_scope(function):
    """Serialize a function with explicit user_id/bank_id arguments."""
    signature = inspect.signature(function)
    @wraps(function)
    def invoke(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        with scope_lock(bound.arguments['user_id'], bound.arguments.get('bank_id', 'default')):
            return function(*args, **kwargs)
    return invoke


forgetting_guard = scope_lock


def _insert(kind, user_id, bank_id, payload, *, state='queued', target_id='',
            mutation_id=None, request_key='', fingerprint=''):
    scope = make_scope(user_id, bank_id)
    raw = _json(payload)
    mid = mutation_id or uuid.uuid4().hex
    now = time.time()
    with _db() as conn:
        conn.execute('''INSERT INTO mutations
            (id,kind,user_id,bank_id,target_id,state,input_json,request_key,fingerprint,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (mid, kind, scope.user_id, scope.bank_id, str(target_id or ''), state,
             raw, request_key, fingerprint, now, now))
    return mid


def _get(mid, conn=None):
    if conn is not None:
        row = conn.execute('SELECT * FROM mutations WHERE id=?', (mid,)).fetchone()
        return dict(row) if row else None
    with _db() as con:
        return _get(mid, con)


def _check_scope(user_id, bank_id):
    # Called only inside scope_lock. Repair/forgetting deliberately bypasses
    # this write-only gate, so a failed deletion can actually settle.
    from ducky.wal_engine import assert_scope_writable, DeletionPending, WALIntegrityError
    try:
        assert_scope_writable(user_id, bank_id)
    except DeletionPending as exc:
        failure = MutationUncertain('wal:' + exc.wal_id, 'wal_delete_repair_required')
        failure.repair_source = 'wal'
        failure.wal_id = exc.wal_id
        raise failure from exc
    except WALIntegrityError as exc:
        # This identifies the ledger, not a made-up mutation journal row.
        failure = MutationUncertain('wal:integrity', 'wal_integrity_unknown')
        failure.repair_source = 'wal'
        failure.wal_id = None
        raise failure from exc
    predicate, params = scope_clause(make_scope(user_id, bank_id))
    with _db() as conn:
        row = conn.execute("SELECT id FROM mutations WHERE (state='repair_required' OR "
                           "(kind IN ('mem0_add','mem0_update') AND state='started'))" +
                           predicate + ' ORDER BY created_at LIMIT 1', params).fetchone()
        if row:
            raise MutationUncertain(row['id'])


def _target_ids(result):
    ids = set()
    if isinstance(result, dict):
        for key in ('id', 'memory_id'):
            if result.get(key):
                ids.add(str(result[key]))
        for value in result.values():
            if isinstance(value, (dict, list)):
                ids.update(_target_ids(value))
    elif isinstance(result, list):
        for value in result:
            ids.update(_target_ids(value))
    return ids


def _ack(mid, result):
    raw = _json(result)
    ids = _json(sorted(_target_ids(result)))
    with _db() as conn:
        cur = conn.execute('''UPDATE mutations SET state='acknowledged', result_json=?,
            target_ids_json=?, reason='', updated_at=? WHERE id=? AND state='started' ''',
                           (raw, ids, time.time(), mid))
        if cur.rowcount != 1:
            raise MutationUncertain(mid, 'ack_conflict')


def _uncertain(mid, reason):
    # Never store exception text: provider errors can echo secrets/payloads.
    with _db() as conn:
        conn.execute('''UPDATE mutations SET state='repair_required', reason=?,updated_at=?
            WHERE id=? AND state IN ('queued','started')''', (reason, time.time(), mid))


def perform_mutation(kind, user_id, bank_id, payload, callback, *, target_id=''):
    """Commit intent before callback; only return after durable acknowledgement."""
    scope = make_scope(user_id, bank_id)
    with scope_lock(scope.user_id, scope.bank_id):
        _check_scope(scope.user_id, scope.bank_id)
        mid = _insert(kind, scope.user_id, scope.bank_id, payload,
                      state='started', target_id=target_id)
        try:
            result = callback()
            if isinstance(result, dict) and str(result.get('status', '')).lower() in (
                    'error', 'failed', 'partial', 'repair_required'):
                raise MutationUncertain(mid, 'sdk_reported_failure')
            # None / arbitrary object is not a receipt. Existing mem0 add and
            # update contracts are JSON dict/list (including a valid empty list).
            if not isinstance(result, (dict, list)):
                raise MutationUncertain(mid, 'sdk_receipt_invalid')
            _ack(mid, result)
            return result
        except BaseException as exc:
            try:
                _uncertain(mid, 'sdk_or_ack_uncertain')
            except Exception:
                # The already committed started row remains recoverable; never
                # return success when the second database write also fails.
                pass
            if not isinstance(exc, Exception):
                raise
            raise MutationUncertain(mid) from exc


def execute_request(user_id, bank_id, payload, callback, *, request_key=''):
    """Wrap the COMPLETE synchronous /add work, before raw/SQL side effects.

    Stable request keys survive a crash between SDK ack and HTTP finalization.
    Parent receipt reflects all pipeline work, not merely the SDK leg. Callers
    must use this instead of blindly retrying a successful SDK after a later
    index/receipt failure. Empty keys still get durable evidence, no dedup claim.
    """
    from ducky.idempotency import _fingerprint
    scope = make_scope(user_id, bank_id)
    fp = _fingerprint(payload)
    with scope_lock(scope.user_id, scope.bank_id):
        if request_key:
            receipt = idempotency_receipt(request_key, scope.user_id, scope.bank_id, fp)
            if receipt:
                if receipt['action'] == 'replay':
                    return receipt['response']
                raise MutationUncertain(request_key, receipt.get('reason', receipt['action']))
        _check_scope(scope.user_id, scope.bank_id)
        mid = _insert('request', scope.user_id, scope.bank_id, payload, state='started',
                      request_key=request_key, fingerprint=fp)
        try:
            result = callback()
            if not isinstance(result, dict) or result.get('status') in (
                    'error', 'failed', 'partial', 'repair_required'):
                raise MutationUncertain(mid, 'request_not_completed')
            _ack(mid, result)
            return result
        except BaseException as exc:
            try:
                _uncertain(mid, 'request_uncertain')
            except Exception:
                pass
            if not isinstance(exc, Exception):
                raise
            raise MutationUncertain(mid) from exc


def install_memory_journal(memory):
    """Wrap the singleton's real bound methods, preserving SDK arguments/results."""
    for name in ('add', 'update'):
        original = getattr(memory, name, None)
        if original is None or getattr(original, '_aidumei_durable', False):
            continue
        signature = inspect.signature(original)

        def build(method, original, signature):
            @wraps(original)
            def invoke(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                values = bound.arguments
                metadata = values.get('metadata') or {}
                target = ''
                if method == 'add':
                    user = values.get('user_id')
                    bank = metadata.get('bank_id')
                else:
                    target = values.get('memory_id') or (args[0] if args else '')
                    # update has no user_id in the SDK contract. Resolve owner
                    # from the existing record before any mutation, never use
                    # a caller's unverified metadata as the ownership source.
                    item = memory.get(target)
                    if not isinstance(item, dict) or not item.get('user_id'):
                        raise ValueError('cannot determine mutation target owner')
                    user = item['user_id']
                    bank = (item.get('metadata') or {}).get('bank_id') or item.get('bank_id')
                scope = make_scope(user, bank)
                with scope_lock(scope.user_id, scope.bank_id):
                    if method == 'update':
                        current = memory.get(target)
                        if not isinstance(current, dict) or not current.get('user_id'):
                            raise ValueError('mutation target no longer exists')
                        current_scope = make_scope(current['user_id'],
                            (current.get('metadata') or {}).get('bank_id') or current.get('bank_id'))
                        if current_scope != scope:
                            raise ValueError('mutation target scope changed')
                    return perform_mutation('mem0_' + method, scope.user_id, scope.bank_id,
                                            {'args': list(args), 'kwargs': kwargs},
                                            lambda: original(*args, **kwargs), target_id=target)
            invoke._aidumei_durable = True
            return invoke
        setattr(memory, name, build(name, original, signature))
    return memory


def accept_job(payload, *, job_id=None):
    """Persist original request before giving the caller an accepted receipt.

    Production callers must include messages, metadata and infer. Old internal
    callers remain accepted, but incomplete payloads are explicitly identified
    on restart and can never be replayed automatically.
    """
    binding = payload.get('idempotency') or {}
    scope = make_scope(payload.get('user_id'), payload.get('bank_id'))
    with scope_lock(scope.user_id, scope.bank_id):
        _check_scope(scope.user_id, scope.bank_id)
        return _insert('job', scope.user_id, scope.bank_id, payload, mutation_id=job_id,
                       request_key=str(binding.get('key') or ''),
                       fingerprint=str(binding.get('fingerprint') or ''))


def attach_job_input(job_id, *, messages, metadata, infer, user_id, bank_id):
    """Coalesce supplies exact input before enqueuing; does not replace a terminal row."""
    row = _get(job_id)
    scope = make_scope(user_id, bank_id)
    if not row:
        return accept_job({'user_id': scope.user_id, 'bank_id': scope.bank_id,
                           'messages': messages, 'metadata': metadata, 'infer': infer}, job_id=job_id)
    if (row['user_id'], row['bank_id']) != (scope.user_id, scope.bank_id):
        raise ValueError('job scope mismatch')
    with scope_lock(scope.user_id, scope.bank_id), _db() as conn:
        row = _get(job_id, conn)
        if row['state'] != 'queued':
            raise MutationUncertain(job_id, row['state'])
        payload = json.loads(row['input_json'])
        payload.update(messages=messages, metadata=metadata, infer=infer)
        conn.execute('UPDATE mutations SET input_json=?,updated_at=? WHERE id=?',
                     (_json(payload), time.time(), job_id))


def update_job(job_id, **fields):
    row = _get(job_id)
    if not row:
        raise KeyError(job_id)
    with scope_lock(row['user_id'], row['bank_id']), _db() as conn:
        row = _get(job_id, conn)
        if row['state'] == 'forgotten':
            raise MutationForgotten(job_id, 'forgotten')
        status = fields.get('status')
        if row['state'] in ('acknowledged', 'not_applied', 'repair_required'):
            return  # late callbacks cannot turn an uncertain/terminal job green
        if status == 'running':
            state = 'started'
        elif status == 'done':
            result = fields.get('result')
            failed = not isinstance(result, dict) or result.get('status') in (
                'error', 'failed', 'partial', 'repair_required')
            state = 'repair_required' if failed else 'acknowledged'
        elif status == 'error':
            state = 'repair_required'
        else:
            state = row['state']
        # Persist structured result. Error strings intentionally omitted.
        result = fields.get('result')
        conn.execute('''UPDATE mutations SET state=?,result_json=?,target_ids_json=?,
            reason=?,updated_at=? WHERE id=?''',
                     (state, _json(result), _json(sorted(_target_ids(result))),
                      'job_uncertain' if state == 'repair_required' else '', time.time(), job_id))


@contextmanager
def execution_guard(user_id, bank_id='default', *, job_ids=()):
    """Wrap the entire background batch (including running/done transitions).

    Prevents a stale in-memory queue from resurrecting data after deletion.
    Unknown IDs are rejected; callers must not treat them as legacy safe work.
    """
    scope = make_scope(user_id, bank_id)
    with scope_lock(scope.user_id, scope.bank_id):
        _check_scope(scope.user_id, scope.bank_id)
        for jid in job_ids:
            row = _get(jid)
            if not row or (row['user_id'], row['bank_id']) != (scope.user_id, scope.bank_id):
                raise MutationUncertain(jid, 'unknown_job')
            if row['state'] != 'queued':
                raise MutationUncertain(jid, row['state'])
        yield


def job_record(job_id, *, user_id=None, bank_id=None):
    row = _get(job_id)
    if not row or row['kind'] != 'job':
        return None
    if user_id is not None and row['user_id'] != str(user_id):
        return None
    if bank_id is not None and row['bank_id'] != str(bank_id):
        return None
    payload = json.loads(row['input_json'] or '{}')
    status = {'acknowledged': 'done', 'started': 'running'}.get(row['state'], row['state'])
    result = json.loads(row['result_json'] or 'null')
    if row['state'] == 'queued' and isinstance(result, dict) and result.get('status') == 'coalescing':
        status = 'coalescing'
    return {'job_id': job_id, 'status': status, 'created_at': row['created_at'],
            'updated_at': row['updated_at'], 'user_id': row['user_id'], 'bank_id': row['bank_id'],
            'payload_preview': (payload.get('text_preview') or '')[:120],
            'result': result,
            'error': row['reason'] or None, 'accepted_durable': True,
            'payload_complete': 'messages' in payload,
            'durable': row['state'] == 'acknowledged'}


def startup_recover():
    """Run before accepting traffic. Recovery NEVER invokes the SDK.

    Acquires the same scope lock as writers: a live writer in another process
    must finish before it can be inspected. Queued jobs are conservatively held
    for repair too (their surrounding SQL/ingest side effects may have run).
    """
    initialize_journal()  # deep integrity/identity validation before any state change
    with _db() as conn:
        scopes = conn.execute("SELECT DISTINCT user_id,bank_id FROM mutations WHERE state IN ('queued','started')").fetchall()
    changed = 0
    for scope in scopes:
        predicate, params = scope_clause(make_scope(scope['user_id'], scope['bank_id']))
        with scope_lock(scope['user_id'], scope['bank_id']), _db() as conn:
            cur = conn.execute('''UPDATE mutations SET state='repair_required',
                reason='interrupted_process',updated_at=? WHERE state IN ('queued','started')''' +
                               predicate, [time.time(), *params])
            changed += cur.rowcount
    return {**journal_health(deep=True), 'recovered': 0, 'held_for_repair': changed,
            'automatic_replay': False}


def journal_health(*, deep=False):
    """No body disclosure; deep integrity scan is for startup/diagnostics only."""
    try:
        from contextlib import closing
        with closing(_read_connection(deep=deep)) as conn:
            counts = {row[0]: row[1] for row in conn.execute('SELECT state,count(*) FROM mutations GROUP BY state')}
            integrity = 'ok' if deep else 'not_checked'  # _read_connection verified every quick_check row
            failed_jobs = conn.execute("SELECT count(*) FROM mutations WHERE kind='job' AND state='repair_required'").fetchone()[0]
        unknown = set(counts) - set(_STATES)
        ok = integrity in ('ok', 'not_checked') and not unknown and not counts.get('repair_required')
        return {'status': 'ok' if ok else 'degraded', 'counts': counts,
                'repair_required': counts.get('repair_required', 0), 'failed_durable_jobs': failed_jobs, 'integrity': integrity,
                'automatic_replay': False}
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return {'status': 'degraded', 'error': type(exc).__name__, 'integrity': 'unknown',
                'reason': getattr(exc, 'reason', 'journal_evidence_unknown'), 'automatic_replay': False}


def inspect_mutation(mutation_id, user_id, bank_id, *, include_payload=False):
    scope = make_scope(user_id, bank_id)
    row = _get(mutation_id)
    if not row or (row['user_id'], row['bank_id']) != (scope.user_id, scope.bank_id):
        return None
    if include_payload:
        row['input'] = json.loads(row.pop('input_json') or 'null')
        row['result'] = json.loads(row.pop('result_json') or 'null')
    else:
        row.pop('input_json', None)
        row.pop('result_json', None)
    return row


def list_repairs(user_id, bank_id='default'):
    scope = make_scope(user_id, bank_id)
    predicate, params = scope_clause(scope)
    with _db() as conn:
        return [dict(row) for row in conn.execute('''SELECT id,kind,state,target_id,reason,created_at,updated_at
            FROM mutations WHERE state='repair_required' ''' + predicate + ' ORDER BY created_at', params)]


def resolve_mutation(mutation_id, user_id, bank_id, *, resolution, evidence, result=None):
    """Operator-only evidence recording. Never infer success from absence of error.

    evidence must describe an independent backend verification; this function
    records the operator's decision, it cannot prove an LLM-generated add's
    completeness. The API integrating this must enforce administrator access.
    """
    if resolution not in ('confirmed_applied', 'confirmed_not_applied'):
        raise ValueError('explicit confirmed resolution required')
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('verification evidence required')
    scope = make_scope(user_id, bank_id)
    with scope_lock(scope.user_id, scope.bank_id), _db() as conn:
        row = _get(mutation_id, conn)
        if not row or (row['user_id'], row['bank_id']) != (scope.user_id, scope.bank_id):
            raise KeyError(mutation_id)
        if row['state'] != 'repair_required':
            raise ValueError('only repair_required records can be resolved')
        state = 'acknowledged' if resolution == 'confirmed_applied' else 'not_applied'
        if state == 'acknowledged':
            if result is None:
                result = {'status': 'ok', 'durable': True, 'action': 'operator_verified',
                          'mutation_id': mutation_id}
            if not isinstance(result, dict) or result.get('status') in (
                    'error', 'failed', 'partial', 'repair_required'):
                raise ValueError('confirmed_applied requires a successful receipt object')
        conn.execute('''UPDATE mutations SET state=?,result_json=?,target_ids_json=?,
            reason=?,updated_at=? WHERE id=?''',
                     (state, _json(result), _json(sorted(_target_ids(result))),
                      # evidence may contain private text: store only its digest.
                      resolution + ':' + hashlib.sha256(evidence.encode()).hexdigest(),
                      time.time(), mutation_id))
    return {'id': mutation_id, 'state': state, 'automatic_replay': False}


@serialized_scope
def _erase(user_id, bank_id, target_id=None):
    scope = make_scope(user_id, bank_id)
    # Every body is scrubbed: even an unrelated add's source can quote the
    # forgotten target. Keep only content-free receipts for acknowledged writes
    # whose returned IDs prove they are unrelated. Queued/uncertain work has no
    # such proof and is explicitly cancelled, never replayed after erasure.
    predicate, params = scope_clause(scope)
    with scope_lock(scope.user_id, scope.bank_id), _db() as conn:
        records = conn.execute("SELECT * FROM mutations WHERE state!='forgotten'" + predicate,
                               params).fetchall()
        for row in records:
            ids = set(json.loads(row['target_ids_json']))
            if row['target_id']:
                ids.add(row['target_id'])
            unrelated_ack = (target_id is not None and row['state'] == 'acknowledged'
                             and ids and str(target_id) not in ids)
            receipt = _json({'status': 'ok', 'durable': True, 'action': 'journal_receipt_redacted',
                             'mutation_id': row['id'], 'target_ids': sorted(ids)}) if unrelated_ack else None
            conn.execute("""UPDATE mutations SET input_json=NULL,result_json=?,
                target_id=?,target_ids_json=?,reason='privacy_erased',state=?,updated_at=? WHERE id=?""",
                         (receipt, row['target_id'] if unrelated_ack else '',
                          _json(sorted(ids)) if unrelated_ack else '[]',
                          'acknowledged' if unrelated_ack else 'forgotten', time.time(), row['id']))
        count = len(records)
    # Clear this process's caches as well. Other processes cannot execute stale
    # work because execution_guard validates the durable forgotten marker.
    import sys
    coalesce = sys.modules.get('ducky.speed.coalesce')
    if coalesce is not None:
        with coalesce._coalesce_lock:
            for key, value in list(coalesce._coalesce_buf.items()):
                if (value.get('user_id'), value.get('bank_id') or 'default') == (scope.user_id, scope.bank_id):
                    del coalesce._coalesce_buf[key]
    jobs = sys.modules.get('ducky.speed.jobs')
    if jobs is not None:
        with jobs._jobs_lock:
            for jid, value in list(jobs._jobs.items()):
                if (value.get('user_id'), value.get('bank_id')) == (scope.user_id, scope.bank_id):
                    jobs._jobs.pop(jid, None)
                    jobs._job_idem.pop(jid, None)
    return count


def erase_scope(user_id, bank_id='default'):
    return _erase(user_id, bank_id)


def erase_target(user_id, bank_id, memory_id):
    if not str(memory_id or '').strip():
        raise ValueError('memory_id required')
    return _erase(user_id, bank_id, memory_id)


def idempotency_receipt(key, user_id, bank_id, fingerprint):
    """Persistent jobs outrank expiring in-memory-era accepted receipts."""
    if not key:
        return None
    predicate, params = scope_clause(make_scope(user_id, bank_id))
    with _db() as conn:
        row = conn.execute('SELECT * FROM mutations WHERE request_key=?' + predicate +
                           ' ORDER BY created_at DESC LIMIT 1', [key, *params]).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row['fingerprint'] and row['fingerprint'] != fingerprint:
        return {'action': 'conflict', 'key': key}
    if row['state'] == 'not_applied':
        return None  # operator verified no side effect; explicit retry permitted
    if row['kind'] == 'job':
        if row['state'] == 'acknowledged':
            from ducky.idempotency import _durable_receipt
            record = _durable_receipt(json.loads(row['result_json'] or '{}'), row['id'], key)
        else:
            record = {'status': 'forgotten' if row['state'] == 'forgotten' else 'accepted',
                      'durable': False, 'accepted_durable': True, 'job_id': row['id']}
    else:
        record = json.loads(row['result_json'] or '{}')
        if row['state'] == 'forgotten':
            record = {'status': 'forgotten', 'durable': False}
        elif row['state'] in ('queued', 'started'):
            return {'action': 'pending', 'key': key, 'reason': 'in_flight'}
    if row['state'] == 'repair_required':
        return {'action': 'pending', 'key': key, 'reason': 'repair_required', 'job_id': row['id']}
    from ducky.idempotency import redact_receipt
    return {'action': 'replay', 'key': key, 'state': row['state'],
            'response': redact_receipt(record)}
