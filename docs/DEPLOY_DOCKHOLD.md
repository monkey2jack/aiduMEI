# Deploy aiduMEI on Dockhold

Dockhold builds the `Dockerfile` in this repository and serves it at an HTTPS
URL that stays on. Nothing has to be added to the repository.

https://app.dockhold.eu/new?repo=https://github.com/monkey2jack/aiduMEI

Dockhold injects `PORT` and aiduMEI reads it. Leave `AIDUMEM_API_PORT` and
`MEM0_API_PORT` unset to use that value; explicit values take precedence.

## Set before the first request

| Variable | Value | Why |
|---|---|---|
| `AIDUMEM_HOST` | `0.0.0.0` | The image binds loopback. Without this the platform cannot reach the service. |
| `AIDUMEM_API_TOKEN` | a token, in the Vault | aiduMEI refuses to start on a non-loopback bind with no credential. It is a secret, so it belongs in the Vault rather than a plain variable. |
| `AIDUMEM_UI_PASSWORD` | a different password, in the Vault | Gives the console a stable login across container replacements. |
| `AIDUMEI_ENGINE_MODE` | `cloud` | The standard image includes cloud dependencies. `auto` and `local` also require the local embedding extra and a downloaded model. |

## Turn on storage, or it forgets

aiduMEI keeps facts, observations and the FTS index in SQLite files. A Dockhold
app has no persistent disk until storage is turned on, so without it every
deploy starts from an empty memory. This was measured both ways: a record
written through `POST /add/raw` was gone after a restart without storage, and
survived a restart with it.

Turn on app storage, then set the following variables to actual absolute
paths. Use the path Dockhold reports in `DATA_DIR`; the strings below are
placeholders, not environment-variable expressions for the settings form.

| Variable | Value |
|---|---|
| `AIDUMEM_DATA_DIR` | the path Dockhold reports in `DATA_DIR` |
| `AIDUMEM_LOG_DIR` | a folder under that path |
| `AIDUMEM_CONFIG_FILE` | `mem0_config_local.json` under that path |

## Put the model configuration on the same disk

Provision a private configuration file at `AIDUMEM_CONFIG_FILE` using a
runtime file mount, file upload, or shell access supported by your deployment.
For a fresh installation, start from `mem0_config_local.json.example` and fill
in `llm.config` and `embedder.config` with your model names, API endpoints and
credentials. The example is a template; it is not loaded automatically.

The image excludes real configuration and credentials from the build context.
Its default configuration location, `/app/mem0_config_local.json`, is under
the root-owned code directory, so the service cannot create or update it
there. For console editing, both the configuration file and its parent
directory must be writable by the runtime user. For a read-only secret file
mount, set `AIDUMEM_CONFIG_READONLY=1` and update it through the mount instead.
Setting model credentials as unrelated environment variables does not create
the required configuration file.

Set **both** storage paths inside that JSON to the persistent disk as well.
For example, if the actual `DATA_DIR` is `/persistent/aidumei`, these fields
should be:

```json
{
  "vector_store": {
    "provider": "qdrant",
    "config": {
      "collection_name": "aidu_mem",
      "path": "/persistent/aidumei/qdrant",
      "embedding_model_dims": 1024,
      "on_disk": true
    }
  },
  "history_db_path": "/persistent/aidumei/history.db"
}
```

These are fields to update in the full configuration, alongside the LLM and
embedder sections. Match `embedding_model_dims` and the embedder's
`embedding_dims` to your embedding model. Changing `AIDUMEM_DATA_DIR` relocates
aiduMEI's SQLite files; it does **not** rewrite mem0's Qdrant or history paths.
Keeping the template's relative `./data/...` values would write those stores
to `/app/data` even when Dockhold persists a different directory.

Restart the service after provisioning the configuration. Without valid model
configuration, `POST /add/raw` still works while recall is degraded, as
described in [HEALTH.md](HEALTH.md). A raw-record persistence check alone does
not validate the vector store.

## Check it worked

```bash
curl -s -H "Authorization: Bearer $AIDUMEM_API_TOKEN" \
  https://<your-app>.dockhold.app/health \
  | jq '{health_status, degraded, degraded_details, runtime_paths: .probes.runtime_paths}'
```

`health_status` must be `ok`, `data_dir` must be the persistent path that was
set, and `data_dir_writable` must be true. Inspect every `degraded` entry and
its explanation; a reachable HTTP endpoint alone does not establish working
memory.

A 200 does not prove the token is load-bearing, so check the request that has
to fail:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -d '{"content":"probe"}' \
  https://<your-app>.dockhold.app/add/raw
```

That must be `401`. Once the model credentials are in place, run the full
acceptance check from a shell in the running container:

```bash
python scripts/e2e_smoke.py --json
```

The script uses the same port priority as the service, including `PORT`.
Require exit code 0, `status: PASS`, zero failures and zero warnings. When
running from another machine, pass `--api https://<your-app>.dockhold.app`
and supply the API token and a matching local configuration file through
`AIDUMEM_CONFIG_FILE`; the script checks that file locally.

Finally, test persistence across a **container replacement or redeploy**;
restarting the Python process alone may leave the same temporary filesystem
in place:

1. Use a dedicated temporary tenant and `POST /add` to store a unique test
   fact with model extraction enabled. Confirm it is recalled by `POST /search`
   before the replacement and inspect `POST /search_trace` for a vector result.
2. Replace or redeploy the container with the same persistent storage attached.
3. Confirm the configuration is still present, check authenticated `/health`,
   and repeat the same tenant's search. Inspect the trace to confirm the vector
   result survived; a raw-text fallback alone does not prove this.
4. Verify the configured Qdrant directory and history database are on the
   persistent path, then remove the temporary tenant with
   `POST /delete_all`, using the JSON body below and the same tenant/bank as
   the test write. Require HTTP 200 and `status: committed`.

```json
{"user_id":"<temporary-tenant>","bank_id":"default","confirm":true}
```

The smoke script cleans its tenant before exiting, so use a separate tenant
for this cross-redeploy check. Keep the test fact non-sensitive and record
the before/after results as deployment evidence.

## Notes

- Name the app something other than `aidumei`. Dockhold injects service
  variables named after the app, and an app called `aidumei` produces
  `AIDUMEI_<id>_PORT_*`, which collide with this project's own `AIDUME[IM]_*`
  name registry and fill the startup log with warnings about unknown variables.
- The filesystem outside the storage path is scratch, so caches start cold
  after each deploy and the first requests pay for it.
- Sizing and prices: https://dockhold.eu/pricing
