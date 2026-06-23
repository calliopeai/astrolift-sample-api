"""
Worker workload — consumes the queue, writes artifacts to the object store.

This is the second workload of the multi-workload app. Behaviour by
binding:

  * queue bound        long-poll the queue; for each "render" message,
                       write a rendered artifact to the object store
                       (when bound) and delete the message.
  * queue absent       idle loop (the base / no-queue variants still run
                       cleanly as a deployment) — it simply logs the
                       §3.1 diagnostic block each loop and sleeps.

The §3.1 debug block is logged on every loop iteration so the worker's
logs are immediately useful (spec 42 §3.1 — non-HTTP fixtures log the
same diagnostic block to stdout on each run).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import signal
import time

from app import bindings
from app.diagnostics import debug_block, startup_banner

WORKLOAD = "worker"
KIND = "deployment"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("sample-api.worker")

# Idle sleep when there's no queue bound, and the long-poll wait when
# there is. Kept short so the worker is responsive to SIGTERM.
_IDLE_SLEEP_S = 15
_POLL_WAIT_S = 10

_running = True


def _stop(signum, _frame) -> None:  # noqa: ANN001
    global _running
    log.info("received signal %s; draining and exiting", signum)
    _running = False


def _render_artifact(record: dict) -> bytes:
    """Produce the 'rendered' artifact bytes for a record. Minimal but
    real: a small JSON summary stamped with render time."""
    return json.dumps(
        {
            "record_id": record.get("id"),
            "rendered_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "payload": record.get("payload"),
            "renderer": "sample-api.worker",
        },
        indent=2,
    ).encode("utf-8")


def _handle_message(body: str) -> None:
    """Process one queue message: render + archive the artifact."""
    try:
        msg = json.loads(body)
    except json.JSONDecodeError:
        log.warning("dropping non-JSON message: %r", body[:200])
        return

    record = msg.get("record") or {"id": msg.get("record_id")}
    artifact = _render_artifact(record)

    if bindings.present("object_store"):
        s3 = bindings.client("object_store")
        key = bindings.bucket_key(f"artifacts/{record.get('id', 'unknown')}.json")
        s3.put_object(
            Bucket=bindings.bucket_name(),
            Key=key,
            Body=artifact,
            ContentType="application/json",
        )
        log.info("rendered artifact for record %s -> s3://%s/%s", record.get("id"), bindings.bucket_name(), key)
    else:
        # No object store bound — the render still "happens", we just log
        # it rather than persist. Keeps the queue-only variant meaningful.
        log.info("rendered artifact for record %s (%d bytes, no object store bound)", record.get("id"), len(artifact))


def _drain_queue_once() -> int:
    """Receive a batch and process it. Returns the number handled."""
    sqs = bindings.client("queue")
    url = bindings.queue_url()
    resp = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=_POLL_WAIT_S)
    messages = resp.get("Messages", [])
    for msg in messages:
        try:
            _handle_message(msg.get("Body", ""))
        finally:
            # Delete even on handler error: a poison message must not wedge
            # the worker forever. A real system would dead-letter; the
            # fixture keeps it simple and observable via logs.
            sqs.delete_message(QueueUrl=url, ReceiptHandle=msg["ReceiptHandle"])
    return len(messages)


def run() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    has_queue = bindings.present("queue")
    log.info(startup_banner(WORKLOAD))
    log.info("worker mode: %s", "consume-queue" if has_queue else "idle (no queue bound)")

    while _running:
        # Diagnostic block every loop — the worker's equivalent of /debug.
        log.info("debug %s", json.dumps(debug_block(KIND, WORKLOAD)))

        if not has_queue:
            _sleep_interruptible(_IDLE_SLEEP_S)
            continue

        try:
            handled = _drain_queue_once()
            if handled == 0:
                # Long-poll already waited; loop straight back to re-poll.
                continue
        except Exception as exc:  # noqa: BLE001
            log.error("queue drain failed: %s: %s", type(exc).__name__, exc)
            bindings.reset_clients()  # drop a possibly-stale handle
            _sleep_interruptible(_IDLE_SLEEP_S)


def _sleep_interruptible(seconds: int) -> None:
    """Sleep in 1s slices so SIGTERM is honoured promptly."""
    for _ in range(seconds):
        if not _running:
            return
        time.sleep(1)


if __name__ == "__main__":
    run()
