import os

from waitress import serve
from app import app, start_background_startup_tasks
import logging

# Render (and other PaaS hosts) assign the port via $PORT and require
# binding to it; the office PC never sets this, so it keeps using 5000.
port = int(os.environ.get("PORT", "5000"))

try:
    log = logging.getLogger("waitress")
    log.info(f"Starting waitress on port {port}")
except Exception:
    pass

# app.py's data load / image scan / recurring stock export are gated behind
# `if __name__ == "__main__":`, which never runs when this module does
# `from app import app` — production (launched via serve.py) was therefore
# never starting the background export timer at all. Start it explicitly.
start_background_startup_tasks()

serve(app, host="0.0.0.0", port=port, threads=8)
