from waitress import serve
from app import app
import os, logging

logging.basicConfig(
    filename=r"C:\tally_stock\logs\app.log",
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)
log = logging.getLogger("waitress")
log.info("Starting waitress on port 5000")
serve(app, host="127.0.0.1", port=5000, threads=8)
