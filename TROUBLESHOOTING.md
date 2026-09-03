# aiduMEI Troubleshooting

Every runbook follows the same pattern: symptom → probe → command → repair.

## 1. Service will not start

- Probe: process log and bind address.
- Command: `curl -s http://127.0.0.1:8767/health`
- Repair: check `AIDUMEM_HOST`; a non-loopback bind requires `AIDUMEM_API_TOKEN` or a UI password. Set credentials or bind loopback.

## 2. `/health` is not `ok`

- Probe: `degraded` and `degraded_details`.
- Command: `curl -s http://127.0.0.1:8767/health | jq '{status:.health_status, degraded, details:.degraded_details}'`
- Repair: fix the named component, then rerun health. Do not silence a probe.

## 3. Runtime data path is wrong

- Probe: `probes.runtime_paths`.
- Command: `curl -s http://127.0.0.1:8767/health | jq '.probes.runtime_paths'`
- Repair: set `AIDUMEM_DATA_DIR`, `AIDUMEM_LOG_DIR`, and `AIDUMEM_HOME` consistently, grant write permission, restart.

## 4. Requests return 401

- Probe: API credential configuration.
- Command: `curl -i -H "Authorization: Bearer $AIDUMEM_API_TOKEN" http://127.0.0.1:8767/health`
- Repair: use the same token configured for the service; do not disable authentication for remote access.

## 5. Write accepted but immediate recall misses

- Probe: `action` and job/coalesce state.
- Command: `python scripts/e2e_smoke.py --json`
- Repair: if action is `async_queued` or `coalesce_buffered`, wait for the job or call `/add/coalesce/flush`; then rerun the smoke.

## 6. Recall always empty

- Probe: `_recall_legs`, `_recall_strength`, and `entity_keywords_ok`.
- Command: `curl -s -X POST http://127.0.0.1:8767/search -H 'content-type: application/json' -d '{"query":"...","user_id":"...","bank_id":"default","limit":5}'`
- Repair: configure `AIDUMEM_ENTITY_KEYWORDS` for host-specific names; verify the same `user_id`/`bank_id` was used for write and search.

## 7. Semantic recall degraded

- Probe: `engine_mode`, `vector_leg`, and embedding configuration.
- Command: `curl -s http://127.0.0.1:8767/health | jq '.probes.vector_backend'`
- Repair: in `cloud` mode, restore cloud embedding; in `auto/local`, ensure local dependencies and the staged model exist.

## 8. Local fallback never activates

- Probe: local embed availability and engine mode.
- Command: `python -c "from ducky.local_embed import is_local_embed_available; print(is_local_embed_available())"`
- Repair: install `[local-embed]`, run `fetch_local_embed_model.py`, set `AIDUMEI_ENGINE_MODE=auto` or `local`, and restart.

## 9. Delete reports `partial` or `failed`

- Probe: `failed_layers` and `not_cleared`.
- Command: rerun the delete and inspect the response body.
- Repair: fix the named failed layer, rerun deletion, then run `scripts/e2e_smoke.py`; `not_cleared` contains matrix-exempt system tables, not silent failures.

## 10. Restore finished but behavior is wrong

- Probe: restore count and post-restore smoke.
- Command: `python scripts/e2e_smoke.py --json`
- Repair: restore the verified snapshot again and stop the service before restore if the provider requires exclusive access. Never treat restore as complete without smoke success.

## 11. Requests return 503 `no_credential_public_access_denied` behind a reverse proxy

- Probe: the instance has no credential and the request carries a proxy trace (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`, …). A credential-less instance serves direct loopback only; a proxy hop is refused fail-closed even when the proxy itself runs on the same host.
- Command: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8767/facts?user_id=probe` (direct → 200) vs. the same request through the proxy (→ 503).
- Repair: configure a credential (`AIDUMEM_API_TOKEN`, recommended) so the gate, not the loopback rule, protects the write surface. If you knowingly sit behind a trusted proxy, `AIDUMEI_TRUST_PROXY=1` declares it. Since v20.3.2 the service starts uvicorn with `proxy_headers=False`; if you run uvicorn by hand keep that flag, otherwise the forwarded IP overwrites the peer address and the trust switch cannot work.

## 12. Requests return 421 `host_not_allowed` or 403 `cross_site_write_refused`

- Probe: credential-less instance; the `Host` header is not a loopback name, or a browser sent a write with a foreign `Origin` / `Sec-Fetch-Site: cross-site`. This is DNS-rebinding protection: an attacker page can resolve its own domain to 127.0.0.1 and drive your browser against the local API.
- Command: `curl -s -H 'Host: memory.internal' http://127.0.0.1:8767/health` (→ 421 until the name is trusted).
- Repair: reach the service as `127.0.0.1` / `localhost`, or list your names in `AIDUMEI_TRUSTED_HOSTS=memory.internal,10.0.0.5`. Neither check runs once a credential is configured or `AIDUMEI_TRUST_PROXY=1` is set (the proxy owns virtual-host routing then). CLI/MCP clients never send `Origin`, so the 403 branch cannot hit them.
