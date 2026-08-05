import os
import socket
import subprocess
import threading
import json

import redis
from django.conf import settings

_lock = threading.Lock()

_redis_client = redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)

REGISTRY_KEY_PREFIX = "matterport_server:"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_port_alive(port: int) -> bool:
    """Check if something is actually listening on this port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ensure_server_running(matterport_id: str) -> int:
    """Starts matterport-dl.py's own local HTTP server for this tour if it
    isn't already running, and returns the port it's listening on.

    Uses a Redis-backed registry (shared across all gunicorn workers and
    Celery, unlike an in-process dict) plus a Redis lock, so concurrent
    requests from different workers never spawn duplicate subprocess
    servers for the same tour."""
    registry_key = f"{REGISTRY_KEY_PREFIX}{matterport_id}"
    lock_key = f"{registry_key}:lock"

    # Fast path: already registered and actually alive.
    existing = _redis_client.get(registry_key)
    if existing:
        data = json.loads(existing)
        if _is_port_alive(data["port"]):
            return data["port"]
        else:
            _redis_client.delete(registry_key)

    # Slow path: acquire a short-lived Redis lock so only ONE process
    # spawns the subprocess for this matterport_id, even under concurrency.
    with _lock:
        got_lock = _redis_client.set(lock_key, "1", nx=True, ex=30)
        if not got_lock:
            # Someone else is spawning it right now — wait briefly and
            # check the registry again instead of racing to spawn our own.
            import time
            for _ in range(20):
                time.sleep(0.5)
                existing = _redis_client.get(registry_key)
                if existing:
                    data = json.loads(existing)
                    if _is_port_alive(data["port"]):
                        return data["port"]
            # Fall through and try to spawn ourselves if we timed out waiting.

        try:
            # Double-check after acquiring the lock — another process may
            # have just finished spawning it while we were waiting.
            existing = _redis_client.get(registry_key)
            if existing:
                data = json.loads(existing)
                if _is_port_alive(data["port"]):
                    return data["port"]

            port = _find_free_port()
            log_dir = os.path.join(settings.BASE_DIR, "tour_server_logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, f"{matterport_id}.log")
            log_file = open(log_path, "w", encoding="utf-8")

            process = subprocess.Popen(
                [
                    settings.MATTERPORT_DL_PYTHON,
                    "matterport-dl.py",
                    matterport_id,
                    "127.0.0.1",
                    str(port),
                ],
                cwd=settings.MATTERPORT_DL_DIR,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )

            _redis_client.set(
                registry_key,
                json.dumps({"port": port, "pid": process.pid}),
                ex=3600,  # expire after 1hr of inactivity as a safety net
            )
            return port
        finally:
            _redis_client.delete(lock_key)


def stop_server(matterport_id: str) -> None:
    """Stops the running matterport-dl.py server subprocess for this tour,
    if one is running, and removes it from the shared registry."""
    registry_key = f"{REGISTRY_KEY_PREFIX}{matterport_id}"
    existing = _redis_client.get(registry_key)

    if existing is None:
        print(f"[stop_server] No running server found for {matterport_id}")
        return

    data = json.loads(existing)
    pid = data["pid"]

    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"[stop_server] Terminated server for {matterport_id} (pid={pid})")
    except ProcessLookupError:
        print(f"[stop_server] Server for {matterport_id} (pid={pid}) was already dead")
    except Exception as e:
        print(f"[stop_server] Error stopping {matterport_id} (pid={pid}): {e}")

    _redis_client.delete(registry_key)
