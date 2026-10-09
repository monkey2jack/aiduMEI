"""Actual SDK + HTTP regression for inner fallback, not a fixed-dict SDK fake."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROBE = Path(__file__).with_name('f04_inner_durability_probe.py')


def probe(root, mode):
    env = {**os.environ, 'PYTHONPATH': str(PROBE.parent.parent), 'MEM0_TELEMETRY': 'false'}
    out = subprocess.run([sys.executable, str(PROBE), str(root), mode], env=env,
                         capture_output=True, text=True, timeout=35)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads((root / (mode + '.json')).read_text())


def sdk_rows(out):
    return [row for row in out['rows'] if row['kind'].startswith('mem0_')]


@pytest.mark.parametrize('mode', [
    'dedup_after_ack', 'dedup_after_ack_uncertain', 'dedup_fts', 'dedup_sidecar',
    'selfedit_after_ack', 'selfedit_fts', 'selfedit_sidecar_uncertain',
    'selfedit_ledger_sql', 'selfedit_ledger_after_commit', 'speed_after_ack',
])
def test_acknowledged_update_then_inner_failure_never_adds_again(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['http'] == out['retry']['http'] == 409, out
    assert out['response']['detail']['status'] == 'repair_required'
    assert out['response']['detail']['automatic_replay'] is False
    assert out['sdk_after_first'] == out['sdk_after_retry'] == 2
    assert [row['kind'] for row in sdk_rows(out)] == ['mem0_add', 'mem0_update']
    assert all(row['state'] == 'acknowledged' for row in sdk_rows(out))
    assert len(out['vectors']) == 1 and out['vectors'][0]['id'] == out['seed_target']
    assert out['health']['repair_required'] == 1
    if mode == 'selfedit_ledger_sql':
        assert out['edit_count'] == 0
    elif mode == 'selfedit_ledger_after_commit':
        assert out['edit_count'] == 1


@pytest.mark.parametrize('mode', ['legacy_detect_fallback', 'legacy_dedup_fallback',
                                 'legacy_direct_fallback'])
def test_legacy_fallback_success_requires_no_prior_sdk_effect_in_attempt(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['http'] == out['retry']['http'] == 200, out
    assert out['retry']['body']['idempotency_replayed'] is True
    assert out['sdk_after_first'] == out['sdk_after_retry'] == 2
    rows = sdk_rows(out)
    assert all(row['state'] == 'acknowledged' for row in rows)
    if mode == 'legacy_detect_fallback':
        assert out['sdk_before_llm'] == out['seed_sdk_count'] == 1
        assert rows[-1]['kind'] == 'mem0_update'
        assert out['response']['action'] == 'updated'
    else:
        assert out['sdk_at_update_preflight'] == out['seed_sdk_count'] == 1
        assert rows[-1]['kind'] == 'mem0_add'
        if mode == 'legacy_dedup_fallback':
            assert out['response']['action'] == 'new'
            assert out['response']['details']['dedup_update_failed']
        else:
            assert out['response']['action'] == 'direct'
    assert out['health']['repair_required'] == 0


@pytest.mark.parametrize('mode', ['dedup_search_uncertain', 'selfedit_snapshot_uncertain',
                                 'evolution_sql', 'scope_debt'])
def test_scope_uncertainty_or_sql_failure_stops_before_next_sdk_call(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['http'] == out['retry']['http'] == 409, out
    assert out['sdk_after_first'] == out['sdk_after_retry'] == 1
    assert len(out['vectors']) == 1
    assert out['health']['repair_required'] >= 1


@pytest.mark.parametrize('mode', [
    'new_fts', 'new_episode_uncertain', 'new_epistemic', 'new_salience',
    'new_salience_uncertain', 'new_type_fts', 'new_type_payload_uncertain',
    'direct_after_ack_llm', 'direct_fts', 'direct_salience', 'direct_epistemic',
    'direct_episode', 'direct_type_fts', 'direct_type_payload_uncertain',
])
def test_new_add_sidecar_failure_is_not_swallowed_or_redispatched(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['http'] == out['retry']['http'] == 409, out
    assert out['sdk_after_first'] == out['sdk_after_retry'] == 2
    assert [row['kind'] for row in sdk_rows(out)] == ['mem0_add', 'mem0_add']
    assert all(row['state'] == 'acknowledged' for row in sdk_rows(out))
    assert len(out['vectors']) == 2 and out['health']['repair_required'] == 1


@pytest.mark.parametrize('mode', ['rollback_fts', 'rollback_salience', 'rollback_salience_uncertain'])
def test_rollback_after_ack_sidecar_failure_propagates_without_claiming_undone(tmp_path, mode):
    out = probe(tmp_path, mode)
    assert out['error_type'] == ('MutationUncertain' if mode.endswith('_uncertain')
                                 else 'OperationalError')
    assert out['undone'] == 0
    assert [row['kind'] for row in sdk_rows(out)] == ['mem0_add', 'mem0_update']
    assert all(row['state'] == 'acknowledged' for row in sdk_rows(out))
    assert len(out['vectors']) == 1 and out['vectors'][0]['memory'] == out['restored']
