"""Production route boundary regressions using real SQLite and offline mem0.

All tests spawn fresh processes with denied networking and a filesystem-write
allowlist restricted to their synthetic root. No fixed-dict replacement for
SDK, FastAPI routing, ingress stores, journal or idempotency.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROBE = Path(__file__).with_name('f04_durability_http_probe.py')


def probe(root, mode, code=0):
    env = {**os.environ, 'PYTHONPATH': str(PROBE.parent.parent), 'MEM0_TELEMETRY': 'false'}
    out = subprocess.run([sys.executable, str(PROBE), str(root), mode], env=env,
                         capture_output=True, text=True, timeout=35)
    assert out.returncode == code, out.stdout + out.stderr
    path = root / (mode + '.json')
    return json.loads(path.read_text()) if path.exists() else None


def sdk_rows(out):
    return [row for row in out['rows'] if row['kind'].startswith('mem0_')]


def test_sync_success_replays_from_durable_receipt_after_real_restart(tmp_path):
    initial = probe(tmp_path, 'sync_ok')
    assert initial['http'] == 200, initial
    assert len(initial['vectors']) == 1
    repeated = probe(tmp_path, 'retry')
    assert repeated['http'] == 200 and repeated['response']['idempotency_replayed']
    assert len(sdk_rows(repeated)) == 1
    assert repeated['raw'] == initial['raw']


@pytest.mark.parametrize('mode', ['sync_ack_fail', 'sdk_ack_fail', 'local_ack_fail', 'lite_ack_fail'])
def test_ack_failure_is_not_success_and_never_retries_side_effects(tmp_path, mode):
    initial = probe(tmp_path, mode)
    assert initial['http'] == 409, initial
    assert initial['response']['detail']['status'] == 'repair_required'
    assert initial['response']['detail']['automatic_replay'] is False
    assert initial['health']['status'] == 'degraded'
    repeated = probe(tmp_path, 'retry')
    assert repeated['http'] == 409
    assert len(sdk_rows(repeated)) == len(sdk_rows(initial))
    assert repeated['raw'] == initial['raw']
    assert len(initial['vectors']) == (0 if mode.startswith(('local', 'lite')) else 1)


def test_parent_ack_process_death_does_not_replay_acknowledged_sdk(tmp_path):
    probe(tmp_path, 'sync_ack_exit', 81)
    out = probe(tmp_path, 'retry')
    assert out['http'] == 409 and len(out['vectors']) == 1
    assert len(sdk_rows(out)) == 1 and sdk_rows(out)[0]['state'] == 'acknowledged'
    assert out['recovery']['held_for_repair'] == 1
    assert out['raw'][0][1] == 1


@pytest.mark.parametrize('mode', ['intent_fail', 'async_intent_fail'])
def test_intent_failure_prevents_all_raw_and_sdk_side_effects(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['http'] == 500
    assert out['rows'] == out['raw'] == out['vectors'] == []


@pytest.mark.parametrize(('mode', 'retry'), [('async_accept_exit', 'retry_async'),
                                            ('coalesce_accept_exit', 'retry_coalesce')])
def test_accepted_full_input_survives_process_exit_without_blind_replay(tmp_path, mode, retry):
    initial = probe(tmp_path, mode, 82)
    assert initial['http'] == 200, initial
    assert initial['response']['accepted_durable'] is True
    assert initial['response']['durable'] is False
    job, = initial['rows']
    payload = json.loads(job['input_json'])
    assert payload['messages'][0]['content'] == 'synthetic durable route input'
    assert payload['original_request']['messages'] == payload['messages']
    assert payload['metadata']['bank_id'] == 'work' and payload['infer'] is False
    out = probe(tmp_path, retry)
    assert out['http'] == 409 and not out['vectors']
    assert out['rows'][0]['state'] == 'repair_required'
    assert out['health']['failed_durable_jobs'] == 1


def test_async_completion_ack_failure_holds_job_and_key_for_repair(tmp_path):
    initial = probe(tmp_path, 'async_done_fail')
    assert initial['http'] == 200 and initial['response']['status'] == 'accepted'
    assert initial['health']['failed_durable_jobs'] == 1
    out = probe(tmp_path, 'retry_async')
    assert out['http'] == 409 and len(out['vectors']) == 1
    assert len(sdk_rows(out)) == 1


@pytest.mark.parametrize('mode', ['post_sdk_error', 'post_sdk_llm_error'])
def test_post_sdk_failures_never_enter_direct_fallback(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['http'] == 409, out
    assert len(sdk_rows(out)) == len(out['vectors']) == 1
    assert sdk_rows(out)[0]['state'] == 'acknowledged'


def test_update_uncertain_is_explicit_and_actual_update_is_preserved(tmp_path):
    out = probe(tmp_path, 'update_ack_fail')
    assert out['http'] == 409, out
    assert out['response']['detail']['status'] == 'repair_required'
    assert out['vectors'][0]['memory'] == 'synthetic changed content'
    assert [row['state'] for row in sdk_rows(out)] == ['acknowledged', 'repair_required']


def test_stale_coalesce_callback_cannot_resurrect_forgotten_request(tmp_path):
    out = probe(tmp_path, 'forgotten_callback')
    assert out['http'] == 200 and out['stale_callback_blocked']
    assert out['vectors'] == [] and out['rows'][0]['state'] == 'forgotten'
    assert out['rows'][0]['input_json'] is None


@pytest.mark.parametrize('phase', ['running', 'callback', 'done'])
def test_cross_process_recovery_waits_for_entire_async_execution(tmp_path, phase):
    out = probe(tmp_path, 'guard_' + phase)
    assert out['http'] == 200 and out['recovery_blocked'] is True
    assert all(row['state'] == 'acknowledged' for row in out['rows'])
    assert out['health']['failed_durable_jobs'] == 0
    assert len(sdk_rows(out)) == 1


def repair(root, *args, code=0, owner='synthetic-owner', bank='work', wrapper=False):
    entry = [str(PROBE.parent.parent / 'scripts' / 'mutation_repair.py')] if wrapper else [
        '-m', 'ducky.mutation_repair']
    env = {**os.environ, 'PYTHONPATH': str(PROBE.parent.parent),
           'AIDUMEM_DATA_DIR': str(root / 'data'), 'AIDUMEM_LOG_DIR': str(root / 'logs')}
    proc = subprocess.run([sys.executable, *entry, '--data-dir', str(root / 'data'),
                           '--owner', owner, '--bank-id', bank, *args],
                          env=env, capture_output=True, text=True, timeout=15)
    assert proc.returncode == code, proc.stdout + proc.stderr
    return proc


def test_repair_cli_exact_scope_evidence_and_explicit_resolution(tmp_path):
    initial = probe(tmp_path, 'sync_ack_fail')
    listed = json.loads(repair(tmp_path, 'list').stdout)['mutations']
    mid, = [row['id'] for row in listed]
    hidden = repair(tmp_path, 'inspect', '--mutation-id', mid, owner='other', code=3)
    assert mid not in hidden.stderr and 'synthetic durable route input' not in hidden.stderr
    assert json.loads(repair(tmp_path, 'list', bank='other').stdout)['mutations'] == []
    preview = repair(tmp_path, 'inspect', '--mutation-id', mid, wrapper=True)
    assert 'synthetic durable route input' not in preview.stdout
    payload = repair(tmp_path, 'inspect', '--mutation-id', mid, '--include-payload')
    assert json.loads(payload.stdout)['input']['messages'][0]['content'] == 'synthetic durable route input'
    repair(tmp_path, 'resolve', '--mutation-id', mid, '--resolution', 'confirmed_applied', code=2)
    evidence = tmp_path / 'evidence.txt'
    evidence.write_text('Operator compared real vector and raw store; no missing legs.')
    resolved = repair(tmp_path, 'resolve', '--mutation-id', mid, '--resolution', 'confirmed_applied',
                      '--evidence-file', str(evidence))
    assert json.loads(resolved.stdout)['automatic_replay'] is False
    assert evidence.read_text() not in resolved.stdout
    repeated = probe(tmp_path, 'retry')
    assert repeated['http'] == 200 and repeated['response']['action'] == 'operator_verified'
    assert len(sdk_rows(repeated)) == 1 and repeated['raw'] == initial['raw']
    repair(tmp_path, 'resolve', '--mutation-id', mid, '--resolution', 'confirmed_applied',
           '--evidence', 'cannot resolve terminal state again', code=2)


def test_repair_cli_refuses_live_api_process(tmp_path):
    probe(tmp_path, 'sync_ack_fail')
    code = ("from ducky.process_lock import acquire_api_process_lock; from pathlib import Path; "
            "import sys; acquire_api_process_lock(sys.argv[1]); "
            "Path(sys.argv[2]).touch(); sys.stdin.read()")
    ready = tmp_path / 'live-ready'
    env = {**os.environ, 'PYTHONPATH': str(PROBE.parent.parent)}
    proc = subprocess.Popen([sys.executable, '-c', code, str(tmp_path / 'data'), str(ready)],
                            env=env, stdin=subprocess.PIPE)
    import time
    try:
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:
            time.sleep(.01)
        assert ready.exists()
        out = repair(tmp_path, 'list', code=2)
        assert json.loads(out.stderr)['status'] == 'refused'
    finally:
        proc.communicate(timeout=5)


def test_repair_cli_rejects_empty_evidence_without_replay(tmp_path):
    probe(tmp_path, 'async_accept_exit', 82)
    initial = probe(tmp_path, 'retry_async')
    mid = initial['rows'][0]['id']
    evidence = tmp_path / 'empty-evidence'
    evidence.write_text(' \n')
    repair(tmp_path, 'resolve', '--mutation-id', mid, '--resolution', 'confirmed_not_applied',
           '--evidence-file', str(evidence), code=2)
    assert json.loads(repair(tmp_path, 'list').stdout)['mutations'][0]['id'] == mid
    out = repair(tmp_path, 'resolve', '--mutation-id', mid, '--resolution', 'confirmed_not_applied',
                 '--evidence', 'Verified all backend and SQL side effects absent or compensated')
    assert json.loads(out.stdout) == {'id': mid, 'state': 'not_applied', 'automatic_replay': False}
