from waitress import serve
from app import app, start_background_startup_tasks
import logging

try:
    log = logging.getLogger("waitress")
    log.info("Starting waitress on port 5000")
except Exception:
    pass

# app.py's data load / image scan / recurring stock export are gated behind
# `if __name__ == "__main__":`, which never runs when this module does
# `from app import app` — production (launched via serve.py) was therefore
# never starting the background export timer at all. Start it explicitly.
start_background_startup_tasks()

serve(app, host="127.0.0.1", port=5000, threads=8)
