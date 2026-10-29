"""Отдача HTML-страниц сайта (витрина + вход/регистрация)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import seo
from ..db import get_session

router = APIRouter()

_STATIC = Path(__file__).resolve().parents[2] / "static"
_INDEX = _STATIC / "index.html"
_AUTH = _STATIC / "auth.html"
_VERIFY = _STATIC / "verify.html"
_RESET = _STATIC / "reset.html"
_REVIEWS = _STATIC / "reviews.html"
_FAVICON = _STATIC / "favicon.svg"
_OG = _STATIC / "og.png"
_YANDEX = _STATIC / "yandex_efe8bde404b85b63.html"
_CATALOG_CSS = _STATIC / "catalog.css"


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


@router.api_route("/yandex_efe8bde404b85b63.html", methods=["GET", "HEAD"])
async def yandex_verify() -> FileResponse:
    return FileResponse(_YANDEX, media_type="text/html")


@router.api_route("/catalog.css", methods=["GET", "HEAD"])
async def catalog_css() -> FileResponse:
    return FileResponse(_CATALOG_CSS, media_type="text/css")


# --- SEO: серверные страницы, sitemap, robots -----------------------------

@router.api_route("/robots.txt", methods=["GET", "HEAD"])
async def robots_txt() -> PlainTextResponse:
    return PlainTextResponse(seo.robots_txt())


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"])
async def sitemap_xml(session: AsyncSession = Depends(get_session)) -> Response:
    return Response(await seo.sitemap_xml(session), media_type="application/xml")


@router.api_route("/p/{slug}", methods=["GET", "HEAD"])
async def product_page(slug: str, session: AsyncSession = Depends(get_session)):
    # slug = "<id>-<транслитерация>"; берём ведущее число.
    head = slug.split("-", 1)[0]
    if not head.isdigit():
        raise HTTPException(status_code=404)
    result = await seo.product_page(session, int(head))
    if result is None:
        raise HTTPException(status_code=404)
    page_html, canonical_path = result
    # нормализуем URL: если slug не совпал с каноническим — 301 на правильный.
    if f"/p/{slug}" != canonical_path:
        return RedirectResponse(canonical_path, status_code=301)
    return HTMLResponse(page_html)
