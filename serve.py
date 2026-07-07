from waitress import serve
from app import app
import os
import logging
from config import Config

try:
    log_path = os.path.abspath(Config.LOG_FILE)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s"
    )
except Exception:
    pass

try:
    log = logging.getLogger("waitress")
    log.info("Starting waitress on port 5000")
except Exception:
    pass

serve(app, host="127.0.0.1", port=5000, threads=8)
