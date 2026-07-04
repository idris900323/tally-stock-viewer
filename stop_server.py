import pathlib
import sys

try:
    import psutil
except ImportError:
    print("psutil not installed")
    sys.exit(1)

PID_FILE = pathlib.Path(r"C:\tally_stock\app.pid")
TUNNEL_PID_FILE = pathlib.Path(r"C:\tally_stock\tunnel.pid")


def _stop(pid_file, label):
    if not pid_file.exists():
        print(f"No {label} pid file found.")
        return
    try:
        pid = int(pid_file.read_text().strip())
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=10)
        pid_file.unlink(missing_ok=True)
        print(f"{label} stopped (pid {pid}).")
    except psutil.NoSuchProcess:
        pid_file.unlink(missing_ok=True)
        print(f"{label} process not found; cleaned up pid file.")
    except Exception as exc:
        print(f"Failed to stop {label}: {exc}")


_stop(PID_FILE, "Server")
_stop(TUNNEL_PID_FILE, "Tunnel")