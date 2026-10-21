"""Render entry point:  uvicorn main:app --host 0.0.0.0 --port $PORT  (run from cloud_app/)."""
import logging

from phonerec.api import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
