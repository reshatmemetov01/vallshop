"""Оформление меню: реестр кнопок + переопределения (текст/эмодзи/порядок/видимость).

Рендер клавиатур берёт данные из кэша (sync), админка меняет БД и обновляет кэш.
Настраиваемые меню: главное ("main"), профиль ("profile"), FAQ ("faq").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy import select

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, MenuButton
from ..services import msettings as msettings_service

log = logging.getLogger("vallshop.menu")


@dataclass
class Entry:
    key: str
    menu: str                 # "main" | "profile" | "faq"
    label: str                # дефолтный текст
    emoji_id: Optional[str]   # дефолтный custom_emoji id (или None)
    style: Optional[str]      # "primary"/"success"/"danger"/None
    kind: str                 # "cb" | "url"
    target: str               # callback_data, либо ключ URL (offer/agreement/privacy), либо прямой url
    order: int
    full_width: bool = False  # True — кнопка занимает всю строку (не встаёт в пару)
    plain: str = ""           # обычный эмодзи для дефолта франшиз (без премиум-иконки)


# Колонок в ряд для каждого меню.
MENU_COLUMNS = {"main": 2, "profile": 1, "faq": 2, "earn": 1}
MENU_TITLES = {
    "main": "Главное меню", "profile": "Профиль",
    "faq": "FAQ / Правила", "earn": "Заработок",
}
# У каких меню есть редактируемый текст экрана и ключ настройки.
MENU_CAPTION = {"main": "cap_main", "faq": "cap_faq"}

REGISTRY: List[Entry] = [
    # Главное меню: [Каталог, Профиль] / [Активировать Код] / [FAQ, Отзывы]
    Entry("main.catalog",  "main", "Каталог",            "5805550320985578625", "primary", "cb", "catalog", 10, plain="🛍"),
    Entry("main.profile",  "main", "Мой профиль",        "6035084557378654059", "success", "cb", "profile", 20, plain="👤"),
    Entry("main.activate", "main", "Активировать Код",   "5422711448914647622", "primary", "cb", "activate", 30, full_width=True, plain="🎟"),
    Entry("main.faq",      "main", "FAQ / Правила",      "6030848053177486888", "danger",  "cb", "faq", 40, plain="❓"),
    Entry("main.reviews",  "main", "Отзывы",             "5890925363067886150", "primary", "cb", "reviews", 50, plain="⭐"),
    Entry("main.earn",     "main", "Заработать",         "5904462880941545555", "success", "cb", "earn", 60, full_width=True, plain="💰"),
    # Профиль (каждая на своей строке)
    Entry("profile.balance",  "profile", "Мой баланс",          "5778421276024509124", "danger",  "cb", "balance", 10, plain="💳"),
    Entry("profile.myorders", "profile", "Мои заказы",          "5904359114531675993", "primary", "cb", "myorders", 20, plain="🧾"),
    Entry("profile.mytopups", "profile", "История пополнений",  "6039859895291877126", None,      "cb", "mytopups", 30, plain="💸"),
    # FAQ / Правила: [Техподдержка] / [Конфиденциальность, Соглашение] / [Оферта]
    Entry("faq.support",   "faq", "Техподдержка",                  "6021618194228187816", "primary", "url", "support", 10, full_width=True, plain="🆘"),
    Entry("faq.privacy",   "faq", "Политика конфиденциальности",   "5935757052042285202", None,      "url", "privacy", 20, plain="🔒"),
    Entry("faq.agreement", "faq", "Политика соглашения",           "6030445631921721471", None,      "url", "agreement", 30, plain="📄"),
    Entry("faq.offer",     "faq", "Публичная оферта",              "6030445631921721471", "success", "url", "offer", 40, full_width=True, plain="📑"),
    # Заработок: Реферальная система + Франшиза (франшиза видна только в головном
    # боте; её можно скрыть здесь, пока она в разработке).
    Entry("earn.ref",       "earn", "Реферальная система", "5879905000972358125", "primary", "cb", "ref", 10, full_width=True, plain="👥"),
    Entry("earn.franchise", "earn", "🏪 Франшиза",         None,                  "success", "cb", "fr_home", 20, full_width=True),
]

_BY_KEY: Dict[str, Entry] = {e.key: e for e in REGISTRY}

# Кэш переопределений в разрезе магазина:
#   _overrides[merchant_id][key] -> {"label","emoji_id","enabled","sort_order"}
#   _captions[merchant_id][skey] -> текст экрана
_overrides: Dict[int, Dict[str, dict]] = {}
_captions: Dict[int, Dict[str, str]] = {}
# Названия магазинов франшиз (для нейтральных дефолт-текстов): merchant_id -> title.
_titles: Dict[int, str] = {}


@dataclass
class Resolved:
    key: str
    text: str
    emoji_id: Optional[str]
    style: Optional[str]
    kind: str
    target: str
    enabled: bool
    order: int
    full_width: bool


def _resolve_one(e: Entry, merchant_id: int = HEAD_MERCHANT_ID) -> Resolved:
    ov = _overrides.get(merchant_id, {}).get(e.key, {})
    label = ov.get("label")
    emoji = ov.get("emoji_id", "__default__")
    is_franchise = merchant_id != HEAD_MERCHANT_ID
    # Эмодзи: у головного — премиум-иконка из реестра; у франшизы по умолчанию
    # премиум-иконки НЕТ (минимальный вид), только если владелец сам задал.
    if emoji == "__default__":
        emoji_id = None if is_franchise else e.emoji_id
    else:
        emoji_id = emoji or None
    # Текст: своя подпись важнее всего; у франшизы без своей иконки и без
    # переопределения — обычный эмодзи в тексте (🛍 Каталог).
    if label:
        text = label
    elif is_franchise and not emoji_id and e.plain:
        text = f"{e.plain} {e.label}"
    else:
        text = e.label
    return Resolved(
        key=e.key,
        text=text,
        emoji_id=emoji_id,
        style=e.style,
        kind=e.kind,
        target=e.target,
        enabled=ov.get("enabled", True),
        order=ov.get("sort_order") if ov.get("sort_order") is not None else e.order,
        full_width=e.full_width,
    )


def menu_entries(
    menu: str, merchant_id: int = HEAD_MERCHANT_ID, *, only_enabled: bool = True
) -> List[Resolved]:
    items = [_resolve_one(e, merchant_id) for e in REGISTRY if e.menu == menu]
    if only_enabled:
        items = [r for r in items if r.enabled]
    items.sort(key=lambda r: (r.order, r.key))
    return items


def get_entry(key: str) -> Optional[Entry]:
    return _BY_KEY.get(key)


def resolved(key: str, merchant_id: int = HEAD_MERCHANT_ID) -> Optional[Resolved]:
    e = _BY_KEY.get(key)
    return _resolve_one(e, merchant_id) if e else None


def _franchise_default_caption(menu: str, merchant_id: int) -> Optional[str]:
    """Нейтральный дефолт-текст для франшизы (без бренда платформы).

    Подставляет название магазина франшизы. Возвращает None, если для меню нет
    нейтрального шаблона (тогда используется переданный default).
    """
    from .. import texts
    shop = _titles.get(merchant_id) or "магазин"
    if menu == "main":
        return texts.START_FRANCHISE.format(shop=shop)
    if menu == "faq":
        return texts.FAQ_FRANCHISE
    return None


def caption(menu: str, default: str, merchant_id: int = HEAD_MERCHANT_ID) -> str:
    """Текст экрана: своё переопределение магазина → нейтральный дефолт для
    франшизы → переданный default (бренд платформы, для головного)."""
    skey = MENU_CAPTION.get(menu)
    if skey and _captions.get(merchant_id, {}).get(skey):
        return _captions[merchant_id][skey]
    if merchant_id != HEAD_MERCHANT_ID:
        fb = _franchise_default_caption(menu, merchant_id)
        if fb is not None:
            return fb
    return default


# ── Загрузка/обновление кэша ────────────────────────────────────────────────
async def load(db: Database) -> None:
    global _overrides, _captions, _titles
    from ..db.models import Merchant
    try:
        async with db.session() as session:
            mrows = list(await session.scalars(select(Merchant)))
            _titles = {m.id: m.title for m in mrows if m.title}
            rows = list(await session.scalars(select(MenuButton)))
            ov: Dict[int, Dict[str, dict]] = {}
            for r in rows:
                ov.setdefault(r.merchant_id, {})[r.key] = {
                    "label": r.label,
                    "emoji_id": r.emoji_id if r.emoji_id is not None else "__default__",
                    "enabled": bool(r.enabled),
                    "sort_order": r.sort_order,
                }
            _overrides = ov
            grouped = await msettings_service.all_grouped(session)
            cap_keys = set(MENU_CAPTION.values())
            _captions = {
                mid: {k: v for k, v in vals.items() if k in cap_keys}
                for mid, vals in grouped.items()
            }
    except Exception:  # noqa: BLE001 — кэш не должен ронять бота
        log.exception("Не удалось загрузить оформление меню")


async def _get_or_create(session, key: str, merchant_id: int) -> MenuButton:
    row = await session.get(MenuButton, {"key": key, "merchant_id": merchant_id})
    if row is None:
        e = _BY_KEY[key]
        row = MenuButton(key=key, merchant_id=merchant_id, enabled=True, sort_order=e.order)
        session.add(row)
        await session.flush()
    return row


async def set_label(
    db: Database, key: str, label: Optional[str], merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    async with db.session() as session:
        row = await _get_or_create(session, key, merchant_id)
        row.label = label  # None = сброс к дефолту
        await session.commit()
    await load(db)


async def set_emoji(
    db: Database, key: str, emoji_id: Optional[str], merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    """emoji_id: строка id, "" (без эмодзи) или None (сброс к дефолту)."""
    async with db.session() as session:
        row = await _get_or_create(session, key, merchant_id)
        row.emoji_id = emoji_id
        await session.commit()
    await load(db)


async def toggle(db: Database, key: str, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    async with db.session() as session:
        row = await _get_or_create(session, key, merchant_id)
        row.enabled = not bool(row.enabled)
        await session.commit()
    await load(db)


async def move(
    db: Database, key: str, direction: int, merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    """Переместить кнопку вверх (-1) или вниз (+1) внутри её меню."""
    e = _BY_KEY[key]
    ordered = menu_entries(e.menu, merchant_id, only_enabled=False)
    idx = next((i for i, r in enumerate(ordered) if r.key == key), None)
    if idx is None:
        return
    swap = idx + direction
    if swap < 0 or swap >= len(ordered):
        return
    a, b = ordered[idx], ordered[swap]
    async with db.session() as session:
        ra = await _get_or_create(session, a.key, merchant_id)
        rb = await _get_or_create(session, b.key, merchant_id)
        ra.sort_order, rb.sort_order = b.order, a.order
        await session.commit()
    await load(db)


async def reset_all(db: Database, merchant_id: int = HEAD_MERCHANT_ID) -> None:
    """Сбросить оформление магазина к значениям по умолчанию (реестр)."""
    from sqlalchemy import delete as sa_delete
    from ..db.models import MerchantSetting
    async with db.session() as session:
        await session.execute(sa_delete(MenuButton).where(MenuButton.merchant_id == merchant_id))
        await session.execute(
            sa_delete(MerchantSetting).where(
                MerchantSetting.merchant_id == merchant_id,
                MerchantSetting.key.in_(set(MENU_CAPTION.values())),
            )
        )
        await session.commit()
    await load(db)


async def set_caption(
    db: Database, menu: str, text: Optional[str], merchant_id: int = HEAD_MERCHANT_ID
) -> None:
    skey = MENU_CAPTION.get(menu)
    if not skey:
        return
    async with db.session() as session:
        await msettings_service.set(session, merchant_id, skey, text)
        await session.commit()
    await load(db)
