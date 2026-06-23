"""
Shared diagnostic surface — the spec 42 §3.1 debug block.

Both the web service (``GET /debug``) and the worker (logged each loop)
emit the same structure so the diagnostic contract is identical across
the two workloads of this multi-workload app.
"""

from __future__ import annotations

import datetime as _dt
import os
import socket
import time

from app.bindings import ENVELOPE_KEYS, SERVICES, bindings_map, present

APP_NAME = "sample-api"

# Process start time — uptime is measured from import, which for both
# entrypoints is effectively process boot.
_STARTED_MONO = time.monotonic()
_STARTED_AT = _dt.datetime.now(_dt.timezone.utc)

# Non-secret env keys we always surface by name. Binding anchor/envelope
# keys are added dynamically (keys only, values redacted) so /debug shows
# *which* binding env the platform injected without leaking the values.
_NONSECRET_KEYS: tuple[str, ...] = (
    "LOG_LEVEL",
    "PORT",
    "ASTROLIFT_APP",
    "ASTROLIFT_WORKLOAD",
    "AWS_REGION",
)


def _git_sha() -> str:
    return os.environ.get("GIT_SHA") or os.environ.get("GIT_COMMIT") or "unknown"


def env_seen() -> list[str]:
    """Non-secret env key names + the *names* of any present binding env
    keys (never their values). Sorted for stable output."""
    seen: set[str] = {k for k in _NONSECRET_KEYS if os.environ.get(k) is not None}
    for svc in SERVICES:
        if present(svc):
            for key in ENVELOPE_KEYS[svc]:
                if os.environ.get(key) is not None:
                    seen.add(key)
    return sorted(seen)


def debug_block(kind: str, workload: str) -> dict:
    """The §3.1 diagnostic JSON. ``kind`` is the manifest workload kind
    (``deployment``); ``workload`` is which of the two this process is
    (``web`` / ``worker``)."""
    now = _dt.datetime.now(_dt.timezone.utc)
    sha = _git_sha()
    return {
        "app": APP_NAME,
        "version": sha,
        "kind": kind,
        "workload": workload,
        "hostname": socket.gethostname(),
        "uptime_s": round(time.monotonic() - _STARTED_MONO, 3),
        "now": now.isoformat(),
        "env_seen": env_seen(),
        "bindings": bindings_map(),
        "build": {
            "image": os.environ.get("IMAGE_REF", "docker.io/calliopeai/astrolift-sample-api"),
            "built_at": os.environ.get("BUILT_AT", "unknown"),
            "commit": sha,
        },
    }


def startup_banner(workload: str, port: int | None = None) -> str:
    """One-line structured boot banner so logs are immediately useful."""
    bound = [svc for svc in SERVICES if present(svc)]
    return (
        f"[startup] app={APP_NAME} workload={workload} "
        f"version={_git_sha()} "
        f"bindings={'+'.join(bound) if bound else 'none'} "
        f"port={port if port is not None else '-'} "
        f"started_at={_STARTED_AT.isoformat()}"
    )
