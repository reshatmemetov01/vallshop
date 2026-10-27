"""Отдача HTML-страниц сайта (витрина + вход/регистрация)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

_STATIC = Path(__file__).resolve().parents[2] / "static"
_INDEX = _STATIC / "index.html"
_AUTH = _STATIC / "auth.html"


@router.api_route("/", methods=["GET", "HEAD"])
async def index() -> FileResponse:
    return FileResponse(_INDEX)


@router.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(_AUTH)


@router.get("/register")
async def register_page() -> FileResponse:
    return FileResponse(_AUTH)
