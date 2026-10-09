"""Independent inventory + actual HTTP denial, not a signature/marker census.

The checked-in inventory was taken from the previously released route surface.
Every new route needs an explicit resource review. OpenAPI is used only to make
well-formed requests, never to decide whether a route needs authorization.
"""
import json
from pathlib import Path

import pytest

pytest_plugins = ["test_f04_auth_boundaries"]

INVENTORY = json.loads((Path(__file__).parent/'fixtures/http_resource_census.json').read_text())
PRIVATE = [x for x in INVENTORY if x['resource'] == 'memory-or-instance-state']


def test_complete_independent_route_inventory():
    import api_server
    from fastapi.routing import APIRoute
    actual = {(name, method, r.path) for name, app in [('root',api_server.app),('alias',api_server._api_alias)]
              for r in app.routes if isinstance(r, APIRoute) for method in r.methods}
    expected = {(x['app'],x['method'],x['path']) for x in INVENTORY}
    assert actual == expected, (actual-expected, expected-actual)
    assert len(PRIVATE) >= 280


def value(schema, document):
    if '$ref' in schema:
        return value(document['components']['schemas'][schema['$ref'].split('/')[-1]], document)
    if 'anyOf' in schema:
        return value(next(x for x in schema['anyOf'] if x.get('type') != 'null'), document)
    if 'enum' in schema:
        return schema['enum'][0]
    kind = schema.get('type')
    if kind == 'object':
        return {key: value(schema['properties'][key], document) for key in schema.get('required', [])}
    if kind == 'array':
        return [value(schema.get('items', {}), document)]
    if kind == 'boolean':
        return False
    if kind in ('number','integer'):
        return max(1, schema.get('minimum',1))
    return 'synthetic'


@pytest.mark.parametrize('entry', PRIVATE, ids=lambda x: x['app']+':'+x['method']+':'+x['path'])
def test_unregistered_token_denied_on_every_resource(api, monkeypatch, entry):
    import api_server
    client, _, _ = api
    monkeypatch.setenv('AIDUMEI_CALLER_BINDINGS', '{}')
    # Enable global features too: a disabled feature's 404 cannot prove auth.
    for flag in ('AIDUMEM_PERSONA_ENABLED','AIDUMEI_CRYSTALS_ENABLED','AIDUMEI_CODE_GRAPH_ENABLED',
                 'AIDUMEI_EVOLVE_ADMIN_ENABLED','AIDUMEI_SKILL_DRAFTS_ENABLED'):
        monkeypatch.setenv(flag,'true')
    app = api_server.app if entry['app']=='root' else api_server._api_alias
    doc = app.openapi()
    op = doc['paths'][entry['path']][entry['method'].lower()]
    path, params = entry['path'], {}
    for p in op.get('parameters', []):
        if p['in']=='path':
            path=path.replace('{'+p['name']+'}',str(value(p['schema'],doc)))
        elif p.get('required'):
            params[p['name']] = value(p['schema'],doc)
    args = {'params':params}
    if op.get('requestBody'):
        content = op['requestBody']['content']
        mime = next(iter(content))
        args['json' if mime=='application/json' else 'data'] = value(content[mime]['schema'],doc)
    response=client.request(entry['method'], ('/api' if entry['app']=='alias' else '')+path, **args)
    assert response.status_code == 403, (entry,response.status_code,response.text[:500])


def test_unclassified_route_cannot_start():
    from fastapi import FastAPI
    from ducky.scope_auth import ScopeRegistrar
    with pytest.raises(RuntimeError, match='Unclassified'):
        @ScopeRegistrar(FastAPI()).get('/new-memory-helper')
        def new_helper():
            return {'memory':'secret'}
