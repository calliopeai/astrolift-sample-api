"""
Managed-service bindings — lazy, absence-tolerant.

This module is the single source of truth for "which managed services
are attached to this pod?". The Astrolift platform injects a stable
connection envelope (a fixed set of env keys) per bound managed service
(see the backend ``astrolift_manifest/env_injection.py`` ``_ENVELOPES``
table). We detect a binding purely by the presence of its anchor env
var, so the base / no-services variant runs with zero managed services
and zero client libraries reaching out to anything.

Binding env names (verified against the backend envelope catalogue):

  postgres      POSTGRES_HOST POSTGRES_PORT POSTGRES_DB POSTGRES_USER
                POSTGRES_PASSWORD POSTGRES_SSL_MODE DATABASE_URL
  redis         REDIS_HOST REDIS_PORT REDIS_USER REDIS_PASSWORD
                REDIS_TLS REDIS_URL
  queue (sqs)   QUEUE_URL QUEUE_ARN_OR_ID QUEUE_NAME QUEUE_REGION
  object_store  BUCKET_NAME BUCKET_REGION BUCKET_ENDPOINT BUCKET_PREFIX
  (s3)

Every client is built on first use and cached; a construction or
connection failure is never raised at import time — it surfaces in
``/selftest`` as ``{ok: false, error: ...}`` so a misconfigured binding
is diagnosable rather than crash-looping the pod.
"""

from __future__ import annotations

import os
import threading
from typing import Any

# Anchor env var whose presence means "this service is bound". The
# platform always sets the whole envelope together, so the anchor is a
# reliable presence signal. Order here drives the order services appear
# in /debug and /selftest.
_ANCHOR: dict[str, str] = {
    "postgres": "POSTGRES_HOST",
    "redis": "REDIS_HOST",
    "queue": "QUEUE_URL",
    "object_store": "BUCKET_NAME",
}

# The full envelope key set per service — surfaced (keys only, never
# values) in /debug so an operator can see exactly which binding keys
# the platform injected without leaking secrets.
ENVELOPE_KEYS: dict[str, tuple[str, ...]] = {
    "postgres": (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_SSL_MODE",
        "DATABASE_URL",
    ),
    "redis": (
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_USER",
        "REDIS_PASSWORD",
        "REDIS_TLS",
        "REDIS_URL",
    ),
    "queue": (
        "QUEUE_URL",
        "QUEUE_ARN_OR_ID",
        "QUEUE_NAME",
        "QUEUE_REGION",
    ),
    "object_store": (
        "BUCKET_NAME",
        "BUCKET_REGION",
        "BUCKET_ENDPOINT",
        "BUCKET_PREFIX",
    ),
}

# Services we expose in /debug bindings + /selftest, in display order.
SERVICES: tuple[str, ...] = ("postgres", "redis", "queue", "object_store")

_lock = threading.Lock()
_clients: dict[str, Any] = {}


def present(service: str) -> bool:
    """True when ``service`` is bound (its anchor env var is set)."""
    anchor = _ANCHOR.get(service)
    return bool(anchor and os.environ.get(anchor))


def bindings_map() -> dict[str, str]:
    """``{service: "present"|"absent"}`` for the /debug block."""
    return {svc: ("present" if present(svc) else "absent") for svc in SERVICES}


# --------------------------------------------------------------------------
# Lazy client builders. Each returns a live client or raises; callers
# decide how to handle failure (the API degrades, /selftest reports it).
# --------------------------------------------------------------------------


def _build_postgres() -> Any:
    import psycopg2  # imported lazily so the base variant needs no live DB

    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        conn = psycopg2.connect(dsn)
    else:
        conn = psycopg2.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            dbname=os.environ.get("POSTGRES_DB", "postgres"),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            sslmode=os.environ.get("POSTGRES_SSL_MODE", "prefer"),
            connect_timeout=5,
        )
    conn.autocommit = True
    _ensure_records_table(conn)
    return conn


def _ensure_records_table(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id          TEXT PRIMARY KEY,
                payload     JSONB NOT NULL,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def _build_redis() -> Any:
    import redis  # lazy

    url = os.environ.get("REDIS_URL")
    if url:
        return redis.Redis.from_url(url, socket_timeout=5, decode_responses=True)
    use_tls = os.environ.get("REDIS_TLS", "").lower() in ("1", "true", "yes")
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ.get("REDIS_PORT", "6379")),
        username=os.environ.get("REDIS_USER") or None,
        password=os.environ.get("REDIS_PASSWORD") or None,
        ssl=use_tls,
        socket_timeout=5,
        decode_responses=True,
    )


def _build_sqs() -> Any:
    import boto3  # lazy

    region = os.environ.get("QUEUE_REGION") or os.environ.get("AWS_REGION")
    # BUCKET_ENDPOINT-style override is object-store only; queue honours
    # the standard AWS_ENDPOINT_URL knob for localstack / minio-of-sqs.
    endpoint = os.environ.get("AWS_ENDPOINT_URL") or None
    return boto3.client("sqs", region_name=region, endpoint_url=endpoint)


def _build_s3() -> Any:
    import boto3  # lazy

    region = os.environ.get("BUCKET_REGION") or os.environ.get("AWS_REGION")
    endpoint = os.environ.get("BUCKET_ENDPOINT") or os.environ.get("AWS_ENDPOINT_URL") or None
    return boto3.client("s3", region_name=region, endpoint_url=endpoint)


_BUILDERS = {
    "postgres": _build_postgres,
    "redis": _build_redis,
    "queue": _build_sqs,
    "object_store": _build_s3,
}


def client(service: str) -> Any:
    """Return a cached live client for ``service``.

    Raises ``RuntimeError`` if the service is not bound, or whatever the
    underlying driver raises on a failed connection. Thread-safe + lazy:
    the client is built once on first use.
    """
    if not present(service):
        raise RuntimeError(f"managed service {service!r} is not bound")
    cached = _clients.get(service)
    if cached is not None:
        return cached
    with _lock:
        cached = _clients.get(service)
        if cached is not None:
            return cached
        cached = _BUILDERS[service]()
        _clients[service] = cached
        return cached


def reset_clients() -> None:
    """Drop cached clients (used by the worker between fatal errors so a
    transient connection blip doesn't wedge a stale handle)."""
    with _lock:
        _clients.clear()


def queue_url() -> str:
    return os.environ["QUEUE_URL"]


def bucket_name() -> str:
    return os.environ["BUCKET_NAME"]


def bucket_prefix() -> str:
    return os.environ.get("BUCKET_PREFIX", "").strip("/")


def bucket_key(name: str) -> str:
    """Compose an object key honouring the platform-injected prefix."""
    prefix = bucket_prefix()
    return f"{prefix}/{name}" if prefix else name
