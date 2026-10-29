"""Отдача HTML-страниц сайта (витрина + вход/регистрация)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

_STATIC = Path(__file__).resolve().parents[2] / "static"
_INDEX = _STATIC / "index.html"
_AUTH = _STATIC / "auth.html"
_VERIFY = _STATIC / "verify.html"
_RESET = _STATIC / "reset.html"
_REVIEWS = _STATIC / "reviews.html"
_FAVICON = _STATIC / "favicon.svg"
_OG = _STATIC / "og.png"


@router.api_route("/", methods=["GET", "HEAD"])
async def index() -> FileResponse:
    return FileResponse(_INDEX)


@router.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(_AUTH)


@router.get("/register")
async def register_page() -> FileResponse:
    return FileResponse(_AUTH)


@router.get("/account")
async def account_page() -> FileResponse:
    return FileResponse(_INDEX)


@router.get("/verify")
async def verify_page() -> FileResponse:
    return FileResponse(_VERIFY)


@router.get("/reset")
async def reset_page() -> FileResponse:
    return FileResponse(_RESET)


@router.get("/reviews")
async def reviews_page() -> FileResponse:
    return FileResponse(_REVIEWS)


@router.api_route("/favicon.svg", methods=["GET", "HEAD"])
async def favicon_svg() -> FileResponse:
    return FileResponse(_FAVICON, media_type="image/svg+xml")


@router.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon_ico() -> FileResponse:
    return FileResponse(_FAVICON, media_type="image/svg+xml")


@router.api_route("/og.png", methods=["GET", "HEAD"])
async def og_image() -> FileResponse:
    return FileResponse(_OG, media_type="image/png")
