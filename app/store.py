"""
Record store + per-service side effects for POST /records.

Primary store: PostgreSQL when bound, else an in-memory list (so the
base / no-services variant is fully functional). On each write we
additionally — only when the relevant service is bound —

  * cache the record in redis (most-recent list, capped),
  * enqueue a "render" message to the queue (the worker consumes it),
  * archive the record JSON to the object store.

Every side effect is best-effort and isolated: a redis hiccup must not
fail the write, but it is recorded in the response so the caller can see
which fan-outs succeeded. This mirrors how a real service degrades when
one dependency is briefly unavailable.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import uuid
from typing import Any

from app import bindings

_RECENT_KEY = "sample-api:recent"
_RECENT_CAP = 50

# In-memory fallback store (base variant). Guarded by a lock because
# uvicorn may serve concurrent requests across threads.
_mem_lock = threading.Lock()
_mem_records: list[dict] = []


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _new_record(payload: dict) -> dict:
    return {"id": uuid.uuid4().hex, "payload": payload, "created_at": _now_iso()}


# --------------------------------------------------------------------------
# Primary store
# --------------------------------------------------------------------------


def _pg_insert(record: dict) -> None:
    conn = bindings.client("postgres")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO records (id, payload, created_at) VALUES (%s, %s, %s)",
            (record["id"], json.dumps(record["payload"]), record["created_at"]),
        )


def _pg_list(limit: int) -> list[dict]:
    conn = bindings.client("postgres")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, payload, created_at FROM records ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    out: list[dict] = []
    for rid, payload, created in rows:
        # psycopg2 returns JSONB as a dict already; be defensive for str.
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append({"id": rid, "payload": payload, "created_at": created.isoformat()})
    return out


def _mem_insert(record: dict) -> None:
    with _mem_lock:
        _mem_records.append(record)


def _mem_list(limit: int) -> list[dict]:
    with _mem_lock:
        return list(reversed(_mem_records[-limit:]))


# --------------------------------------------------------------------------
# Side effects (best-effort, per bound service)
# --------------------------------------------------------------------------


def _cache_recent(record: dict) -> None:
    r = bindings.client("redis")
    r.lpush(_RECENT_KEY, json.dumps(record))
    r.ltrim(_RECENT_KEY, 0, _RECENT_CAP - 1)


def _enqueue_render(record: dict) -> None:
    sqs = bindings.client("queue")
    sqs.send_message(
        QueueUrl=bindings.queue_url(),
        MessageBody=json.dumps({"action": "render", "record_id": record["id"], "record": record}),
    )


def _archive(record: dict) -> None:
    s3 = bindings.client("object_store")
    key = bindings.bucket_key(f"records/{record['id']}.json")
    s3.put_object(
        Bucket=bindings.bucket_name(),
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json",
    )


_SIDE_EFFECTS = {
    "redis": ("cached", _cache_recent),
    "queue": ("enqueued", _enqueue_render),
    "object_store": ("archived", _archive),
}


def create_record(payload: dict) -> dict:
    """Store a record (postgres or memory) + run bound side effects.

    Returns ``{record, store, side_effects}`` where ``side_effects`` maps
    each attempted fan-out to ``true`` / an error string, so the response
    is self-describing about what actually happened.
    """
    record = _new_record(payload)

    if bindings.present("postgres"):
        _pg_insert(record)
        store = "postgres"
    else:
        _mem_insert(record)
        store = "memory"

    side_effects: dict[str, Any] = {}
    for svc, (label, fn) in _SIDE_EFFECTS.items():
        if not bindings.present(svc):
            continue
        try:
            fn(record)
            side_effects[label] = True
        except Exception as exc:  # noqa: BLE001 — degrade, don't fail the write
            side_effects[label] = f"{type(exc).__name__}: {exc}"

    return {"record": record, "store": store, "side_effects": side_effects}


def list_records(limit: int = 50) -> dict:
    """List recent records from the primary store."""
    if bindings.present("postgres"):
        return {"store": "postgres", "records": _pg_list(limit)}
    return {"store": "memory", "records": _mem_list(limit)}
