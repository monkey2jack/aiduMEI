"""Filesystem write boundary shared by isolated durability subprocess probes."""
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, unquote, urlsplit


def sqlite_target(value):
    """Decode only the local SQLite URI forms needed by these probes.

    SQLite's audit event omits connect(uri=...), so never infer safety from
    mode=ro. All decoded targets still pass the same filesystem boundary.
    Reject VFS/locking overrides and malformed URI forms rather than guessing.
    """
    raw = os.fsdecode(value)
    if not raw.startswith('file:'):
        return raw
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise AssertionError('invalid SQLite URI control character')
    if re.search(r'%(?![0-9A-Fa-f]{2})', raw):
        raise AssertionError('invalid SQLite URI percent escape')
    try:
        parsed = urlsplit(raw)
        if parsed.netloc not in ('', 'localhost') or parsed.fragment:
            raise ValueError('non-local authority or fragment')
        target = unquote(parsed.path, errors='strict')
        if (not target.startswith('/') or target.startswith('//')
                or any(ord(char) < 32 or ord(char) == 127 for char in target)):
            raise ValueError('invalid absolute path')
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                          errors='strict', max_num_fields=1)
        if query and (query[0][0] != 'mode' or query[0][1] not in ('ro', 'rw', 'rwc')):
            raise ValueError('unsupported query')
    except (ValueError, UnicodeError) as exc:
        raise AssertionError('invalid SQLite URI') from exc
    return target


def write_audit(root, label):
    """Create an audit callback without installing a permanent parent hook."""
    root = Path(root).resolve()

    def audit(event, args):
        path = None
        if event == 'open':
            candidate, mode, flags = args
            if isinstance(candidate, (str, bytes, os.PathLike)) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                path = candidate
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'sqlite3.connect'):
            path = args[0]
        if event == 'sqlite3.connect':
            path = sqlite_target(path)
            if path == ':memory:':
                return
        if path is not None and not Path(os.fsdecode(path)).is_absolute() and event.startswith('os.'):
            directory_fd = args[-1]
            if isinstance(directory_fd, int) and directory_fd != -1:
                if sys.platform == 'darwin':
                    import fcntl
                    directory = fcntl.fcntl(directory_fd, 50, b'\0' * 1024).split(b'\0')[0].decode()
                else:
                    directory = os.readlink(f'/proc/self/fd/{directory_fd}')
                path = Path(directory) / os.fsdecode(path)
        if path is not None and not Path(os.fsdecode(path)).resolve().is_relative_to(root):
            raise AssertionError(f'write outside {label}: {event} {path}')

    return audit
