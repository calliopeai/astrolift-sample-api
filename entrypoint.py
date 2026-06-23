#!/usr/bin/env python3
"""
Single entrypoint serving both workloads from one image.

Usage (set by the manifest container ``command``):

    python entrypoint.py web      # run the FastAPI service (uvicorn)
    python entrypoint.py worker   # run the queue-consuming worker

``web`` honours ``PORT`` (default 8080). Defaults to ``web`` when no
argument is given so a bare ``docker run`` is useful.
"""

from __future__ import annotations

import os
import sys


def _run_web() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 — container needs to bind all interfaces
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


def _run_worker() -> None:
    from worker.main import run

    run()


def main(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else os.environ.get("WORKLOAD_MODE", "web")).lower()
    if mode == "web":
        _run_web()
    elif mode == "worker":
        _run_worker()
    else:
        sys.stderr.write(f"unknown mode {mode!r}; expected 'web' or 'worker'\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
