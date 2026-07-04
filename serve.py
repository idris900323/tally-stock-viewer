import os
from pathlib import Path
import logging

from waitress import serve

from config import Config
from app import app


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

os.makedirs(os.path.dirname(os.path.abspath(Config.LOG_FILE)), exist_ok=True)

logging.basicConfig(
    filename=Config.LOG_FILE,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)
log = logging.getLogger("waitress")
log.info("Starting waitress on port 5000")
serve(app, host="127.0.0.1", port=5000, threads=8)
