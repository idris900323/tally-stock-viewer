from waitress import serve
from app import app
import logging

try:
    log = logging.getLogger("waitress")
    log.info("Starting waitress on port 5000")
except Exception:
    pass

serve(app, host="127.0.0.1", port=5000, threads=8)
