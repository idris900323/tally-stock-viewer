import os
import sys
import time
import socket
import threading
import subprocess
import webbrowser
from pathlib import Path

import psutil
import pystray
from PIL import Image, ImageDraw, ImageFont

APP_DIR = str(Path(__file__).resolve().parent)
APP_ENTRY = str(Path(APP_DIR) / "app.py")
PYTHON_EXE = str(Path(APP_DIR) / ".venv" / "Scripts" / "python.exe")
PYTHONW_EXE = str(Path(APP_DIR) / ".venv" / "Scripts" / "pythonw.exe")
PID_FILE = str(Path(APP_DIR) / "app.pid")
LAUNCHER_PID_FILE = str(Path(APP_DIR) / "launcher.pid")
LOG_FILE = str(Path(APP_DIR) / "logs" / "app.log")
ICON_FILE = str(Path(APP_DIR) / "icon.png")
APP_URL = "http://localhost:5000"
APP_PORT = 5000
SERVE_SCRIPT = str(Path(APP_DIR) / "serve.py")

# cloudflared tunnel constants
CLOUDFLARED_EXE = str(Path(APP_DIR) / "cloudflared.exe")
TUNNEL_NAME = "tally-stock"
TUNNEL_PID_FILE = str(Path(APP_DIR) / "tunnel.pid")


def _write_log(message: str):
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def _is_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", APP_PORT)) == 0


def _read_pid() -> int | None:
    try:
        return int(Path(PID_FILE).read_text())
    except Exception:
        return None


def _write_pid(pid: int):
    try:
        Path(PID_FILE).write_text(str(pid))
    except Exception as exc:
        _write_log(f"Failed to write PID file: {exc}")


def _remove_pid_file():
    try:
        pid_path = Path(PID_FILE)
        if pid_path.exists():
            pid_path.unlink()
    except Exception:
        pass


def _is_process_alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        return False


def _is_live_launcher_process(pid: int) -> bool:
    """True only if pid is a running python/pythonw process whose command
    line references launcher.pyw — a plain aliveness check is not enough,
    because after a crash/reboot the OS can hand the same PID number to an
    unrelated process and a stale launcher.pid would then block startup
    forever."""
    if pid == os.getpid():
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        name = (proc.name() or "").lower()
        if not name.startswith("python"):
            return False
        cmdline = " ".join(proc.cmdline()).lower()
        return "launcher.pyw" in cmdline
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except Exception:
        return False


def _acquire_launcher_lock() -> bool:
    """Single-instance guard. Returns False (caller must exit, touching
    nothing) if another live launcher owns the lock; otherwise records this
    process's PID in LAUNCHER_PID_FILE and returns True."""
    lock_path = Path(LAUNCHER_PID_FILE)
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
        except Exception:
            existing_pid = None
        if existing_pid is not None and _is_live_launcher_process(existing_pid):
            _write_log(f"Another launcher instance is already running (pid={existing_pid}) — exiting.")
            return False
        _write_log(f"Stale launcher.pid found (pid={existing_pid}, no live launcher) — recovering and starting normally.")
    else:
        _write_log("No launcher.pid present — fresh launcher start.")
    try:
        lock_path.write_text(str(os.getpid()))
        _write_log(f"Acquired launcher lock pid={os.getpid()}")
    except Exception as exc:
        _write_log(f"Failed to write launcher PID file: {exc}")
    return True


def _remove_launcher_pid_file():
    # Only remove a lock this process owns, so an unlikely startup race can
    # never delete the surviving instance's lock on the way out.
    try:
        lock_path = Path(LAUNCHER_PID_FILE)
        if lock_path.exists() and lock_path.read_text().strip() == str(os.getpid()):
            lock_path.unlink()
            _write_log("Removed launcher.pid on clean exit.")
    except Exception:
        pass


def _read_tunnel_pid() -> int | None:
    try:
        return int(Path(TUNNEL_PID_FILE).read_text())
    except Exception:
        return None


def _write_tunnel_pid(pid: int):
    try:
        Path(TUNNEL_PID_FILE).write_text(str(pid))
    except Exception as exc:
        _write_log(f"Failed to write tunnel PID file: {exc}")


def _remove_tunnel_pid_file():
    try:
        p = Path(TUNNEL_PID_FILE)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _start_tunnel():
    try:
        if not Path(CLOUDFLARED_EXE).exists():
            _write_log("cloudflared not present; skipping tunnel start")
            return None

        existing = _read_tunnel_pid()
        if existing is not None and _is_process_alive(existing):
            _write_log(f"Tunnel already running pid={existing}")
            return existing

        proc = subprocess.Popen(
            [CLOUDFLARED_EXE, "tunnel", "--url", f"http://localhost:{APP_PORT}", "--name", TUNNEL_NAME],
            cwd=APP_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _write_tunnel_pid(proc.pid)
        _write_log(f"Started cloudflared tunnel pid={proc.pid}")
        return proc.pid
    except Exception as exc:
        _write_log(f"Failed to start cloudflared tunnel: {exc}")
        return None


def _stop_tunnel():
    pid = _read_tunnel_pid()
    if pid is None:
        return

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=10)
        _write_log(f"Stopped cloudflared tunnel pid={pid}")
    except Exception as exc:
        _write_log(f"Failed to stop cloudflared tunnel pid={pid}: {exc}")
    finally:
        _remove_tunnel_pid_file()


def _launch_server():
    if _is_port_open():
        _write_log("Port already open, skipping launch.")
        return None

    try:
        proc = subprocess.Popen(
            [PYTHONW_EXE, SERVE_SCRIPT],
            cwd=APP_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        _write_pid(proc.pid)
        _write_log(f"Launched server process pid={proc.pid}")
        # start cloudflared tunnel (best-effort)
        try:
            _start_tunnel()
        except Exception:
            pass
        return proc.pid
    except Exception as exc:
        _write_log(f"Failed to launch server: {exc}")
        return None


def _wait_for_port(timeout: float = 15.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if _is_port_open():
            return True
        time.sleep(0.5)
    return False


def _open_browser():
    try:
        webbrowser.open(APP_URL)
        _write_log("Opened browser to app URL.")
    except Exception as exc:
        _write_log(f"Failed to open browser: {exc}")


def _stop_server():
    pid = _read_pid()
    if pid is None:
        return

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=10)
        _write_log(f"Stopped server pid={pid}")
    except Exception as exc:
        _write_log(f"Failed to stop server pid={pid}: {exc}")
    finally:
        _remove_pid_file()
        try:
            _stop_tunnel()
        except Exception:
            pass


def _restart_server():
    _write_log("Restart requested.")
    _stop_server()
    time.sleep(2)
    pid = _launch_server()
    if pid is not None and _wait_for_port():
        _open_browser()


def _ensure_running():
    pid = _read_pid()
    if pid is not None and _is_process_alive(pid) and _is_port_open():
        return pid

    _remove_pid_file()
    pid = _launch_server()
    if pid is not None and _wait_for_port():
        _open_browser()
    return pid


def _create_icon_image() -> Image.Image:
    try:
        if Path(ICON_FILE).exists():
            return Image.open(ICON_FILE)
    except Exception:
        pass

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse([4, 4, 60, 60], fill=(34, 139, 34, 255))
    try:
        font = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        font = ImageFont.load_default()
    text = "S"
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(((64 - w) / 2, (64 - h) / 2), text, fill="white", font=font)
    return image


def _watchdog():
    _write_log("Watchdog thread started")
    while True:
        _write_log("Watchdog poll cycle")
        pid = _read_pid()
        if pid is not None:
            if not _is_process_alive(pid) or not _is_port_open():
                _write_log("Server crashed; auto-restarting")
                _restart_server()
        else:
            _write_log("No pid file found; launching server")
            _ensure_running()
        time.sleep(30)


def _create_menu(icon: pystray.Icon):
    # pystray passes the live icon as the callback's first argument; Stop &
    # Exit must use that, not the _create_menu parameter — run_tray() builds
    # the menu with _create_menu(None) before the icon exists, so the closure
    # variable is always None and closure_icon.stop() raised AttributeError
    # (Stop & Exit stopped the server but left the launcher + watchdog alive,
    # which then auto-restarted the server ~30s later).
    return pystray.Menu(
        pystray.MenuItem("Open in Browser", lambda _: _open_browser()),
        pystray.MenuItem("View Logs", lambda _: os.startfile(LOG_FILE)),
        pystray.MenuItem("Restart Server", lambda _: _restart_server()),
        pystray.MenuItem("Stop & Exit", lambda live_icon: (_stop_server(), live_icon.stop())),
    )


def run_tray():
    _ensure_running()
    icon = pystray.Icon(
        "Tally Stock Viewer",
        _create_icon_image(),
        "Tally Stock Viewer",
        _create_menu(None),
    )

    # Started unconditionally, before icon.run(), so the safety-net watchdog
    # can never be blocked by tray icon initialization failing or being
    # slow (see on_icon_setup below, which pystray requires to explicitly
    # set icon.visible = True since it replaces the default setup callback).
    threading.Thread(target=_watchdog, daemon=True).start()

    def on_icon_setup(icon):
        _write_log("Tray icon setup callback started")
        icon.visible = True

    icon.run(on_icon_setup)


if __name__ == "__main__":
    # Single-instance guard must be the very first thing that runs: a blocked
    # duplicate must exit before the server launch, tray icon, or watchdog
    # thread ever start.
    if not _acquire_launcher_lock():
        sys.exit(0)
    try:
        run_tray()
    finally:
        # icon.run() returns when Stop & Exit calls icon.stop(); removing the
        # lock here keeps the next start on the clean fresh-start path. A
        # forced kill skips this, which is what the stale-lock recovery in
        # _acquire_launcher_lock() is for.
        _remove_launcher_pid_file()
