"""Real SQLite URI opens and rejected writes in disposable child processes."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


CHILD = """
import json, os, sqlite3, sys
from pathlib import Path
from f04_probe_guard import write_audit
c = json.load(sys.stdin)
root = Path(c['root'])
if c['guard'] == 'http':
    from f04_durability_http_probe import isolate
    isolate(root)
else:
    sys.addaudithook(write_audit(root, 'synthetic root'))
try:
    if c['operation'] == 'sqlite':
        with sqlite3.connect(c['target'], uri=True) as conn:
            assert conn.execute('SELECT body FROM records').fetchall() == [('synthetic',)]
            if c['write']:
                conn.execute("INSERT INTO records VALUES ('accepted')")
    elif c['operation'] == 'open':
        Path(c['target']).write_text('forbidden')
    elif c['operation'] == 'mkdir':
        Path(c['target']).mkdir()
    elif c['operation'] == 'remove':
        Path(c['target']).unlink()
except AssertionError as exc:
    assert not c['allowed'], str(exc)
    assert 'write outside' in str(exc) or 'invalid SQLite URI' in str(exc)
    print('blocked')
else:
    assert c['allowed'], 'guard admitted forbidden operation'
    print('accepted')
"""


@pytest.fixture
def files(tmp_path):
    root = tmp_path / 'isolated'
    root.mkdir()
    target = root / 'journal ü space % ? #.sqlite3'
    outside = tmp_path / 'outside.sqlite3'
    for path in (target, outside):
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE records(body TEXT)')
            conn.execute("INSERT INTO records VALUES ('synthetic')")
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    return root, target, outside


def run(files, guard, target, *, allowed=False, write=True, operation='sqlite'):
    root, _, outside = files
    before = outside.read_bytes()
    tests = Path(__file__).parent
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(tests), str(tests.parent))),
           'PYTHONDONTWRITEBYTECODE': '1'}
    config = dict(root=str(root), guard=guard, target=str(target), allowed=allowed,
                  write=write, operation=operation)
    result = subprocess.run([sys.executable, '-c', CHILD], input=json.dumps(config),
                            cwd=outside.parent, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ('accepted' if allowed else 'blocked')
    assert outside.read_bytes() == before


@pytest.mark.parametrize('guard', ['shared', 'http'])
@pytest.mark.parametrize('mode', ['', '?mode=ro', '?mode=rw', '?mode=rwc'])
@pytest.mark.parametrize('authority', ['', 'localhost'])
def test_real_local_sqlite_uri_with_escaped_filename(files, guard, mode, authority):
    root, target, _ = files
    uri = target.as_uri().replace('file:///', 'file://' + authority + '/') + mode
    run(files, guard, uri, allowed=True, write=mode != '?mode=ro')
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT count(*) FROM records').fetchone()[0] == (1 if mode == '?mode=ro' else 2)


@pytest.mark.parametrize('guard', ['shared', 'http'])
@pytest.mark.parametrize('case', ['absolute', 'outside_ro', 'relative', 'traversal',
                                 'encoded_traversal', 'encoded_slash', 'symlink'])
def test_outside_sqlite_targets_still_rejected(files, guard, case):
    root, _, outside = files
    uri = {
        'absolute': outside.as_uri() + '?mode=rw',
        'outside_ro': outside.as_uri() + '?mode=ro',
        'relative': 'file:outside.sqlite3?mode=rw',
        'traversal': root.as_uri() + '/../outside.sqlite3?mode=rw',
        'encoded_traversal': root.as_uri() + '/%2e%2e/outside.sqlite3?mode=rw',
        'encoded_slash': root.as_uri() + '/%2E%2e%2Foutside.sqlite3?mode=rw',
        'symlink': (root / 'escape' / 'outside.sqlite3').as_uri() + '?mode=rw',
    }[case]
    run(files, guard, uri)


@pytest.mark.parametrize('guard', ['shared', 'http'])
@pytest.mark.parametrize('change', [
    'remote', 'userinfo', 'port', 'encoded_authority', 'fragment', 'bad_escape',
    'bad_utf8', 'nul', 'control', 'empty_path', 'network_path', 'vfs', 'nolock',
    'duplicate_mode', 'memory_mode', 'empty_mode', 'malformed_query', 'unknown_query',
])
def test_malformed_or_unsupported_sqlite_uri_rejected(files, guard, change):
    root, target, _ = files
    uri = target.as_uri()
    changed = {
        'remote': uri.replace('file:///', 'file://remote/'),
        'userinfo': uri.replace('file:///', 'file://user@localhost/'),
        'port': uri.replace('file:///', 'file://localhost:123/'),
        'encoded_authority': uri.replace('file:///', 'file://%6cocalhost/'),
        'fragment': uri + '#ignored',
        'bad_escape': uri + '%GG', 'bad_utf8': uri + '%FF',
        'nul': uri + '%00outside.sqlite3', 'control': uri + '\n',
        'empty_path': 'file:?mode=rw', 'network_path': 'file:////' + str(root).lstrip('/'),
        'vfs': uri + '?vfs=unix-none', 'nolock': uri + '?nolock=1',
        'duplicate_mode': uri + '?mode=ro&mode=rw', 'memory_mode': uri + '?mode=memory',
        'empty_mode': uri + '?mode=', 'malformed_query': uri + '?mode',
        'unknown_query': uri + '?target=' + str(target),
    }[change]
    run(files, guard, changed)


@pytest.mark.parametrize('guard', ['shared', 'http'])
@pytest.mark.parametrize('operation', ['sqlite', 'open', 'mkdir', 'remove'])
def test_plain_outside_write_guard_is_preserved(files, guard, operation):
    root, _, outside = files
    target = outside.parent / 'forbidden-directory' if operation == 'mkdir' else outside
    run(files, guard, target, operation=operation)
    assert not (root.parent / 'forbidden-directory').exists()
