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


def _watchdog(icon: pystray.Icon):
    while icon.visible:
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
    return pystray.Menu(
        pystray.MenuItem("Open in Browser", lambda _: _open_browser()),
        pystray.MenuItem("View Logs", lambda _: os.startfile(LOG_FILE)),
        pystray.MenuItem("Restart Server", lambda _: _restart_server()),
        pystray.MenuItem("Stop & Exit", lambda _: (_stop_server(), icon.stop())),
    )


def run_tray():
    _ensure_running()
    icon = pystray.Icon(
        "Tally Stock Viewer",
        _create_icon_image(),
        "Tally Stock Viewer",
        _create_menu(None),
    )

    def on_icon_setup(icon):
        threading.Thread(target=_watchdog, args=(icon,), daemon=True).start()

    icon.run(on_icon_setup)


if __name__ == "__main__":
    run_tray()
