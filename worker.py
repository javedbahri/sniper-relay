# worker.py — RQ worker with Windows-safe SimpleWorker, startup checks, and heartbeat
from dotenv import load_dotenv; load_dotenv()

import os
import socket
import time
import threading
import redis
from rq import Queue, Worker, SimpleWorker

# ---- env ----
REDIS_URL  = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.getenv("RQ_QUEUE", "sniper")
HEARTBEAT_KEY = os.getenv("WORKER_HEARTBEAT_KEY", "worker:heartbeat")
HEARTBEAT_SEC = int(os.getenv("WORKER_HEARTBEAT_SEC", "15"))

def _heartbeat_loop(r: "redis.Redis", worker_name: str, stop_event: threading.Event):
    """Lightweight liveness signal in Redis."""
    key = f"{HEARTBEAT_KEY}:{worker_name}"
    while not stop_event.is_set():
        try:
            r.set(key, "1", ex=HEARTBEAT_SEC * 3)  # expire if we miss a couple beats
        except Exception:
            pass
        stop_event.wait(HEARTBEAT_SEC)

def main():
    # Connect Redis
    try:
        r = redis.from_url(REDIS_URL)
        if not r.ping():
            raise RuntimeError("Redis ping returned False")
    except Exception as e:
        raise SystemExit(f"[worker] Redis init failed: {e}")

    q = Queue(QUEUE_NAME, connection=r)

    # Choose worker type (Windows has no fork)
    if os.name == "nt":
        w = SimpleWorker([q], connection=r)
        print("[worker] Starting RQ SimpleWorker (Windows, no fork)…")
    else:
        w = Worker([q], connection=r)
        print("[worker] Starting RQ Worker (POSIX fork)…")

    # Start heartbeat thread
    worker_name = f"{socket.gethostname()}:{QUEUE_NAME}"
    stop_event = threading.Event()
    t = threading.Thread(target=_heartbeat_loop, args=(r, worker_name, stop_event), daemon=True)
    t.start()

    try:
        # Run the worker loop
        w.work(logging_level="INFO")
    finally:
        stop_event.set()
        t.join(timeout=2)

if __name__ == "__main__":
    main()
