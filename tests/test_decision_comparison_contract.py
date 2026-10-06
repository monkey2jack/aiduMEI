"""Measurement regressions: missing evidence cannot improve reported accuracy."""
import pytest
from scripts import decision_compare as c


@pytest.mark.parametrize('runner,case_name', [('_run_choice','MEMORY_CASES'),('_run_noul','RETRIEVAL_CASES'),('_run_score','SCORE_CASES')])
def test_transport_failures_stay_in_accuracy_denominator(monkeypatch,runner,case_name):
    monkeypatch.setattr(c,'_call',lambda *args:(None,12.0,'TimeoutError'))
    result=getattr(c,runner)({},2)
    assert result['total']==2*len(getattr(c,case_name))
    assert result['unique_cases']==len(getattr(c,case_name))
    assert result['correct']==0 and result['usable_answers']==0 and result['accuracy']==0
    if runner=='_run_noul':
        assert result['negative_total']==2*sum(not x[3] for x in c.RETRIEVAL_CASES)
        assert result['correct_reject_rate']==0


@pytest.mark.parametrize('bad', [True,float('nan'),float('inf'),-.01,1.01,'0.9',None])
def test_invalid_support_answers_do_not_disappear(monkeypatch,bad):
    monkeypatch.setattr(c,'RETRIEVAL_CASES',[('x','q','d',False)])
    monkeypatch.setattr(c,'_call',lambda *args:({'answers':{'x':{'type':'noul','noul':bad}}},1,None))
    result=c._run_noul({},1)
    assert result['total']==result['negative_total']==1
    assert result['correct']==result['usable_answers']==result['correct_reject']==0


def test_support_threshold_matches_product_policy(monkeypatch):
    monkeypatch.setattr(c,'RETRIEVAL_CASES',[('x','q','d',False)])
    monkeypatch.setattr(c,'_call',lambda *args:({'answers':{'x':{'type':'noul','noul':.55}}},1,None))
    result=c._run_noul({},1)
    assert result['threshold']==.6 and result['correct']==1


@pytest.mark.parametrize('runner,case_name', [('_run_choice','MEMORY_CASES'),('_run_noul','RETRIEVAL_CASES'),('_run_score','SCORE_CASES')])
def test_questions_reference_their_own_state_item(monkeypatch,runner,case_name):
    def capture(cfg,state,questions):
        for i,item in enumerate(state['items']):
            instruction=questions[item['id']]['instructions']
            assert f'items[{i}]' in instruction and f"id={item['id']}" in instruction
        return {'answers':{}},1,None
    monkeypatch.setattr(c,'_call',capture)
    result=getattr(c,runner)({},1)
    assert result['total']==len(getattr(c,case_name)) and result['usable_answers']==0


@pytest.mark.parametrize('bad', [True,float('nan'),-1,3,'2',None])
def test_invalid_score_answers_stay_in_denominator(monkeypatch,bad):
    monkeypatch.setattr(c,'SCORE_CASES',[('x','q','d',2)])
    monkeypatch.setattr(c,'_call',lambda *args:({'answers':{'x':{'type':'score','score':bad}}},1,None))
    result=c._run_score({},1)
    assert result['total']==1 and result['correct']==result['usable_answers']==0


def test_bad_cloudflare_account_id_is_rejected_before_network(monkeypatch):
    monkeypatch.setattr(c,'_provider',lambda *args:pytest.fail('malformed ID reached transport'))
    data,_,error=c._call({'provider':'cloudflare','model':'clef','account_id':'0'*33,
                          'openai_base_url':'https://api.cloudflare.com/client/v4','api_key':'synthetic'}, {}, {})
    assert data is None and error=='ValueError'
