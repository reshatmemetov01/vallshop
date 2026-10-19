"""Клавиатуры бота.

Цвет кнопок (Bot API ``style``): success → 🟢, danger → 🔴, primary → 🔵.
Кастом-эмодзи (премиум) на кнопках — ``icon_custom_emoji_id`` (нужен Premium у
владельца бота). Переключатели: BUTTON_STYLES=0 и CUSTOM_EMOJI=0.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import List, Mapping, Optional

from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db.models import Product, Variant
from .texts import OUT_OF_STOCK_NOTE, money, rub


def _truthy(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


_STYLES = _truthy(os.getenv("BUTTON_STYLES", "1"))
_CUSTOM_EMOJI = _truthy(os.getenv("CUSTOM_EMOJI", "1"))

# Кастом-эмодзи (custom_emoji_id) для кнопок.
EMOJI = {
    "catalog": "5805550320985578625",
    "profile": "6035084557378654059",
    "balance": "5778421276024509124",
    "orders": "5904359114531675993",
    "topups": "6039859895291877126",
    "topup": "5890848474563352982",
    "back": "6039539366177541657",  # назад/отмена/меню
    "product": "5298953332079999355",   # товар PUBG Mobile
    "variant": "5242743089327513390",   # номиналы UC
    "admin": "5778570255555105942",     # админ-панель
    "copy": "6028171274939797252",       # скопировать адрес
    "paylink": "5902206159095339799",    # страница оплаты
    "check": "6039859895291877126",      # проверить оплату
    "faq": "6030848053177486888",        # FAQ / Правила
    "offer": "6030445631921721471",      # публичная оферта
    "agreement": "6030445631921721471",  # пользовательское соглашение
    "privacy": "5935757052042285202",    # политика конфиденциальности
    "support": "6021618194228187816",    # техподдержка
    "activate": "5422711448914647622",   # активировать код
    "review_leave": "6028338546736107668",  # оставить отзыв
    "buy_more": "6028205772117118673",      # купить ещё
    "reviews": "5890925363067886150",       # отзывы
    "earn": "5904462880941545555",          # заработать
    "referral": "5879905000972358125",      # реферальная система
    "usdt": "5891105528356018797",          # способ оплаты USDT
}

SUPPORT_URL = "https://t.me/vallmanager"


def _as_url(v: Optional[str]) -> Optional[str]:
    """Привести контакт к валидному URL: @user/t.me/username → https://t.me/…."""
    if not v:
        return None
    v = v.strip()
    if v.startswith(("http://", "https://", "tg://")):
        return v
    if v.startswith("@"):
        return "https://t.me/" + v[1:]
    return "https://t.me/" + v


def _b(
    text: str, *, style: Optional[str] = None, icon: Optional[str] = None, **kw
) -> InlineKeyboardButton:
    """Кнопка с опциональным цветом (style) и кастом-эмодзи (icon)."""
    if style and _STYLES:
        kw["style"] = style
    if icon and _CUSTOM_EMOJI:
        kw["icon_custom_emoji_id"] = icon
    return InlineKeyboardButton(text=text, **kw)


# ── Главное меню (настраивается в «Оформление») ───────────────────────────────
def _render_menu(menu: str, url_map: Optional[dict] = None, merchant_id: int = 0):
    """Собрать ряды кнопок меню из реестра с учётом переопределений магазина."""
    from .services import menu as menu_service

    url_map = url_map or {}
    cols = menu_service.MENU_COLUMNS.get(menu, 1)
    kb = InlineKeyboardBuilder()

    def _mk(r):
        if r.kind == "url":
            if r.target == "support":
                # Франшиза — свой контакт (из url_map); головной — дефолт платформы.
                # Нормализуем @username/t.me в валидный URL (иначе Telegram отклонит
                # всю клавиатуру и экран «не откроется»).
                url = _as_url(url_map.get("support")) or (SUPPORT_URL if merchant_id == 0 else None)
            else:
                url = url_map.get(r.target)
            if not url:
                return None  # нет ссылки — пропускаем
            # Значение вида "cb:<data>" — документ в самом боте (кнопка-колбэк),
            # иначе обычная ссылка (url-кнопка).
            if isinstance(url, str) and url.startswith("cb:"):
                return _b(r.text, callback_data=url[3:], style=r.style, icon=r.emoji_id or None)
            return _b(r.text, url=url, style=r.style, icon=r.emoji_id or None)
        return _b(r.text, callback_data=r.target, style=r.style, icon=r.emoji_id or None)

    # Раскладка: обычные кнопки встают парами (по cols), full_width — на всю строку.
    # Позиции стабильны: скрытие/показ не перетасовывает соседей по рядам.
    buf: list = []

    def _flush():
        if buf:
            kb.row(*buf)
            buf.clear()

    for r in menu_service.menu_entries(menu, merchant_id):
        btn = _mk(r)
        if btn is None:
            continue
        if r.full_width or cols <= 1:
            _flush()
            kb.row(btn)
        else:
            buf.append(btn)
            if len(buf) >= cols:
                _flush()
    _flush()
    return kb


def main_menu_kb(
    is_admin: bool = False, is_merchant_admin: bool = False, merchant_id: int = 0
) -> InlineKeyboardMarkup:
    kb = _render_menu("main", merchant_id=merchant_id)
    # Админ-панель платформы — только в головном боте (не в ботах франшиз).
    if is_admin and merchant_id == 0:
        kb.row(_b("Админ-панель", callback_data="admin", style="danger", icon=EMOJI["admin"]))
    if is_merchant_admin:
        kb.row(_b("🏪 Кабинет франшизы", callback_data="ma_home", style="success"))
    return kb.as_markup()


# Текст постоянной нижней кнопки «Меню» (reply-клавиатура).
MENU_BUTTON_TEXT = "🏠 Меню"


def persistent_menu_kb() -> ReplyKeyboardMarkup:
    """Постоянная нижняя кнопка «Меню» — всегда видна, быстрый возврат в меню."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Нажмите «🏠 Меню» для возврата",
    )


def faq_kb(
    offer_url: Optional[str] = None,
    agreement_url: Optional[str] = None,
    privacy_url: Optional[str] = None,
    merchant_id: int = 0,
    support_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    kb = _render_menu("faq", {
        "offer": offer_url, "agreement": agreement_url,
        "privacy": privacy_url, "support": support_url,
    }, merchant_id=merchant_id)
    kb.row(_b("Меню", callback_data="menu", icon=EMOJI["back"]))
    return kb.as_markup()


# ── Каталог ───────────────────────────────────────────────────────────────────
def products_kb(
    products: List[Product],
    back: str = "menu",
    back_text: str = "Меню",
    columns: int = 1,
    overrides: Optional[Mapping[int, dict]] = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    cols = max(1, columns)
    buf: list = []
    overrides = overrides or {}

    def _flush():
        if buf:
            kb.row(*buf)
            buf.clear()

    for p in products:
        ov = overrides.get(p.id)
        title = (ov.get("title") if ov else None) or p.title
        if ov and ov.get("emoji_id") is not None:
            icon = ov["emoji_id"] or None          # "" → без эмодзи
        else:
            icon = getattr(p, "icon_emoji_id", None) or EMOJI["product"]
        # None (не задан) → синий по умолчанию; "" → без цвета (обычная кнопка).
        raw_style = getattr(p, "button_style", None)
        style = "primary" if raw_style is None else (raw_style or None)
        btn = _b(title, callback_data=f"prod:{p.id}", style=style, icon=icon)
        if getattr(p, "full_width", False) or cols <= 1:
            _flush()
            kb.row(btn)
        else:
            buf.append(btn)
            if len(buf) >= cols:
                _flush()
    _flush()
    kb.row(_b(back_text, callback_data=back, icon=EMOJI["back"]))
    return kb.as_markup()


def variants_kb(
    variants: List[Variant],
    prices: Mapping[int, str],  # id -> готовая строка цены (₽ / $)
    stock: Mapping[int, int],
    currency: str = "USDT",
    product_icon: Optional[str] = None,
    back: str = "catalog",
    overrides: Optional[Mapping[int, dict]] = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    overrides = overrides or {}
    for v in variants:
        price = prices[v.id]  # уже отформатированная строка (₽ / $)
        src = getattr(v, "source", "stock")
        out = False
        if src == "vpnresellers":
            # VPN: выдача мгновенная от провайдера, своего стока нет — «под заказ» не показываем.
            note = " · ✅ в наличии"
        elif src == "fazercard":
            st = stock.get(v.id)
            if st is None:
                note = " · ✅ в наличии"       # оффер есть у поставщика, под заказ
            elif st > 0:
                note = f" · ✅ {st} шт"
            else:
                note = " · ❌ нет в наличии"    # st == 0 (нет у поставщика)
                out = True
        else:
            in_stock = stock.get(v.id, 0) or 0
            out = in_stock <= 0
            note = "" if in_stock > 0 else f" · {OUT_OF_STOCK_NOTE}"
        ov = overrides.get(v.id)
        title = (ov.get("title") if ov else None) or v.title
        if ov and ov.get("emoji_id") is not None:
            icon = ov["emoji_id"] or None          # "" → без эмодзи
        else:
            icon = getattr(v, "icon_emoji_id", None) or product_icon or EMOJI["variant"]
        # Нет в наличии — красная кнопка; иначе обычная.
        style = "danger" if out else None
        kb.row(_b(f"{title} — {price}{note}", callback_data=f"var:{v.id}", icon=icon, style=style))
    kb.row(_b("Назад", callback_data=back, icon=EMOJI["back"]))
    return kb.as_markup()


def buy_kb(variant_id: int, back: str = "catalog") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_b("💳 Купить", callback_data=f"buy:{variant_id}", style="success"))
    kb.row(_b("Назад", callback_data=back, icon=EMOJI["back"]))
    return kb.as_markup()


def out_of_stock_kb(back: str = "catalog") -> InlineKeyboardMarkup:
    """Экран номинала без «Купить» — товара нет в наличии."""
    kb = InlineKeyboardBuilder()
    kb.row(_b("❌ Нет в наличии", callback_data="noop", style="danger"))
    kb.row(_b("Назад", callback_data=back, icon=EMOJI["back"]))
    return kb.as_markup()


def not_enough_balance_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_b("Пополнить баланс", callback_data="topup", style="success", icon=EMOJI["topup"]))
    kb.row(_b("Назад", callback_data="menu", icon=EMOJI["back"]))
    return kb.as_markup()


def broadcast_post_kb(buttons, broadcast_id: Optional[int] = None) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура под постом рассылки из списка кнопок админа.

    Кнопка: ``{"label", "kind": "cb"|"url", "target", "style", "icon"}``.
    По одной в ряд. Пустой список → None (сообщение без клавиатуры).

    Если задан ``broadcast_id`` — кнопки-разделы (cb) получают tracking-колбэк
    ``bck:<id>:<idx>:<target>`` для подсчёта кликов (middleware запишет клик и
    перепишет data в ``target``). Без id (напр. предпросмотр) — обычный target.
    Клики по url-кнопкам Telegram не сообщает — они остаются обычными ссылками.
    """
    if not buttons:
        return None
    kb = InlineKeyboardBuilder()
    for idx, b in enumerate(buttons):
        label = b.get("label") or "Кнопка"
        style = b.get("style") or None
        icon = b.get("icon") or None
        if b.get("kind") == "url":
            url = _as_url(b.get("target"))
            if not url:
                continue
            kb.row(_b(label, url=url, style=style, icon=icon))
        else:
            target = b.get("target")
            if not target:
                continue
            data = f"bck:{broadcast_id}:{idx}:{target}" if broadcast_id is not None else target
            kb.row(_b(label, callback_data=data, style=style, icon=icon))
    markup = kb.as_markup()
    return markup if markup.inline_keyboard else None


def channel_post_kb(buttons, post_id: int) -> Optional[InlineKeyboardMarkup]:
    """Клавиатура поста в канал. Кнопки-разделы → трекинг-колбэк cpk:<id>:<idx>:<target>
    (клик считается, бот открывается по deep-link); ссылки — обычные url (не считаются).
    По одной в ряд."""
    if not buttons:
        return None
    kb = InlineKeyboardBuilder()
    for idx, b in enumerate(buttons):
        label = b.get("label") or "Кнопка"
        style = b.get("style") or None
        icon = b.get("icon") or None
        if b.get("kind") == "url":
            url = _as_url(b.get("target"))
            if not url:
                continue
            kb.row(_b(label, url=url, style=style, icon=icon))
        else:
            target = b.get("target") or ""
            kb.row(_b(label, callback_data=f"cpk:{post_id}:{idx}:{target}", style=style, icon=icon))
    markup = kb.as_markup()
    return markup if markup.inline_keyboard else None


def after_purchase_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_b("Каталог", callback_data="catalog", style="primary", icon=EMOJI["catalog"]))
    kb.row(
        _b("Профиль", callback_data="profile", style="success", icon=EMOJI["profile"]),
        _b("Меню", callback_data="menu", icon=EMOJI["back"]),
    )
    return kb.as_markup()


def order_done_kb(order_id: int, variant_id: int) -> InlineKeyboardMarkup:
    """Кнопки под сообщением о выполненном заказе."""
    kb = InlineKeyboardBuilder()
    kb.row(
        _b("Оставить отзыв", callback_data=f"review:{order_id}",
           style="success", icon=EMOJI["review_leave"]),
        _b("Купить ещё", callback_data=f"var:{variant_id}",
           style="primary", icon=EMOJI["buy_more"]),
    )
    kb.row(_b("Меню", callback_data="menu", icon=EMOJI["back"]))
    return kb.as_markup()


def earn_kb(show_franchise: bool = False, merchant_id: int = 0) -> InlineKeyboardMarkup:
    """Меню «Заработок» из реестра оформления (видимость кнопок — в Оформлении).

    Кнопка «Франшиза» показывается только в головном боте (show_franchise) и
    только если не скрыта админом в разделе Оформление.
    """
    from .services import menu as menu_service
    kb = InlineKeyboardBuilder()
    for r in menu_service.menu_entries("earn", merchant_id):
        if r.key == "earn.franchise" and not show_franchise:
            continue
        kb.row(_b(r.text, callback_data=r.target, style=r.style, icon=r.emoji_id or None))
    kb.row(_b("Назад", callback_data="menu", icon=EMOJI["back"]))
    return kb.as_markup()


def referral_kb(can_transfer: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_transfer:
        kb.row(_b("Перевести на основной", callback_data="ref_transfer", style="success", icon=EMOJI["topup"]))
    kb.row(_b("Назад", callback_data="earn", icon=EMOJI["back"]))
    return kb.as_markup()


def review_rating_kb(order_id: int) -> InlineKeyboardMarkup:
    """Выбор оценки 1–5 в один ряд + Меню."""
    kb = InlineKeyboardBuilder()
    for n in (1, 2, 3, 4, 5):
        kb.button(text=str(n), callback_data=f"rate:{order_id}:{n}")
    kb.adjust(5)
    kb.row(_b("Меню", callback_data="menu", style="danger", icon=EMOJI["back"]))
    return kb.as_markup()


def quantity_kb(
    variant_id: int, qty: int, total: Decimal, currency: str, max_qty: int
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    dec = f"qty:{variant_id}:{qty - 1}" if qty > 1 else "noop"
    inc = f"qty:{variant_id}:{qty + 1}" if qty < max_qty else "noop"
    kb.row(
        _b("➖", callback_data=dec),
        _b(f"{qty} шт", callback_data="noop"),
        _b("➕", callback_data=inc),
    )
    kb.row(
        _b(
            f"✅ Купить за {rub(total)}",
            callback_data=f"confirm:{variant_id}:{qty}",
            style="success",
        )
    )
    kb.row(_b("Назад", callback_data=f"var:{variant_id}", icon=EMOJI["back"]))
    return kb.as_markup()


# ── Названия сетей ────────────────────────────────────────────────────────────
NETWORK_LABELS = {
    "TRC20": "TRON (TRC20)",
    "BEP20": "BNB Chain (BEP20)",
    "ERC20": "Ethereum (ERC20)",
    "POLYGON": "Polygon",
    "SOLANA": "Solana",
}
NETWORK_STYLES = {"TRC20": "danger", "SOLANA": "success"}


def network_label(net: str) -> str:
    return NETWORK_LABELS.get(net.upper(), net.upper())


# ── Профиль ───────────────────────────────────────────────────────────────────
def profile_kb(merchant_id: int = 0) -> InlineKeyboardMarkup:
    kb = _render_menu("profile", merchant_id=merchant_id)
    kb.row(_b("Меню", callback_data="menu", icon=EMOJI["back"]))
    return kb.as_markup()


def balance_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_b("Пополнить баланс", callback_data="topup", style="success", icon=EMOJI["topup"]))
    kb.row(_b("Назад", callback_data="profile", icon=EMOJI["back"]))
    return kb.as_markup()


# Премиум-эмодзи иконок способов оплаты по id способа.
_PAY_EMOJI = {
    "card": "5445353829304387411",
    "sbp": "5265074015868822600",
    "sbp_ph": "5265074015868822600",
    "usdt": EMOJI["usdt"],
}


def topup_methods_kb(methods: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Выбор способа пополнения.

    ``methods`` — список словарей ``{id, label}`` (уже отфильтрованных: только
    включённые и с доступным провайдером). ``usdt`` ведёт в свой флоу выбора
    сети, остальные (card/sbp) — в единый рублёвый флоу ``tu_rub:<id>``.
    """
    kb = InlineKeyboardBuilder()
    for m in (methods or []):
        mid = m["id"]
        emo = _PAY_EMOJI.get(mid)
        if mid == "usdt":
            kb.row(_b(m["label"], callback_data="tu_usdt", style="primary", icon=emo))
        else:
            kb.row(_b(m["label"], callback_data=f"tu_rub:{mid}", style="success", icon=emo))
    kb.row(_b("Отмена", callback_data="balance", icon=EMOJI["back"]))
    return kb.as_markup()


def topup_networks_kb(networks: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for net in networks:
        kb.row(
            _b(
                f"USDT · {network_label(net)}",
                callback_data=f"tunet:{net.upper()}",
                style=NETWORK_STYLES.get(net.upper()),
            )
        )
    kb.row(_b("Назад", callback_data="topup", icon=EMOJI["back"]))
    return kb.as_markup()


def topup_payment_kb(
    topup_id: int, checkout_url: str | None, address: str | None = None
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if address:
        kb.row(
            _b(
                "Скопировать адрес",
                icon=EMOJI["copy"],
                copy_text=CopyTextButton(text=address),
            )
        )
    if checkout_url:
        kb.row(_b("Страница оплаты", url=checkout_url, icon=EMOJI["paylink"]))
    kb.row(
        _b("Проверить оплату", callback_data=f"tucheck:{topup_id}", style="success", icon=EMOJI["check"])
    )
    kb.row(_b("В профиль", callback_data="profile", icon=EMOJI["back"]))
    return kb.as_markup()


def back_profile_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_b("В профиль", callback_data="profile", icon=EMOJI["back"]))
    return kb.as_markup()


def topup_cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_b("Отмена", callback_data="balance", icon=EMOJI["back"]))
    return kb.as_markup()
