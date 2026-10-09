"""Exercise provider writes with real hall authorization and no implicit principal."""
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from test_f0_3_plugin_lifecycle import plugin as plugin, _stub


@pytest.mark.parametrize('bindings', [False, True])
def test_provider_all_writes_explicitly_identify_caller(plugin, monkeypatch, bindings):
    from ducky.security import auth
    from ducky.pantheon import authorize_cross_hall, HallError
    from fastapi import HTTPException
    token='synthetic-plugin-test-token'
    monkeypatch.setenv('AIDUMEM_API_TOKEN', token)
    monkeypatch.setenv('AIDUMEI_CALLER_BINDING_MODE', 'off')
    if bindings:
        # Multiple principals cannot be implicitly inferred from a shared token.
        monkeypatch.setenv('AIDUMEI_CALLER_BINDINGS', json.dumps({auth.fingerprint_token(token):['alice','bob']}))
    else:
        monkeypatch.delenv('AIDUMEI_CALLER_BINDINGS', raising=False)

    def respond(method,path,body):
        scope=body if body is not None else {k:v[0] for k,v in parse_qs(urlsplit(path).query).items()}
        auth.set_request_auth_kind('bearer');auth.set_request_token_fingerprint(token)
        try:
            authorize_cross_hall(scope.get('user_id',''),scope.get('caller_user_id',''),bank_id=scope.get('bank_id'),action='write')
        except (HallError,HTTPException):
            return 403,{'error':'caller_rejected'}
        finally:
            auth.clear_request_auth_kind();auth.clear_request_token_fingerprint()
        if urlsplit(path).path=='/session/distill':
            return 200,{'status':'ok','summary':'Synthetic session summary','user_id':'alice','bank_id':'work'}
        return 200,{'status':'ok','durable':True}

    with _stub(respond) as (url,calls):
        p=plugin.AiduMemProvider({'url':url,'user_id':'alice','bank_id':'work'})
        p._client.base=url
        p._spawn=lambda fn,name:fn()
        # The same network/auth path rejects the old missing-caller payload.
        assert p._client.try_request('POST','/add',body={'messages':'old','user_id':'alice','bank_id':'work'}) is None
        p.initialize('auth-session')
        result=json.loads(p.handle_tool_call('aidumem_remember',{'content':'Synthetic remembered item'}))
        assert result=={'result':'Stored in aiduMEI.'}
        for i in range(3):p.sync_turn('Synthetic useful turn '+str(i),'reply',session_id='auth-session')
        p.on_pre_compress([{'role':'user','content':'Synthetic compressed turn'}])
        p.on_memory_write('replace','user','Synthetic profile')
        p.on_session_end([])
        assert 'auth-session' in p._completed_sessions
    produced=calls[1:]
    bodies=[body for _,path,body in produced if urlsplit(path).path=='/add']
    assert {b['metadata'].get('source') for b in bodies}>={'hermes_tool','hermes_turn','pre_compress'}
    assert any(b['messages']=='Synthetic session summary' for b in bodies)
    assert all(b['caller_user_id']=='alice' for b in bodies)
    for _,path,body in produced:
        scope=body or {k:v[0] for k,v in parse_qs(urlsplit(path).query).items()}
        assert (scope['caller_user_id'],scope['user_id'],scope['bank_id'])==('alice','alice','work')
