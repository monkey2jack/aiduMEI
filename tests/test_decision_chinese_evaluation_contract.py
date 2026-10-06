"""A malformed/failed verifier response must not improve measured accuracy."""
import pytest
from scripts import decision_chinese_compare as c


@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), -.1, 1.1, '0.9', None])
def test_invalid_probability_is_not_a_correct_negative(monkeypatch, bad):
    monkeypatch.setitem(c.decision.PROVIDERS, 'fake',
                        lambda *args: {'answers': {'p0': {'type': 'noul', 'noul': bad}}})
    case = c.protocol()['cases'][1]
    result = c.evaluate(case, 'zh', 'fake', {'provider': 'fake'}, .6, 5000)
    assert result['expected'] is False
    assert result['status'] == 'invalid' and result['correct'] is False
    summary = c.metrics([result])
    assert summary['total'] == 1 and summary['correct'] == 0
    assert summary['negative_total'] == 1 and summary['negative_rejected'] == 0


def test_failed_request_stays_in_positive_denominator(monkeypatch):
    def fail(*args):
        raise TimeoutError('private request contents must not enter receipts')
    monkeypatch.setitem(c.decision.PROVIDERS, 'fake', fail)
    result = c.evaluate(c.protocol()['cases'][0], 'zh', 'fake', {'provider': 'fake'}, .6, 5000)
    summary = c.metrics([result])
    assert summary['positive_total'] == 1 and summary['positive_retained'] == 0
    assert summary['network_or_protocol_errors'] == 1
    assert result['error_type'] == 'TimeoutError' and 'private request' not in str(result)


def test_component_fixture_does_not_pass_gold_to_model(monkeypatch):
    def capture(cfg, state, questions):
        assert set(state) == {'query', 'candidates'}
        assert len(state['candidates']) == 1 and set(state['candidates'][0]) == {'text'}
        assert questions == c.QUESTIONS and 'candidates[0]' in questions['p0']['instructions']
        return {'answers': {'p0': {'type': 'noul', 'noul': .59}}}
    monkeypatch.setitem(c.decision.PROVIDERS, 'fake', capture)
    result = c.evaluate(c.protocol()['cases'][1], 'zh', 'fake', {'provider': 'fake'}, .6, 5000)
    assert result['correct'] and result['predicted'] is False
