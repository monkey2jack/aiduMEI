"""Legal scope punctuation must not merge, expose, or flush another tenant."""
import pytest

from ducky.speed import coalesce


@pytest.fixture
def queue(monkeypatch, tmp_path):
    from ducky import utils
    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(coalesce, '_coalesce_buf', {})
    monkeypatch.setattr(coalesce, 'load_speed_cfg', lambda: {
        'coalesce_max_parts': 100, 'coalesce_max_chars': 100_000,
        'coalesce_profiles': {'tech': {}, 'infer_off': {}},
    })
    monkeypatch.setattr(coalesce.time, 'time', lambda: 1_700_000_040.0)
    monkeypatch.setattr(coalesce, 'record_coalesce_enqueue', lambda *a, **kw: None)
    monkeypatch.setattr(coalesce, '_record_wave_from_batch', lambda *a, **kw: None)
    return coalesce


COLLISIONS = [
    (('tenant', 'bank@other', ''), ('tenant@bank', 'other', '')),
    (('tenant', 'bank', 'segment'), ('tenant', 'bank::segment', '')),
]


@pytest.mark.parametrize('scopes', COLLISIONS)
@pytest.mark.parametrize('reverse', [False, True])
def test_punctuation_cannot_merge_distinct_scopes(queue, scopes, reverse):
    first, second = scopes[::-1] if reverse else scopes
    for index, (user, bank, session) in enumerate((first, second)):
        queued = queue.coalesce_enqueue(
            user, f'private-marker-{index}', {'session_id': session},
            bank_id=bank, job_id=f'job-{index}',
        )
        assert queued['buffered'] is True
    batches = queue.coalesce_flush_due(force=True)
    assert len(batches) == 2, 'separate domains were combined into one batch'
    indexed = {(batch['user_id'], batch['bank_id']): batch for batch in batches}
    for index, (user, bank, _) in enumerate((first, second)):
        batch = indexed[(user, bank)]
        assert batch['count'] == 1
        assert batch['job_ids'] == [f'job-{index}']
        assert f'private-marker-{index}' in str(batch['messages'])
        assert f'private-marker-{1-index}' not in str(batch['messages'])


def test_bank_filter_does_not_flush_a_bank_with_a_shared_prefix(queue):
    for bank in ('bank', 'bank::child'):
        queue.coalesce_enqueue('tenant', bank, bank_id=bank)
    flushed = queue.coalesce_flush_due(user_id='tenant', bank_id='bank', force=True)
    assert [(batch['user_id'], batch['bank_id']) for batch in flushed] == [('tenant', 'bank')]
    remaining = queue.coalesce_flush_due(force=True)
    assert [batch['bank_id'] for batch in remaining] == ['bank::child']


def test_user_filter_does_not_flush_a_user_with_a_shared_prefix(queue):
    for user in ('tenant', 'tenant@child'):
        queue.coalesce_enqueue(user, user, bank_id='bank')
    flushed = queue.coalesce_flush_due(user_id='tenant', force=True)
    assert [batch['user_id'] for batch in flushed] == ['tenant']
    remaining = queue.coalesce_flush_due(force=True)
    assert [batch['user_id'] for batch in remaining] == ['tenant@child']


def test_status_matches_exact_user_and_reports_bank(queue):
    for user in ('tenant', 'tenant@child'):
        queue.coalesce_enqueue(user, user, bank_id='bank::child')
    status = queue.coalesce_status(user_id='tenant')
    assert status['buffer_count'] == 1
    assert status['buffers'][0]['user_id'] == 'tenant'
    assert status['buffers'][0]['bank_id'] == 'bank::child'


def test_explicit_key_cannot_override_scope_filter(queue):
    queued = queue.coalesce_enqueue('tenant', 'private', bank_id='other')
    assert queue.coalesce_flush_due(
        user_id='tenant', bank_id='bank', key=queued['key'], force=True,
    ) == []
    assert len(queue.coalesce_flush_due(user_id='tenant', bank_id='other', force=True)) == 1


@pytest.mark.parametrize('events', [
    [({'session_id': 'segment::tech'}, True, 'default'),
     ({'session_id': 'segment', 'coalesce_profile': 'tech'}, True, 'tech')],
    [({}, False, 'default'), ({'coalesce_profile': 'infer_off'}, True, 'infer_off')],
])
def test_session_profile_and_infer_axes_cannot_collide(queue, events):
    for index, (metadata, infer, _) in enumerate(events):
        queue.coalesce_enqueue('tenant', f'private-marker-{index}', metadata,
                               bank_id='bank', infer=infer)
    batches = queue.coalesce_flush_due(force=True)
    assert len(batches) == 2
    for index, (_, infer, profile) in enumerate(events):
        batch = next(b for b in batches if f'private-marker-{index}' in str(b['messages']))
        assert batch['count'] == 1
        assert batch['infer'] is infer
        assert batch['profile'] == profile
