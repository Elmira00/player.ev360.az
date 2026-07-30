import os
import socket
import subprocess
import threading

from django.conf import settings

# In-memory registry of running per-tour matterport-dl server subprocesses.
# Lives only as long as the Django process does (fine for local dev).
_running_servers: dict[str, dict] = {}
_lock = threading.Lock()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_server_running(matterport_id: str) -> int:
    """Starts matterport-dl.py's own local HTTP server for this tour if it
    isn't already running, and returns the port it's listening on."""
    with _lock:
        entry = _running_servers.get(matterport_id)
        if entry is not None and entry["process"].poll() is None:
            return entry["port"]

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
        _running_servers[matterport_id] = {"process": process, "port": port}
        return port


def stop_server(matterport_id: str) -> None:
    """Stops the running matterport-dl.py server subprocess for this tour,
    if one is running, and removes it from the registry."""
    with _lock:
        entry = _running_servers.pop(matterport_id, None)
        if entry is None:
            print(f"[stop_server] No running server found for {matterport_id}")
            return

        process = entry["process"]
        if process.poll() is None:  # still running
            print(f"[stop_server] Terminating server for {matterport_id} (pid={process.pid})")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(f"[stop_server] Force-killing server for {matterport_id} (pid={process.pid})")
                process.kill()
                process.wait()
        else:
            print(f"[stop_server] Server for {matterport_id} was already dead (exit code {process.poll()})")