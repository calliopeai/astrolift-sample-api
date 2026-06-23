"""
FastAPI web service — the public workload of astrolift-sample-api.

Routes:
  POST /records   store a record (+ bound side effects)
  GET  /records   list recent records
  GET  /health    readiness/liveness probe (drives healthcheck.value="/health")
  GET  /debug     the §3.1 diagnostic block
  GET  /selftest  exercise every attached managed service

The service connects to managed services only when their binding env is
present, so the base variant runs with zero services attached.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from app import store
from app.diagnostics import debug_block, startup_banner
from app.selftest import run_selftest

WORKLOAD = "web"
KIND = "deployment"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sample-api.web")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Structured startup banner so logs are immediately useful (§3.1).
    port = int(os.environ.get("PORT", "8080"))
    log.info(startup_banner(WORKLOAD, port))
    yield


app = FastAPI(
    title="astrolift-sample-api",
    version=os.environ.get("GIT_SHA", "dev"),
    lifespan=_lifespan,
)


@app.post("/records")
async def post_record(payload: dict = Body(default_factory=dict)) -> JSONResponse:
    result = store.create_record(payload)
    return JSONResponse(status_code=201, content=result)


@app.get("/records")
async def get_records(limit: int = 50) -> dict:
    return store.list_records(limit=max(1, min(limit, 500)))


@app.get("/health")
async def health() -> dict:
    # Liveness/readiness must not depend on external services — a bound
    # DB being briefly down should not evict the pod. We report readiness
    # of the *process*; binding health lives in /selftest.
    return {"status": "ok", "workload": WORKLOAD}


@app.get("/debug")
async def debug() -> dict:
    return debug_block(KIND, WORKLOAD)


@app.get("/selftest")
async def selftest() -> JSONResponse:
    report = run_selftest()
    # 200 when every attached service passed (or none attached); 503 when
    # an attached binding failed, so a probe / campaign can gate on it.
    status = 200 if report["ok"] else 503
    return JSONResponse(status_code=status, content=report)
