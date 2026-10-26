"""Отдача HTML-витрины (одностраничник, читает /api/catalog)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

_INDEX = Path(__file__).resolve().parents[2] / "static" / "index.html"


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(_INDEX)
