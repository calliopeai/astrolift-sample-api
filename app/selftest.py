"""
``GET /selftest`` — actually exercise each attached managed service.

This is how the lifecycle cycle's VERIFY-UP step proves a binding *works*,
not merely that the pod started. For every bound service we run a real
round-trip and report ``{ok, latency_ms, error}``; absent services are
reported ``{ok: null, skipped: true}`` so the shape is stable across
variants without claiming a pass for something that isn't attached.

  postgres      SELECT 1
  redis         PING
  queue (sqs)   send message + receive (+ delete) it back
  object_store  put + get + delete a probe object
"""

from __future__ import annotations

import time
import uuid

from app import bindings


def _timed(fn) -> dict:
    """Run ``fn``; return the per-service result dict with latency."""
    start = time.perf_counter()
    try:
        detail = fn()
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        result = {"ok": True, "latency_ms": elapsed, "error": None}
        if detail:
            result["detail"] = detail
        return result
    except Exception as exc:  # noqa: BLE001 — surface any driver failure
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        return {"ok": False, "latency_ms": elapsed, "error": f"{type(exc).__name__}: {exc}"}


def _check_postgres() -> dict:
    conn = bindings.client("postgres")
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        row = cur.fetchone()
    return {"select1": row[0] if row else None}


def _check_redis() -> dict:
    r = bindings.client("redis")
    pong = r.ping()
    return {"ping": bool(pong)}


def _check_queue() -> dict:
    sqs = bindings.client("queue")
    url = bindings.queue_url()
    token = uuid.uuid4().hex
    sqs.send_message(QueueUrl=url, MessageBody=f"selftest:{token}")
    # Best-effort receive of our own probe so the round-trip is real.
    resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
    received = False
    for msg in resp.get("Messages", []):
        if msg.get("Body") == f"selftest:{token}":
            received = True
        # Remove probe (and any drained) so we don't leave selftest litter.
        sqs.delete_message(QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])
    return {"sent": True, "received": received}


def _check_object_store() -> dict:
    s3 = bindings.client("object_store")
    bucket = bindings.bucket_name()
    key = bindings.bucket_key(f"selftest/{uuid.uuid4().hex}.txt")
    body = b"astrolift-sample-api selftest"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    got = s3.get_object(Bucket=bucket, Key=key)
    read = got["Body"].read()
    s3.delete_object(Bucket=bucket, Key=key)
    return {"put_get_delete": True, "roundtrip_ok": read == body}


_CHECKS = {
    "postgres": _check_postgres,
    "redis": _check_redis,
    "queue": _check_queue,
    "object_store": _check_object_store,
}


def run_selftest() -> dict:
    """Exercise every bound service; skip absent ones. Returns the full
    per-service report plus a top-level ``ok`` that is True only when no
    *attached* service failed (all-skipped also counts as ok)."""
    services: dict[str, dict] = {}
    overall_ok = True
    for svc in bindings.SERVICES:
        if not bindings.present(svc):
            services[svc] = {"ok": None, "skipped": True, "latency_ms": None, "error": None}
            continue
        result = _timed(_CHECKS[svc])
        services[svc] = result
        if not result["ok"]:
            overall_ok = False
    return {"ok": overall_ok, "services": services}
