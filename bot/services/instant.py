"""Скидочные товары: FazerCard «Instant use» (коды со скидкой).

Фоновый сканер проходит по уже добавленным обычным FazerCard-товарам
(гифт-карты и ключи), ищет их instant-use версии (у категории ``fzr_a``
оканчивается на ``_instant_use``, закуп ниже) и под каждую создаёт
НЕАКТИВНЫЙ номинал в скрытом разделе «🔥 Скидочные товары» с зеркальной
иерархией (бренд → страна → номинал) и той же наценкой.

Гарантия: скидочная цена строго ниже вашего обычного номинала (иначе не
создаём). По каждому новому — уведомление админу с кнопками «Активировать /
Больше не предлагать». Включение — по-товарное; сам раздел тоже скрыт, пока
его не включат в админке.

Instant-коды «не для хранения»: бот покупает их у FazerCard в момент заказа и
сразу отдаёт клиенту (buy-on-demand), поэтому сток не задействуется.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import HEAD_MERCHANT_ID, InstantCandidate, Product, Variant
from . import notify as notify_service
from . import pricing as pricing_service
from . import rates as rates_service
from . import settings as settings_service
from . import smartprice as smartprice_service

log = logging.getLogger("vallshop.instant")

INSTANT_SUFFIX = "_instant_use"
SECTION_TITLE = "🔥 Скидочные товары"
SECTION_PID_KEY = "instant_section_pid"
PARENT_TITLE = "Игры и Сервисы"       # под каким разделом размещать (по названию)
INSTANT_KINDS = ("giftcard", "gamekey")


def _norm(name) -> str:
    return " ".join(str(name or "").lower().split())


def _is_instant_cat(cat_id: Optional[str]) -> bool:
    return bool(cat_id) and str(cat_id).endswith(INSTANT_SUFFIX)


async def _offers_map(fzr, kind: str, cat: str) -> dict:
    """{offer_id: {"name","price_usd"}} для категории (или {} при ошибке/пусто)."""
    try:
        rows = await asyncio.to_thread(fzr.offers_for, kind, cat)
    except Exception:  # noqa: BLE001 — нет такой категории / сеть
        return {}
    out = {}
    for r in rows or []:
        oid = r.get("id")
        if oid is None:
            continue
        out[str(oid)] = {"name": r.get("name"), "price_usd": r.get("price_usd")}
    return out


async def _ensure_section(session, config: BotConfig) -> Product:
    """Найти/создать скрытый раздел «Скидочные товары» под «Игры и Сервисы»."""
    pid = await settings_service.get(session, SECTION_PID_KEY)
    if pid:
        sec = await session.get(Product, int(pid))
        if sec is not None:
            return sec
    # Родитель — товар с названием «Игры и Сервисы» (иначе верхний уровень).
    parent = await session.scalar(
        select(Product).where(Product.title == PARENT_TITLE).limit(1)
    )
    parent_id = parent.id if parent else None
    # Наверх по порядку.
    min_sort = await session.scalar(
        select(func.min(Product.sort_order)).where(
            Product.parent_id == parent_id if parent_id is not None
            else Product.parent_id.is_(None)
        )
    )
    sec = Product(
        game=SECTION_TITLE[:64], title=SECTION_TITLE[:255],
        parent_id=parent_id, sort_order=int(min_sort or 0) - 1,
        is_active=False,  # раздел скрыт, пока владелец не включит
    )
    session.add(sec)
    await session.flush()
    await settings_service.set(session, SECTION_PID_KEY, str(sec.id))
    return sec


async def _ancestors(session, product: Product) -> list:
    """Цепочка товаров от корня к листу: [root, ..., product]."""
    chain = []
    cur = product
    seen = set()
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        chain.append(cur)
        cur = await session.get(Product, cur.parent_id) if cur.parent_id else None
    chain.reverse()
    return chain


async def _child_by_title(session, parent_id: int, title: str) -> Optional[Product]:
    return await session.scalar(
        select(Product).where(Product.parent_id == parent_id, Product.title == title).limit(1)
    )


async def _mirror_path(session, section: Product, regular: Variant) -> Optional[Product]:
    """Воссоздать иерархию обычного товара под разделом; вернуть лист-товар."""
    reg_product = await session.get(Product, regular.product_id)
    if reg_product is None:
        return None
    chain = await _ancestors(session, reg_product)
    # Отбрасываем самый верхний корень (например «Игры и Сервисы»);
    # если товар сам был корнем — зеркалим его.
    mirror_chain = chain[1:] if len(chain) > 1 else chain
    parent = section
    for node in mirror_chain:
        child = await _child_by_title(session, parent.id, node.title)
        if child is None:
            child = Product(
                game=(node.title or "")[:64], title=(node.title or "")[:255],
                parent_id=parent.id, sort_order=node.sort_order,
                icon_emoji_id=node.icon_emoji_id, button_style=node.button_style,
                full_width=node.full_width, is_active=True,
            )
            session.add(child)
            await session.flush()
        parent = child
    return parent


def _eff_markup(variant: Variant, config: BotConfig) -> Decimal:
    if variant.markup_percent is not None:
        return Decimal(variant.markup_percent)
    return Decimal(config.default_markup_percent or 0)


async def scan(db: Database, fzr, bot: Bot, config: BotConfig) -> None:
    """Один проход сканера скидочных товаров."""
    if fzr is None or not (getattr(fzr, "mock", False) or getattr(fzr, "api_key", None)):
        return
    rate = rates_service.get_rate()

    # Все обычные FazerCard-номиналы (гифт/ключи), сгруппированные по категории.
    async with db.session() as session:
        regulars = list(
            await session.scalars(
                select(Variant).where(
                    Variant.source == "fazercard",
                    Variant.fzr_kind.in_(INSTANT_KINDS),
                    Variant.is_active.is_(True),
                )
            )
        )
    by_cat: dict = {}
    for v in regulars:
        if _is_instant_cat(v.fzr_a):
            continue
        by_cat.setdefault((v.fzr_kind, v.fzr_a), []).append(v.id)

    for (kind, cat), var_ids in by_cat.items():
        instant_cat = f"{cat}{INSTANT_SUFFIX}"
        instant = await _offers_map(fzr, kind, instant_cat)
        if not instant:
            continue  # у этой категории нет instant-версии
        regular = await _offers_map(fzr, kind, cat)
        instant_by_name = {_norm(o["name"]): (oid, o) for oid, o in instant.items()}

        for vid in var_ids:
            try:
                await _process_one(db, bot, config, kind, cat, instant_cat,
                                   vid, regular, instant_by_name, rate)
            except Exception:  # noqa: BLE001 — один номинал не должен ронять проход
                log.exception("instant: ошибка обработки variant=%s", vid)


async def _process_one(db, bot, config, kind, cat, instant_cat, vid,
                       regular_offers, instant_by_name, rate) -> None:
    async with db.session() as session:
        v = await session.get(Variant, vid)
        if v is None:
            return
        reg_off = regular_offers.get(str(v.fzr_b))
        reg_name = reg_off["name"] if reg_off else None
        if not reg_name:
            return
        match = instant_by_name.get(_norm(reg_name))
        if not match:
            return
        inst_id, inst = match
        # Уже обрабатывали этот instant-оффер?
        exists = await session.scalar(
            select(InstantCandidate).where(
                InstantCandidate.fzr_a == instant_cat,
                InstantCandidate.fzr_b == str(inst_id),
            )
        )
        if exists is not None:
            return
        try:
            cost = Decimal(str(inst.get("price_usd")))
        except Exception:  # noqa: BLE001
            return
        markup = _eff_markup(v, config)
        new_price = smartprice_service.target_rub(cost, markup, rate)
        reg_price = pricing_service.price_rub_value(v, Decimal(config.default_markup_percent or 0))
        # Гарантия «строго дешевле»: иначе тихо пропускаем (не захламляем).
        if not (new_price > 0 and reg_price > 0 and new_price < reg_price):
            return

        section = await _ensure_section(session, config)
        leaf = await _mirror_path(session, section, v)
        if leaf is None:
            return
        min_vid = await session.scalar(
            select(func.min(Variant.liog_variation_id)).where(Variant.product_id == leaf.id)
        )
        new_lvid = min(0, int(min_vid or 0)) - 1
        iv = Variant(
            product_id=leaf.id,
            title=(inst.get("name") or v.title)[:255],
            liog_product_id=0, liog_variation_id=new_lvid,
            cost_usd=cost, markup_percent=markup, price_rub=new_price,
            price_mode=smartprice_service.MODE_SMART,
            source="fazercard", fzr_kind=kind,
            fzr_a=instant_cat, fzr_b=str(inst_id),
            icon_emoji_id=v.icon_emoji_id, description=v.description,
            pending_text=v.pending_text, delivered_text=v.delivered_text,
            is_active=False,  # по-товарное включение
        )
        session.add(iv)
        await session.flush()
        cand = InstantCandidate(
            fzr_kind=kind, fzr_a=instant_cat, fzr_b=str(inst_id),
            regular_variant_id=v.id, variant_id=iv.id, status="pending",
        )
        session.add(cand)
        await session.flush()
        path = " → ".join(p for p in [
            (await session.get(Product, leaf.id)).title, iv.title
        ] if p)
        cand_id = cand.id
        diff = reg_price - new_price
        reg_s = pricing_service._fmt_rub(reg_price) if hasattr(pricing_service, "_fmt_rub") else str(int(reg_price))
        new_s = pricing_service._fmt_rub(new_price) if hasattr(pricing_service, "_fmt_rub") else str(int(new_price))
        diff_s = pricing_service._fmt_rub(diff) if hasattr(pricing_service, "_fmt_rub") else str(int(diff))
        await session.commit()

    kbb = InlineKeyboardBuilder()
    kbb.row(InlineKeyboardButton(text="✅ Активировать", callback_data=f"inst_on:{cand_id}"))
    kbb.row(InlineKeyboardButton(text="🚫 Больше не предлагать", callback_data=f"inst_off:{cand_id}"))
    await notify_service.notify_admins(
        bot, config.admin_ids,
        f"🔥 <b>Скидочный товар найден</b>\n"
        f"{path}\n"
        f"Ваша цена: <b>{reg_s} ₽</b> → скидочная: <b>{new_s} ₽</b> "
        f"(дешевле на <b>{diff_s} ₽</b>)\n\n"
        f"Добавлен в «Скидочные товары» (пока скрыт). Активировать этот номинал?",
        reply_markup=kbb.as_markup(),
    )
