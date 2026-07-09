"""Detached helper spawned by the System Panel's restart routes
(/admin/system/pull_and_restart, /admin/system/restart_app_only).

Why this exists: those routes used to just call os._exit(0) and trust
that launcher.pyw's watchdog thread would notice the dead process and
relaunch it. On 2026-07-09 a real pull_and_restart left the site down
indefinitely — launcher.pyw had a pre-existing bug (Path used before it
was imported) that made it crash instantly on every launch, so its
watchdog never actually ran. Relying solely on an external supervisor
being alive and correctly configured is exactly the assumption that
failed, so this helper makes the restart self-sufficient: it is spawned
directly by app.py right before it exits, independent of whether
launcher.pyw's watchdog is present, running, or fixed.

It waits for this machine's port 5000 to be released by the exiting
app.py/serve.py process, then launches a fresh serve.py and records its
pid to app.pid (kept in sync with launcher.pyw's own bookkeeping, so if
launcher.pyw's watchdog is ALSO running it just sees the port already
open and skips launching a second instance).
"""
import os
import socket
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHONW_EXE = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")
SERVE_SCRIPT = os.path.join(BASE_DIR, "serve.py")
PID_FILE = os.path.join(BASE_DIR, "app.pid")
LOG_FILE = os.path.join(BASE_DIR, "logs", "app.log")
PORT = 5000
PORT_RELEASE_TIMEOUT = 20
PORT_REOPEN_TIMEOUT = 15


def _log(message):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] [relaunch_helper] {message}\n")
    except Exception:
        pass


def _port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    deadline = time.time() + PORT_RELEASE_TIMEOUT
    while _port_open() and time.time() < deadline:
        time.sleep(0.5)

    if _port_open():
        _log(f"Port {PORT} still open after {PORT_RELEASE_TIMEOUT}s wait; "
             "aborting relaunch to avoid running a second instance.")
        return

    python_w = PYTHONW_EXE if os.path.exists(PYTHONW_EXE) else sys.executable
    try:
        proc = subprocess.Popen(
            [python_w, SERVE_SCRIPT],
            cwd=BASE_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        _log(f"Failed to relaunch server: {exc}")
        return

    try:
        with open(PID_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(proc.pid))
    except Exception:
        pass

    deadline = time.time() + PORT_REOPEN_TIMEOUT
    while not _port_open() and time.time() < deadline:
        time.sleep(0.5)

    if _port_open():
        _log(f"Relaunched server pid={proc.pid}; port {PORT} is back up.")
    else:
        _log(f"Relaunched server pid={proc.pid}; port {PORT} did not come up "
             f"within {PORT_REOPEN_TIMEOUT}s — check the log above this line.")


if __name__ == "__main__":
    main()
