"""SEO: серверные (SSR) страницы товаров, sitemap.xml, robots.txt.

Задача — дать поисковикам готовый HTML с содержимым и отдельные URL на каждую
категорию/товар (`/p/<id>-<slug>`), с правильными title/description и
микроразметкой. Покупка выполняется в интерактивной витрине (SPA): кнопка
«Купить» ведёт в магазин с открытым товаром (`/?open=<id>`).
"""

from __future__ import annotations

import html
import re
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Product
from bot.services import catalog as catalog_service
from bot.services import mprice as mprice_service
from bot.services import stock as stock_service
from bot.services.pricing import owner_price_rub

from .config import settings
from .textfmt import clean_text

_ONDEMAND = {"fazercard", "socproof", "vpnresellers"}

# Транслитерация для slug (кириллица → латиница).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    out = []
    for ch in text:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug or "tovar"


def _base() -> str:
    return (settings.base_url or "https://vallshop.com").rstrip("/")


def product_url(p: Product) -> str:
    return f"/p/{p.id}-{slugify(clean_text(p.title) or p.title)}"


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _rub(v) -> str:
    v = Decimal(v or 0)
    return (f"{int(v)}" if v == v.to_integral_value() else f"{v:.2f}") + " ₽"


async def _parents(session: AsyncSession, p: Product) -> list:
    chain = []
    cur = p
    seen = set()
    while cur.parent_id and cur.parent_id not in seen:
        seen.add(cur.parent_id)
        parent = await catalog_service.get_product(session, cur.parent_id)
        if not parent:
            break
        chain.append(parent)
        cur = parent
    return list(reversed(chain))


def _layout(title: str, description: str, canonical: str, body: str, jsonld: str = "") -> str:
    base = _base()
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(canonical)}">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="theme-color" content="#ff8a2e">
<meta property="og:type" content="website">
<meta property="og:site_name" content="VallShop">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:url" content="{_e(canonical)}">
<meta property="og:image" content="{base}/og.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{base}/og.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Kaushan+Script&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/catalog.css">
{jsonld}
</head>
<body>
<header><div class="wrap bar">
  <a href="/" class="logo">Vall<span>Shop</span></a>
  <nav class="nav">
    <a href="/">Каталог</a>
    <a href="/reviews">Отзывы</a>
    <a href="/" class="cta">Открыть магазин</a>
  </nav>
</div></header>
<main class="wrap">
{body}
</main>
<footer><div class="wrap foot">
  <div>© VallShop · цифровые товары и услуги</div>
  <div><a href="/">Каталог</a> · <a href="/reviews">Отзывы</a> · <a href="/">Пополнить баланс</a></div>
</div></footer>
</body>
</html>"""


async def product_page(session: AsyncSession, product_id: int):
    """Вернуть (html, canonical_path) или None, если товар не найден/скрыт."""
    product = await catalog_service.get_product(session, product_id)
    if not product or not product.is_active:
        return None

    ptitle = clean_text(product.title) or product.title
    canonical_path = product_url(product)
    canonical = _base() + canonical_path
    parents = await _parents(session, product)

    # Хлебные крошки (видимые + микроразметка).
    crumb_items = [("Главная", _base() + "/")]
    for pr in parents:
        crumb_items.append((clean_text(pr.title) or pr.title, _base() + product_url(pr)))
    crumb_html = '<nav class="crumbs"><a href="/">🏠 Главная</a>'
    for pr in parents:
        crumb_html += (f' <span class="sep">›</span> '
                       f'<a href="{_e(product_url(pr))}">{_e(clean_text(pr.title) or pr.title)}</a>')
    crumb_html += f' <span class="sep">›</span> <b>{_e(ptitle)}</b></nav>'

    children = await catalog_service.list_children(session, product_id)
    jsonld_breadcrumb = _breadcrumb_jsonld(crumb_items + [(ptitle, canonical)])

    if children:
        cards = ""
        for c in children:
            cards += (
                f'<a class="card" href="{_e(product_url(c))}">'
                f'<div class="t">{_e(clean_text(c.title) or c.title)}</div>'
                f'<div class="d">{_e(clean_text(c.description) or "Открыть раздел и выбрать номинал")}</div>'
                f'<div class="arr">Открыть →</div></a>'
            )
        desc = clean_text(product.description) or (
            f"{ptitle} в VallShop — выберите раздел, оплатите с баланса "
            f"(карта, СБП, USDT) и получите заказ мгновенно."
        )
        body = (
            f"{crumb_html}"
            f"<h1>{_e(ptitle)}</h1>"
            f'<p class="lead">{_e(desc)}</p>'
            f'<div class="grid">{cards}</div>'
            f"{_seo_block(ptitle)}"
        )
        title = f"{ptitle} — купить в VallShop | цифровые товары"
        return _layout(title, desc, canonical, body, jsonld_breadcrumb), canonical_path

    # Листовой товар с номиналами.
    variants = await catalog_service.list_variants(session, product_id)
    owner_prices = {v.id: owner_price_rub(v, settings.default_markup_percent) for v in variants}
    client = await mprice_service.client_prices(session, settings.merchant_id, owner_prices)
    counts = await stock_service.counts_by_variant(session, [v.id for v in variants])

    rows = ""
    prices_num = []
    for v in variants:
        src = v.source or "stock"
        available = True if src in _ONDEMAND else int(counts.get(v.id, 0) or 0) > 0
        price = client.get(v.id)
        price_txt = (_rub(price) + (" / 1000" if src == "socproof" else "")) if price and price > 0 else "—"
        if price and price > 0:
            prices_num.append(Decimal(price))
        badge = ('<span class="badge ok">в наличии</span>' if available
                 else '<span class="badge bad">нет в наличии</span>')
        vdesc = clean_text(v.description)
        desc = f'<div class="vd">{_e(vdesc)}</div>' if vdesc else ""
        btn = (f'<a class="buy" href="/?open={product.id}" rel="nofollow">Купить</a>' if available
               else '<span class="buy off">Нет в наличии</span>')
        rows += (
            f'<div class="vcard"><div class="vt">{_e(clean_text(v.title) or v.title)}</div>'
            f'<div class="vp">{price_txt.replace(" / 1000", " <small>/ 1000</small>")}</div>'
            f'{badge}{desc}{btn}</div>'
        )

    lo = min(prices_num) if prices_num else None
    hi = max(prices_num) if prices_num else None
    price_hint = (f" Цены от {_rub(lo)}." if lo else "")
    desc = clean_text(product.description) or (
        f"Купить {ptitle} в VallShop: быстрая выдача, оплата картой, СБП или USDT.{price_hint}"
    )
    title = f"{ptitle} — купить, цены | VallShop"
    body = (
        f"{crumb_html}"
        f"<h1>Купить {_e(ptitle)}</h1>"
        f'<p class="lead">{_e(desc)}</p>'
        f'<div class="chips"><span class="chip">⚡ Мгновенная выдача</span>'
        f'<span class="chip">💳 Карта · СБП · USDT</span><span class="chip">🛡️ Оплата с баланса</span></div>'
        f'<div class="vgrid">{rows}</div>'
        f"{_seo_block(ptitle)}"
    )
    jsonld = jsonld_breadcrumb
    if lo:
        jsonld += _product_jsonld(ptitle, desc, lo, hi, canonical)
    return _layout(title, desc, canonical, body, jsonld), canonical_path


def _seo_block(title: str) -> str:
    t = _e(title)
    return (
        f'<div class="seo-text"><h2>Почему выгодно покупать {t} в VallShop</h2>'
        f"<p>VallShop — магазин цифровых товаров и услуг: Telegram Stars и Premium, "
        f"пополнение Steam, игровые сервисы, SMM-накрутка и VPN. Пополните баланс "
        f"удобным способом (банковская карта, СБП или USDT) и оформляйте заказы в один клик — "
        f"выдача происходит мгновенно или в короткий срок, а статус виден в личном кабинете.</p>"
        f"<p>Оплата проходит через проверенных платёжных провайдеров, покупки совершаются "
        f"с внутреннего баланса. Поддержка на связи — поможем с любым вопросом по заказу {t}.</p></div>"
    )


def _jsonld(obj_json: str) -> str:
    return f'<script type="application/ld+json">{obj_json}</script>'


def _breadcrumb_jsonld(items: list) -> str:
    import json
    el = [
        {"@type": "ListItem", "position": i + 1, "name": name, "item": url}
        for i, (name, url) in enumerate(items)
    ]
    data = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": el}
    return _jsonld(json.dumps(data, ensure_ascii=False))


def _product_jsonld(name: str, desc: str, lo: Decimal, hi: Optional[Decimal], url: str) -> str:
    import json
    offers = {
        "@type": "AggregateOffer",
        "priceCurrency": "RUB",
        "lowPrice": float(lo),
        "highPrice": float(hi if hi is not None else lo),
        "availability": "https://schema.org/InStock",
        "url": url,
    }
    data = {
        "@context": "https://schema.org", "@type": "Product",
        "name": name, "description": desc, "brand": {"@type": "Brand", "name": "VallShop"},
        "offers": offers,
    }
    return _jsonld(json.dumps(data, ensure_ascii=False))


async def sitemap_xml(session: AsyncSession) -> str:
    base = _base()
    urls = [(base + "/", "1.0"), (base + "/reviews", "0.5")]
    # Все активные товары (все уровни) рекурсивно.
    async def walk(parent_id):
        for p in await catalog_service.list_products(session, parent_id=parent_id):
            urls.append((base + product_url(p), "0.8"))
            await walk(p.id)
    await walk(None)
    body = "".join(
        f"<url><loc>{_e(loc)}</loc><changefreq>daily</changefreq><priority>{pr}</priority></url>"
        for loc, pr in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


def robots_txt() -> str:
    base = _base()
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /account\n"
        "Disallow: /login\n"
        "Disallow: /register\n"
        "Disallow: /api/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
