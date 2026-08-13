"""Dump broker queue state and Celery consumer state from inside a worker pod.

Run when the helm e2e fails:
the queue lengths tell whether a task is still sitting in the broker,
and the inspect calls tell whether each worker's consumer is alive and what it holds.

Usage: kubectl exec -i <deployment> -- uv run python - < scripts/lib/broker_state.py
"""

import os
import traceback


def broker_queues() -> None:
    import redis

    r = redis.Redis.from_url(os.environ["CELERY_BROKER_URL"])
    # Celery queues are plain lists, so every list key is a queue.
    # Discovering them covers any size queues the routing table produces.
    for key in sorted(r.scan_iter()):
        if r.type(key) == b"list":
            print(key.decode(), r.llen(key))
    print("unacked", r.hlen("unacked"))
    print("unacked_index", r.zcard("unacked_index"))


def celery_inspect() -> None:
    from climate_ref_celery.app import app

    insp = app.control.inspect(timeout=10)
    print("ping", insp.ping())
    print("active", insp.active())
    print("reserved", insp.reserved())
    print("active_queues", insp.active_queues())


# Each section tolerates the other failing, so a dead broker still leaves the inspect output.
for title, section in (("Broker queue state", broker_queues), ("Celery inspect", celery_inspect)):
    print(f"=== {title} ===")
    try:
        section()
    except Exception:
        traceback.print_exc()
    print()
