"""Isolated child-process crash probe; never uses network or production data."""
import json
import os
from pathlib import Path
import socket
import sys

from f04_probe_guard import write_audit


def no_network(*args, **kwargs):
    raise AssertionError('external network forbidden in durability tests')


def offline_memory(root):
    os.environ['MEM0_TELEMETRY'] = 'false'
    os.environ['MEM0_DIR'] = str(Path(root) / 'sdk-state')
    for key in list(os.environ):
        if key.lower().endswith('_proxy'):
            os.environ.pop(key)
    socket.socket.connect = no_network
    socket.getaddrinfo = no_network
    from mem0 import Memory
    from ducky.mutation_journal import install_memory_journal
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    memory = Memory.from_config({
        'version': 'v1.1',
        'llm': {'provider': 'openai', 'config': {'api_key': 'synthetic-offline-test', 'model': 'gpt-4o-mini'}},
        'embedder': {'provider': 'openai', 'config': {
            'api_key': 'synthetic-offline-test', 'model': 'text-embedding-3-small', 'embedding_dims': 4}},
        'vector_store': {'provider': 'qdrant', 'config': {
            'path': str(root / 'vectors'), 'collection_name': 'durability_synthetic', 'embedding_model_dims': 4}},
        'history_db_path': str(root / 'sdk_history.sqlite3'),
    })
    # Only the external embedding provider is replaced; actual SDK extraction
    # bypass, vector writes, get/update and history transactions remain real.
    memory.embedding_model.embed = lambda text, *args, **kwargs: [1.0, 0.0, 0.0, 0.0]
    return install_memory_journal(memory)


def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    os.environ.update(AIDUMEM_DATA_DIR=str(root / 'data'), AIDUMEM_LOG_DIR=str(root / 'logs'),
                      MEM0_TELEMETRY='false', MEM0_DIR=str(root / 'sdk-state'),
                      HF_HOME=str(root / 'hf-cache'), XDG_CACHE_HOME=str(root / 'cache'),
                      TMPDIR=str(root / 'tmp'))
    (root / 'tmp').mkdir(exist_ok=True)
    sys.dont_write_bytecode = True
    real_makedirs = os.makedirs
    def isolated_makedirs(path, mode=0o777, exist_ok=False):
        if str(path) == '/tmp/qdrant':
            path = root / 'unused-sdk-default-qdrant'
        return real_makedirs(path, mode=mode, exist_ok=exist_ok)
    os.makedirs = isolated_makedirs
    sys.addaudithook(write_audit(root, 'synthetic root'))
    from ducky import mutation_journal as journal
    if mode in ('add_exit', 'update_exit', 'ack_exit'):
        memory = offline_memory(root / 'sdk')
        if mode == 'update_exit':
            results = memory.get_all(filters={'user_id': 'synthetic-owner'})['results']
            target = results[0]['id']
            original = memory.update.__wrapped__
            def after_effect(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(71)
            # Preserve actual SDK signature when wrapping the fault injector.
            import functools
            memory.update = functools.wraps(original)(after_effect)
            journal.install_memory_journal(memory)
            memory.update(target, 'synthetic updated secret')
        elif mode == 'add_exit':
            original = memory.add.__wrapped__
            def after_effect(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(71)
            import functools
            memory.add = functools.wraps(original)(after_effect)
            journal.install_memory_journal(memory)
            memory.add('synthetic durable secret', user_id='synthetic-owner',
                       metadata={'bank_id': 'work'}, infer=False)
        else:
            result = memory.add('synthetic durable secret', user_id='synthetic-owner',
                                metadata={'bank_id': 'work'}, infer=False)
            (root / 'sdk-result.json').write_text(json.dumps(result))
            os._exit(72)
    elif mode == 'job_exit':
        from ducky.speed.jobs import job_create
        jid = job_create({'user_id': 'synthetic-owner', 'bank_id': 'work',
                          'messages': [{'role': 'user', 'content': 'original complete synthetic text'}],
                          'metadata': {'session_id': 'synthetic-session'}, 'infer': False})
        (root / 'job-id').write_text(jid)
        os._exit(73)
    elif mode == 'coalesce_exit':
        from ducky.speed.coalesce import coalesce_enqueue
        out = coalesce_enqueue('synthetic-owner', [{'role': 'user', 'content': 'coalesced complete synthetic text'}],
                               {'no_fastpath': True}, bank_id='work', infer=False)
        (root / 'coalesce.json').write_text(json.dumps(out))
        os._exit(74)
    elif mode == 'inspect_sdk':
        memory = offline_memory(root / 'sdk')
        rows = memory.get_all(filters={'user_id': 'synthetic-owner'})['results']
        (root / 'sdk-records.json').write_text(json.dumps(rows))
        (root / 'recovery.json').write_text(json.dumps(journal.startup_recover()))
        memory.vector_store.client.close()
    elif mode == 'recover':
        (root / 'recovery.json').write_text(json.dumps(journal.startup_recover()))
    else:
        raise ValueError(mode)


if __name__ == '__main__':
    main()
