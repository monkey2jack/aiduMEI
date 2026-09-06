# Deploy aiduMEI on Dockhold

Dockhold builds the `Dockerfile` in this repository and serves it at an HTTPS
URL that stays on. Nothing has to be added to the repository.

https://app.dockhold.eu/new?repo=https://github.com/monkey2jack/aiduMEI

The port needs no configuration. Dockhold injects `PORT` and aiduMEI reads it.

## Set before the first request

| Variable | Value | Why |
|---|---|---|
| `AIDUMEM_HOST` | `0.0.0.0` | The image binds loopback. Without this the platform cannot reach the service. |
| `AIDUMEM_API_TOKEN` | a token, in the Vault | aiduMEI refuses to start on a non-loopback bind with no credential. It is a secret, so it belongs in the Vault rather than a plain variable. |

## Turn on storage, or it forgets

aiduMEI keeps facts, observations and the FTS index in SQLite files. A Dockhold
app has no persistent disk until storage is turned on, so without it every
deploy starts from an empty memory. This was measured both ways: a record
written through `POST /add/raw` was gone after a restart without storage, and
survived a restart with it.

Turn on app storage, then set:

| Variable | Value |
|---|---|
| `AIDUMEM_DATA_DIR` | the path Dockhold reports in `DATA_DIR` |
| `AIDUMEM_LOG_DIR` | a folder under that path |

Two things follow from having a writable disk. `AIDUMEM_UI_PASSWORD` has no
default, so without storage aiduMEI generates a console password on every boot
and writes the plaintext to a file that is gone by the next deploy; set it in
the Vault. And set the LLM and embedding credentials as for any other
deployment, or the vector backend stays unconfigured: `POST /add/raw` works and
recall is degraded, exactly as described in `docs/HEALTH.md`.

## Check it worked

```bash
curl -s -H "Authorization: Bearer $AIDUMEM_API_TOKEN" \
  https://<your-app>.dockhold.app/health \
  | jq '.status, .probes.runtime_paths.data_dir, .probes.runtime_paths.data_dir_writable'
```

`data_dir` must be the path that was set, and `data_dir_writable` must be true.

A 200 does not prove the token is load-bearing, so check the request that has
to fail:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -d '{"content":"probe"}' \
  https://<your-app>.dockhold.app/add/raw
```

That must be `401`. `scripts/e2e_smoke.py --json` remains the full acceptance
check once LLM and embedding credentials are in place.

## Notes

- Name the app something other than `aidumei`. Dockhold injects service
  variables named after the app, and an app called `aidumei` produces
  `AIDUMEI_<id>_PORT_*`, which collide with this project's own `AIDUME[IM]_*`
  name registry and fill the startup log with warnings about unknown variables.
- The filesystem outside the storage path is scratch, so caches start cold
  after each deploy and the first requests pay for it.
- Sizing and prices: https://dockhold.eu/pricing
