# astrolift-sample-api

The managed-service **workhorse** fixture for the Astrolift core-loop
validation campaign (spec 41) — repo **R2** in the spec 42 fixture
catalog. A minimal-but-real **multi-workload** Python (FastAPI) app that
connects to managed services **only when their binding env is present**,
so it runs across the whole `sample-api` variant matrix — from the
zero-service `base` variant up to `full` (all four services).

## Topology

One image, two workloads (both `kind = deployment`):

| Workload | Public | Role |
|---|:--:|---|
| `web` | yes | FastAPI service: stores records, fans out to bound services, exposes the debug + selftest surface. |
| `worker` | no | Consumes the queue (when bound), renders an artifact per message, archives it to the object store (when bound). Idles cleanly when no queue is bound. |

The container `command` selects the workload from the single image:

```
command = ["python", "entrypoint.py", "web"]      # the FastAPI service
command = ["python", "entrypoint.py", "worker"]   # the queue worker
```

### Managed services (attached per variant)

The app opens **live** clients for four services, and only when the
service's binding env is present (lazy + absence-tolerant):

| Service | Anchor env (presence) | Client | What the app does |
|---|---|---|---|
| postgres | `POSTGRES_HOST` | psycopg2 | primary record store (else in-memory) |
| redis | `REDIS_HOST` | redis | caches the most-recent records (capped list) |
| queue (SQS) | `QUEUE_URL` | boto3 | API enqueues "render" jobs; worker consumes |
| object store (S3) | `BUCKET_NAME` | boto3 | API archives records; worker writes artifacts |

When none are present, records are kept in memory and the worker idles —
**the `base` variant runs with zero managed services attached.**

## HTTP surface (the spec 42 §3.1 contract)

| Route | Purpose |
|---|---|
| `POST /records` | Store a record (JSON body) + run bound side effects. Returns `{record, store, side_effects}` — `side_effects` reports per fan-out (`cached`/`enqueued`/`archived`) `true` or an error string. |
| `GET /records?limit=N` | List recent records from the primary store. |
| `GET /health` | `200 {status:"ok"}` when the process is ready. Drives the k8s readiness/liveness probe (`healthcheck.value = "/health"`). Deliberately independent of external services. |
| `GET /debug` | The §3.1 diagnostic JSON (see below). |
| `GET /selftest` | Exercises every **attached** managed service for real; `200` when all attached pass (or none attached), `503` when an attached binding fails. |

`/debug` shape:

```json
{ "app": "sample-api", "version": "<git-sha>", "kind": "deployment",
  "workload": "web", "hostname": "<pod>", "uptime_s": 123, "now": "<iso>",
  "env_seen": ["LOG_LEVEL","PORT","POSTGRES_HOST", ...],
  "bindings": { "postgres": "present", "redis": "absent", "queue": "absent", "object_store": "absent" },
  "build": { "image": "...", "built_at": "...", "commit": "<git-sha>" } }
```

`env_seen` lists non-secret env key **names** plus the **names** of any
present binding env keys — values are never included.

### The `/selftest` contract (how VERIFY-UP proves bindings work)

`/selftest` is how the lifecycle cycle proves a binding *works*, not
merely that the pod started. Per attached service it runs a real
round-trip and reports `{ok, latency_ms, error}`:

| Service | Round-trip |
|---|---|
| postgres | `SELECT 1` |
| redis | `PING` |
| queue (SQS) | send a probe message + receive it back (+ delete) |
| object store (S3) | put + get + delete a probe object |

Absent services report `{ok: null, skipped: true}` — the shape stays
stable across variants without claiming a pass for something unattached.

The worker logs the same `/debug` block to stdout on **every loop**, so
the non-HTTP workload's logs are equally diagnostic.

## Variant → matrix-cell map

`manifests/` holds the spec 42 §5 `sample-api` variants. Every variant is
multi-workload (web + worker). They drive the spec 41 §4.2 AWS managed-
service driver grid.

| Manifest | Services attached | AWS variant(s) | Exercises |
|---|---|---|---|
| `base.toml` | none | — | in-memory store; idle worker; base/no-services cell |
| `pg.toml` | postgres | `rds` | postgres driver + `SELECT 1` |
| `cache.toml` | redis | `elasticache` | redis driver + `PING` |
| `queue.toml` | queue | `sqs` | queue driver + worker consume + send/receive |
| `objstore.toml` | object_store | `s3` | object-store driver + put/get/delete |
| `full.toml` | all four | `rds`,`elasticache`,`sqs`,`s3` | the full fan-out; queued render → S3 artifact |
| `exotic.toml` | mysql, search, vector_index, time_series, email, model_endpoint | `rds_mysql`,`opensearch`,`opensearch_vector`,`timestream`,`ses`,`bedrock` | **binding-presence only** — long-tail kinds parse + inject envelopes; app does not open clients for these |

## Binding env names (per service)

Verified against the backend connection-envelope catalogue
(`astrolift_manifest/env_injection.py` `_ENVELOPES`). The platform injects
the **whole envelope** for a bound service; the app keys off the anchor:

- **postgres** — `POSTGRES_HOST` `POSTGRES_PORT` `POSTGRES_DB`
  `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_SSL_MODE` `DATABASE_URL`
- **redis** — `REDIS_HOST` `REDIS_PORT` `REDIS_USER` `REDIS_PASSWORD`
  `REDIS_TLS` `REDIS_URL`
- **queue** — `QUEUE_URL` `QUEUE_ARN_OR_ID` `QUEUE_NAME` `QUEUE_REGION`
- **object_store** — `BUCKET_NAME` `BUCKET_REGION` `BUCKET_ENDPOINT`
  `BUCKET_PREFIX`

The app prefers the URL forms when present (`DATABASE_URL`, `REDIS_URL`),
falls back to the discrete host/port keys, and composes object keys under
`BUCKET_PREFIX`.

## Schema notes (divergences from spec 42 §4, by code inspection)

Authored against the **real backend parser** (`astrolift_manifest/parser.py`)
— every manifest here parses. Two things the parser does *differently*
from the spec 42 §4 prose, confirmed in code:

1. **Managed-service `kind` is not validated at parse time.** The parser
   accepts any `kind` string; the canonical 16-kind vocabulary lives in
   `astrolift_drivers/service_catalog.py` `KIND_DESCRIPTIONS`. The spec
   §4 list uses a few labels that are **not** the code's names:

   | spec 42 §4 label | code-canonical kind | note |
   |---|---|---|
   | `event_stream` | `mq` | Kafka-protocol broker |
   | `key_value` | `kv_store` | DynamoDB-shape |
   | `vector_db` | `vector_index` | |
   | `filesystem` | `nfs` | |
   | `model_endpoint` | *(none)* | no such kind in `KIND_DESCRIPTIONS`; no envelope |

   This repo uses the **code-canonical** names so the catalog/UI recognise
   the bindings and the env-injection envelopes fire. The one exception is
   `exotic.toml`'s Bedrock entry: there is no `model_endpoint` kind (nor a
   `bedrock` variant) in the current catalog, so it is attached as
   `kind = "model_endpoint"` purely for **binding-presence** coverage — it
   parses and emits no envelope, which is exactly the intent of `exotic`.

2. **The §4 inline skeletons use `;`-separated keys** (e.g.
   `name = "web"; kind = "deployment"`) — that is display shorthand, not
   valid TOML. Real TOML needs one key per line, which is what these
   manifests use.

`object_store` binds **no** `S3_BUCKET`-style key — the envelope is
`BUCKET_*`. The app reads `BUCKET_NAME` / `BUCKET_PREFIX` / `BUCKET_REGION`
/ `BUCKET_ENDPOINT` accordingly.

## Build

`.github/workflows/build.yml` builds the single image and pushes
`docker.io/calliopeai/astrolift-sample-api:main-<sha7>` + `:latest` on
`main`, using the org-level `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`
secrets, with `GIT_SHA` passed as a build-arg (surfaced as `version` in
`/debug`). Mirrors `astrolift-agents/.github/workflows/build-images.yml`.

## Run locally

```bash
pip install -r requirements.txt

# web (base variant — no services)
python entrypoint.py web        # http://127.0.0.1:8080  (PORT overrides)

# worker (idles with no queue bound)
python entrypoint.py worker
```

Attach services by exporting their envelope env (e.g. `POSTGRES_HOST=...`,
`QUEUE_URL=...`, `BUCKET_NAME=...`) before launch — the app detects them
on first use and `/selftest` will exercise them.
