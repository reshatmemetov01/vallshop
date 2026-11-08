"""Отдача HTML-страниц сайта (витрина + вход/регистрация)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import seo
from ..db import get_session

router = APIRouter()


# Правовые документы (оферта/соглашение/политика) генерируются из bot.legal.
# Контакт поддержки и юрлицо берём из тех же переменных окружения, что и бот.
def _legal_config() -> SimpleNamespace:
    return SimpleNamespace(
        support_contact=os.getenv("SUPPORT_CONTACT", ""),
        legal_entity=os.getenv("LEGAL_ENTITY", ""),
    )

_STATIC = Path(__file__).resolve().parents[2] / "static"
_INDEX = _STATIC / "index.html"
_AUTH = _STATIC / "auth.html"
_VERIFY = _STATIC / "verify.html"
_RESET = _STATIC / "reset.html"
_REVIEWS = _STATIC / "reviews.html"
_ADMIN = _STATIC / "admin.html"
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


@router.get("/catalog")
async def catalog_page() -> FileResponse:
    return FileResponse(_INDEX)


@router.get("/redeem")
async def redeem_page() -> FileResponse:
    return FileResponse(_INDEX)


@router.api_route("/admin", methods=["GET", "HEAD"])
@router.api_route("/admin/", methods=["GET", "HEAD"])
async def admin_page() -> FileResponse:
    return FileResponse(_ADMIN)


@router.get("/notrack")
async def notrack(off: str | None = None) -> HTMLResponse:
    """Пометить браузер владельца, чтобы его визиты не считались (cookie).

    Открой в каждом браузере/устройстве, с которого сам заходишь на сайт.
    ?off — снять пометку (снова считать этот браузер).
    """
    if off is not None:
        resp = HTMLResponse(_notrack_html(
            "✅ Готово. Этот браузер снова считается в посещениях."))
        resp.delete_cookie("vs_notrack", path="/")
        return resp
    resp = HTMLResponse(_notrack_html(
        "🚫 Готово. Твои визиты с этого браузера больше не учитываются в статистике."))
    resp.set_cookie(
        "vs_notrack", "1", max_age=60 * 60 * 24 * 365 * 3,  # ~3 года
        path="/", samesite="lax",
    )
    return resp


def _notrack_html(msg: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex'><title>VallShop</title>"
        "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;background:#0a0f1a;color:#eef4f8;font-family:"
        "-apple-system,Segoe UI,Roboto,Arial,sans-serif;text-align:center;padding:24px}"
        ".c{max-width:440px}a{color:#3ba3f0}</style></head><body><div class='c'>"
        f"<div style='font-size:18px;font-weight:700;margin-bottom:10px'>{msg}</div>"
        "<div style='color:#a3b4c2;font-size:14px'>Можно закрыть эту страницу. "
        "<a href='/'>На сайт →</a></div></div></body></html>"
    )


@router.get("/verify")
async def verify_page() -> FileResponse:
    return FileResponse(_VERIFY)


@router.get("/reset")
async def reset_page() -> FileResponse:
    return FileResponse(_RESET)


@router.get("/reviews")
async def reviews_page() -> FileResponse:
    return FileResponse(_REVIEWS)


# --- Правовые документы --------------------------------------------------

@router.api_route("/offer", methods=["GET", "HEAD"])
@router.api_route("/terms", methods=["GET", "HEAD"])
async def offer_page() -> HTMLResponse:
    from bot import legal
    return HTMLResponse(legal.offer_html(_legal_config()))


@router.api_route("/agreement", methods=["GET", "HEAD"])
async def agreement_page() -> HTMLResponse:
    from bot import legal
    return HTMLResponse(legal.agreement_html(_legal_config()))


@router.api_route("/privacy", methods=["GET", "HEAD"])
async def privacy_page() -> HTMLResponse:
    from bot import legal
    return HTMLResponse(legal.privacy_html(_legal_config()))


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
