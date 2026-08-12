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
    registry_key = f"{REGISTRY_KEY_PREFIX}{matterport_id}"
    lock_key = f"{registry_key}:lock"

    existing = _redis_client.get(registry_key)
    if existing:
        data = json.loads(existing)
        if _is_port_alive(data["port"]):
            return data["port"]
        else:
            _redis_client.delete(registry_key)

    with _lock:
        # Block until we get the lock — do NOT fall through and spawn a
        # duplicate if someone else is already spawning. A previous
        # version fell through after a timeout, which could create an
        # orphaned second subprocess whose PID then vanishes from Redis
        # once the first spawn overwrites the registry key, leaving it
        # unkillable by stop_server.
        got_lock = False
        for _ in range(40):  # up to ~20s total, matching old wait budget
            got_lock = _redis_client.set(lock_key, "1", nx=True, ex=30)
            if got_lock:
                break
            existing = _redis_client.get(registry_key)
            if existing:
                data = json.loads(existing)
                if _is_port_alive(data["port"]):
                    return data["port"]
            import time
            time.sleep(0.5)

        if not got_lock:
            # Still couldn't get the lock after waiting — something is
            # stuck (e.g. a crashed holder left a lock with no server).
            # Safer to raise than silently spawn a duplicate.
            raise RuntimeError(
                f"Timed out waiting for server lock for {matterport_id}; "
                f"a stuck lock or slow spawn may be blocking it."
            )

        try:
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
                ex=3600,
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
