# Local embedding shim deployment

The native production kernel can use the local OpenAI-compatible embedding shim when the host has the approved `BAAI/bge-small-zh-v1.5` model cached locally.

- Service unit: `deploy/aidumem-embed.service`
- Entry point: `embed_shim.py`
- Bind: `127.0.0.1:8770`
- Dimension: 512
- Model cache: resolved under the service user's `HOME` by fastembed

Before enabling it, verify the model cache hash and run a synthetic embedding smoke probe. Keep the service loopback-only. Roll back by stopping the unit and restoring the previous embedding configuration; do not overwrite the production database during a rollback.
