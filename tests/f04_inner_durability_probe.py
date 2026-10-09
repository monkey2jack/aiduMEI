"""Offline process probe for internal post-SDK errors and safe fallback evidence."""
from __future__ import annotations

import functools
import json
from pathlib import Path
import sqlite3
import sys

from f04_durability_http_probe import BODY, build_app, dump, isolate, records


def sdk_rows():
    return [row for row in records() if row['kind'].startswith('mem0_')]


def after_ack_update(memory, uncertain=False):
    from ducky.mutation_journal import MutationUncertain
    original = memory.update
    @functools.wraps(original)
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        row = sdk_rows()[-1]
        assert row['kind'] == 'mem0_update' and row['state'] == 'acknowledged', result
        if uncertain:
            raise MutationUncertain(row['id'], 'synthetic_post_ack_uncertain')
        raise RuntimeError('synthetic failure after actual SDK ACK')
    memory.update = wrapped


def fail_after_call(module, name, uncertain=False):
    from ducky.mutation_journal import MutationUncertain
    original = getattr(module, name)
    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        row = sdk_rows()[-1]
        assert row['state'] == 'acknowledged', result
        if uncertain:
            raise MutationUncertain(row['id'], 'synthetic_sidecar_uncertain')
        raise sqlite3.OperationalError('synthetic sidecar failure after real commit')
    setattr(module, name, wrapped)


def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    isolate(root)
    client, memory = build_app(root)
    from ducky import self_edit, mutation_journal as journal
    from ducky import text_fts, utils
    from ducky.hot import add
    from ducky.mutation_fallback import sdk_attempt_count
    body = json.loads(json.dumps(BODY))
    seeded = client.post('/add', json=body)
    assert seeded.status_code == 200, seeded.text
    seed_count = len(sdk_rows())
    target = memory.get_all(filters={'user_id': body['user_id']})['results'][0]['id']
    body['idempotency_key'] = 'inner-followup-key'
    extra = {'seed_sdk_count': seed_count, 'seed_target': target}
    if mode.startswith('rollback_'):
        restored = 'synthetic previous content restored by rollback'
        eid = self_edit._log_edit(target, body['user_id'], 'duplicate', restored,
                                  body['messages'][0]['content'], 'synthetic snapshot', .99)
        if mode == 'rollback_fts':
            fail_after_call(text_fts, '_index_memory')
        else:
            from ducky.salience import core
            fail_after_call(core, 'on_memory_added', uncertain=mode.endswith('_uncertain'))
        try:
            self_edit.rollback_edit(eid, memory=memory, caller_user_id=body['user_id'])
        except (sqlite3.OperationalError, journal.MutationUncertain) as exc:
            extra['error_type'] = type(exc).__name__
        else:
            raise AssertionError('rollback silently returned success after sidecar failure')
        with sqlite3.connect(utils.FACTS_DB) as conn:
            extra['undone'] = conn.execute('SELECT undone FROM memory_edits WHERE edit_id=?',
                                           (eid,)).fetchone()[0]
        extra['restored'] = restored
        dump(root, mode, client, memory, extra=extra)
        return
    if mode.startswith('direct_') or mode == 'legacy_direct_fallback':
        from ducky import gear
        gear.should_try_llm = lambda: False
        body['infer'] = True
        if mode == 'direct_after_ack_llm' or mode == 'legacy_direct_fallback':
            original_add = memory.add
            class LLMError(RuntimeError):
                pass
            def guarded_add(*args, **kwargs):
                if mode == 'direct_after_ack_llm':
                    result = original_add(*args, **kwargs)
                    assert sdk_rows()[-1]['state'] == 'acknowledged', result
                    raise LLMError('synthetic adapter failure after real add ACK')
                if not extra.get('preflight_refused'):
                    extra['sdk_at_update_preflight'] = len(sdk_rows())
                    extra['preflight_refused'] = True
                    raise LLMError('synthetic adapter refusal before SDK dispatch')
                return original_add(*args, **kwargs)
            memory.add = guarded_add
    if mode in ('new_salience', 'new_salience_uncertain', 'direct_salience'):
        from ducky import mem0_runtime
        fail_after_call(mem0_runtime, 'on_memory_added', uncertain=mode.endswith('_uncertain'))
    if mode in ('new_type_fts', 'direct_type_fts'):
        fail_after_call(text_fts, '_set_memory_type')
    if mode in ('new_type_payload_uncertain', 'direct_type_payload_uncertain'):
        fail_after_call(memory.vector_store.client, 'set_payload', uncertain=True)
    if mode == 'direct_fts':
        fail_after_call(text_fts, '_index_memory')
    if mode == 'direct_epistemic':
        from ducky import epistemic
        fail_after_call(epistemic, 'stamp_memory_refs')
    if mode == 'direct_episode':
        from ducky import evolve_mem
        body['metadata']['_origin_session_id'] = 'synthetic-direct-session'
        fail_after_call(evolve_mem, 'record_episode_step')
    if mode.startswith('selfedit') or mode == 'legacy_detect_fallback':
        body['infer'] = True
        # Only the external LLM provider is supplied offline. Actual candidate
        # search, verdict validation, snapshot, SDK update and ledger stay real.
        def verdict(*args, **kwargs):
            extra['sdk_before_llm'] = sdk_attempt_count(body['user_id'], body['bank_id'])
            if mode == 'legacy_detect_fallback':
                raise RuntimeError('synthetic LLM unavailable before SDK write')
            return json.dumps({'decision': 'duplicate', 'memory_id': target,
                               'merged_content': 'synthetic durable route input verified by self edit',
                               'confidence': .99, 'reason': 'same synthetic fact'})
        self_edit.call_llm = verdict
    if mode in ('dedup_after_ack', 'dedup_after_ack_uncertain', 'selfedit_after_ack', 'speed_after_ack'):
        after_ack_update(memory, uncertain=mode.endswith('_uncertain'))
    if mode == 'speed_after_ack':
        from ducky.speed.pipeline import run_add_pipeline
        def speed(memory, messages, uid, metadata, *, bank_id, infer):
            return run_add_pipeline(memory, messages, uid, metadata, bank_id=bank_id)
        add.lazy_import_layer1 = lambda: speed
    if mode in ('dedup_fts', 'selfedit_fts'):
        fail_after_call(text_fts, '_index_memory')
    if mode in ('dedup_sidecar', 'selfedit_sidecar_uncertain'):
        from ducky.salience import core
        fail_after_call(core, 'on_memory_added', uncertain=mode.endswith('_uncertain'))
    if mode in ('selfedit_ledger_sql', 'selfedit_ledger_after_commit'):
        if mode.endswith('_sql'):
            self_edit.ensure_self_edit_schema()
            with sqlite3.connect(utils.FACTS_DB) as conn:
                conn.execute("CREATE TRIGGER reject_edit BEFORE INSERT ON memory_edits "
                             "BEGIN SELECT RAISE(ABORT,'synthetic ledger transaction failure'); END")
        else:
            fail_after_call(self_edit, '_log_edit')
    if mode == 'evolution_sql':
        body['messages'][0]['content'] += ' compared with additional weekly context'
        with sqlite3.connect(utils.FACTS_DB) as conn:
            conn.execute("CREATE TRIGGER reject_evolution BEFORE INSERT ON knowledge_evolution "
                         "BEGIN SELECT RAISE(ABORT,'synthetic evolution transaction failure'); END")
    if mode == 'legacy_dedup_fallback':
        original = memory.update
        def preflight(*args, **kwargs):
            extra['sdk_at_update_preflight'] = sdk_attempt_count(body['user_id'], body['bank_id'])
            # Explicit pre-dispatch failure: do not call the journalled SDK.
            raise ValueError('synthetic legacy adapter refuses before SDK dispatch')
        memory.update = functools.wraps(original)(preflight)
    if mode in ('dedup_search_uncertain', 'selfedit_snapshot_uncertain'):
        def fail(*args, **kwargs):
            raise journal.MutationUncertain('synthetic-scope-fence', 'scope_blocked')
        if mode.startswith('dedup'):
            memory.search = fail
        else:
            memory.get = fail
    if mode == 'scope_debt':
        mid = journal.accept_job({'user_id': body['user_id'], 'bank_id': body['bank_id'],
                                  'messages': 'synthetic prior debt', 'metadata': {}, 'infer': False})
        journal.update_job(mid, status='error')
    if mode.startswith('new_'):
        body['messages'][0]['content'] = 'a completely different synthetic memory about blue whales'
        if mode == 'new_fts':
            fail_after_call(text_fts, '_index_memory')
        elif mode == 'new_episode_uncertain':
            from ducky import evolve_mem
            body['metadata']['_origin_session_id'] = 'synthetic-episode-session'
            fail_after_call(evolve_mem, 'record_episode_step', uncertain=True)
        elif mode == 'new_epistemic':
            from ducky import epistemic
            fail_after_call(epistemic, 'stamp_memory_refs')
    response = client.post('/add', json=body)
    extra['first_response'] = {'http': response.status_code, 'body': response.json()}
    extra['sdk_after_first'] = len(sdk_rows())
    # Restore faults only by process exit. Key retry must be answered from the
    # journal before any of the failing callbacks can execute again.
    again = client.post('/add', json=body)
    extra['retry'] = {'http': again.status_code, 'body': again.json()}
    extra['sdk_after_retry'] = len(sdk_rows())
    with sqlite3.connect(utils.FACTS_DB) as conn:
        if conn.execute("SELECT name FROM sqlite_master WHERE name='memory_edits'").fetchone():
            extra['edit_count'] = conn.execute('SELECT COUNT(*) FROM memory_edits').fetchone()[0]
    dump(root, mode, client, memory, response, extra)


if __name__ == '__main__':
    main()
