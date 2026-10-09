"""Opt-in real stores for writer tests; preserve unrelated suite WAL evidence."""
from types import SimpleNamespace
import threading

import pytest


@pytest.fixture(name="isolated_write_stores")
def isolated_write_stores(tmp_path, monkeypatch):
    """Fresh product schemas, real WAL and durable journal for exactly one test.

    Only storage locations, the connection cache and WAL instance are isolated.
    Scope locks, uncertain-write checks, journal identity and WAL readers remain
    real. Never clear another test's unresolved debt to make a writer pass.
    """
    from ducky import utils, wal_engine, mutation_journal, memory_types
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    from ducky.text_fts import _ensure_trigram_fts
    from ducky.salience.db import ensure_db

    root = tmp_path / 'write-stores'
    root.mkdir()
    for name, filename in (
        ('FACTS_DB', 'facts.db'), ('TEXT_FTS_DB', 'text_fts.db'),
        ('SALIENCE_DB', 'salience.db'), ('OBS_DB', 'observations.db'),
        ('SCENES_DB', 'scenes.db'),
    ):
        monkeypatch.setattr(utils, name, str(root / filename))
    connections = threading.local()
    monkeypatch.setattr(utils, '_thread_local', connections)
    monkeypatch.setattr(memory_types, '_checked', False)
    ledger = wal_engine.WALEngine(str(root / 'wal'))
    monkeypatch.setattr(wal_engine.WALEngine, '_instance', ledger)
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    conn = utils.get_text_conn()
    _ensure_trigram_fts(conn)  # Real schema only; no delayed network backfill.
    conn.commit()
    conn.close()
    ensure_db()
    memory_types.ensure_memory_types_schema()
    mutation_journal.initialize_journal()
    try:
        yield SimpleNamespace(root=root, wal=ledger)
    finally:
        # Close only our private real SQLite connections. Monkeypatch restores
        # the caller's cache, paths and original WAL singleton unchanged.
        for name, conn in vars(connections).items():
            if name.startswith('conn_'):
                conn.close()
