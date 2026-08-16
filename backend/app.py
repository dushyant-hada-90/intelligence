"""Uvicorn entry: `uvicorn app:app` — thin re-export of the FastAPI app."""

from api.app import app

__all__ = ["app"]
