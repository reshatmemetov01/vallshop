"""Админ-хендлеры: управление каталогом, ценами, наценкой и стоком.

Все команды доступны только пользователям из ADMIN_IDS.
Формат ввода с несколькими полями — через разделитель «|».
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import List, Optional

from aiogram import F, Router
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sqlalchemy import func, select

from ..config import BotConfig
from ..db import Database
from ..db.models import (
    InstantCandidate,
    Order,
    OrderStatus,
    Product,
    StockItem,
    StockStatus,
    Variant,
)
from ..services import catalog as catalog_service
from ..services import stock as stock_service
from ..services.pricing import margin, sale_price
from .. import texts

# Порог наценки для ручной проверки «Скан цен» / /lowmargin (ниже — риск минуса).
LOW_MARGIN_THRESHOLD = Decimal("7")

router = Router()


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, config: BotConfig) -> bool:
        return bool(message.from_user and config.is_admin(message.from_user.id))


# Применяем фильтр ко всем хендлерам этого роутера.
router.message.filter(IsAdmin())


class AddStock(StatesGroup):
    waiting_codes = State()


def _parts(text: str, cmd: str) -> List[str]:
    """Вернуть аргументы команды, разбитые по «|»."""
    raw = text[len(cmd):].strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split("|")]


def _dec(value: str) -> Optional[Decimal]:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


HELP = (
    "🛠 <b>Админ-панель VallShop</b>\n\n"
    "<b>Каталог</b>\n"
    "/products — список товаров\n"
    "/addproduct game | Название [| описание]\n"
    "/variants product_id — номиналы товара\n"
    "/addvariant product_id | Название | liog_product_id | liog_variation_id | cost_usd [| price_usd]\n\n"
    "<b>Цены и наценка</b>\n"
    "/setprice variant_id значение|-  (фикс-цена; - чтобы убрать)\n"
    "/setmarkup variant_id процент|-  (наценка %; - чтобы убрать)\n\n"
    "<b>Видимость</b>\n"
    "/toggleproduct product_id\n"
    "/togglevariant variant_id\n\n"
    "<b>Сток</b>\n"
    "/addstock variant_id — затем отправьте коды (по одному в строке)\n"
    "/stock — остатки по номиналам\n\n"
    "<b>Заказы</b>\n"
    "/orders — последние заказы\n\n"
    "<b>Прочее</b>\n"
    "/emojiid — узнать id кастом-эмодзи (нужен Telegram Premium)"
)


@router.message(Command("admin"))
@router.message(Command("help"))
async def cmd_admin(message: Message, db: Database, config: BotConfig) -> None:
    async with db.session() as session:
        products = await session.scalar(select(func.count()).select_from(Product))
        variants = await session.scalar(select(func.count()).select_from(Variant))
        in_stock = await session.scalar(
            select(func.count())
            .select_from(StockItem)
            .where(StockItem.status == StockStatus.AVAILABLE)
        )
        orders = await session.scalar(select(func.count()).select_from(Order))
    stats = (
        f"\n\n📊 Товаров: {products} · номиналов: {variants} · "
        f"кодов в стоке: {in_stock} · заказов: {orders}"
    )
    await message.answer(HELP + stats)


@router.message(Command("products"))
async def cmd_products(message: Message, db: Database) -> None:
    async with db.session() as session:
        products = await catalog_service.list_products(session, only_active=False)
    if not products:
        await message.answer("Товаров нет. Добавьте: /addproduct")
        return
    lines = ["<b>Товары:</b>"]
    for p in products:
        flag = "🟢" if p.is_active else "🔴"
        lines.append(f"{flag} <code>{p.id}</code> · [{p.game}] {p.title}")
    await message.answer("\n".join(lines))


@router.message(Command("addproduct"))
async def cmd_addproduct(message: Message, db: Database) -> None:
    args = _parts(message.text, "/addproduct")
    if len(args) < 2:
        await message.answer("Формат: /addproduct game | Название [| описание]")
        return
    game, title = args[0], args[1]
    description = args[2] if len(args) > 2 else None
    async with db.session() as session:
        product = Product(game=game, title=title, description=description)
        session.add(product)
        await session.commit()
        pid = product.id
    await message.answer(f"✅ Товар создан: <code>{pid}</code> — {title}")


@router.message(Command("variants"))
async def cmd_variants(message: Message, db: Database, config: BotConfig) -> None:
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Формат: /variants product_id")
        return
    product_id = int(args[1])
    async with db.session() as session:
        variants = await catalog_service.list_variants(
            session, product_id, only_active=False
        )
        if not variants:
            await message.answer("Номиналов нет. Добавьте: /addvariant")
            return
        counts = await stock_service.counts_by_variant(
            session, [v.id for v in variants]
        )
        lines = [f"<b>Номиналы товара {product_id}:</b>"]
        for v in variants:
            flag = "🟢" if v.is_active else "🔴"
            price = sale_price(v, config.default_markup_percent)
            m = margin(v, config.default_markup_percent)
            lines.append(
                f"{flag} <code>{v.id}</code> · {v.title} · "
                f"цена {texts.money(price, config.currency)} "
                f"(закуп {texts.money(Decimal(v.cost_usd or 0), config.currency)}, "
                f"маржа {texts.money(m, config.currency)}) · сток {counts.get(v.id, 0)}"
            )
    await message.answer("\n".join(lines))


@router.message(Command("addvariant"))
async def cmd_addvariant(message: Message, db: Database) -> None:
    args = _parts(message.text, "/addvariant")
    if len(args) < 5:
        await message.answer(
            "Формат: /addvariant product_id | Название | liog_product_id | "
            "liog_variation_id | cost_usd [| price_usd]"
        )
        return
    try:
        product_id = int(args[0])
        liog_product_id = int(args[2])
        liog_variation_id = int(args[3])
    except ValueError:
        await message.answer("product_id, liog_product_id, liog_variation_id — числа.")
        return
    cost = _dec(args[4])
    if cost is None:
        await message.answer("cost_usd — число (например 0.88).")
        return
    price = _dec(args[5]) if len(args) > 5 and args[5] not in ("", "-") else None

    async with db.session() as session:
        product = await session.get(Product, product_id)
        if not product:
            await message.answer(f"Товар {product_id} не найден.")
            return
        variant = Variant(
            product_id=product_id,
            title=args[1],
            liog_product_id=liog_product_id,
            liog_variation_id=liog_variation_id,
            cost_usd=cost,
            price_usd=price,
        )
        session.add(variant)
        await session.commit()
        vid = variant.id
    await message.answer(f"✅ Номинал создан: <code>{vid}</code> — {args[1]}")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message, db: Database, config: BotConfig) -> None:
    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit():
        await message.answer("Формат: /setprice variant_id значение|-")
        return
    variant_id = int(args[1])
    async with db.session() as session:
        variant = await session.get(Variant, variant_id)
        if not variant:
            await message.answer("Номинал не найден.")
            return
        if args[2] == "-":
            variant.price_usd = None
            msg = "фикс-цена убрана (считается по наценке)"
        else:
            val = _dec(args[2])
            if val is None:
                await message.answer("Значение — число.")
                return
            variant.price_usd = val
            msg = f"цена = {texts.money(val, config.currency)}"
        await session.commit()
    await message.answer(f"✅ Номинал {variant_id}: {msg}")


@router.message(Command("setmarkup"))
async def cmd_setmarkup(message: Message, db: Database) -> None:
    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit():
        await message.answer("Формат: /setmarkup variant_id процент|-")
        return
    variant_id = int(args[1])
    async with db.session() as session:
        variant = await session.get(Variant, variant_id)
        if not variant:
            await message.answer("Номинал не найден.")
            return
        if args[2] == "-":
            variant.markup_percent = None
            msg = "наценка сброшена (берётся дефолтная)"
        else:
            val = _dec(args[2])
            if val is None:
                await message.answer("Процент — число.")
                return
            variant.markup_percent = val
            msg = f"наценка = {val}%"
        await session.commit()
    await message.answer(f"✅ Номинал {variant_id}: {msg}")


@router.message(Command("toggleproduct"))
async def cmd_toggleproduct(message: Message, db: Database) -> None:
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Формат: /toggleproduct product_id")
        return
    async with db.session() as session:
        product = await session.get(Product, int(args[1]))
        if not product:
            await message.answer("Товар не найден.")
            return
        product.is_active = not product.is_active
        await session.commit()
        state = "включён 🟢" if product.is_active else "выключен 🔴"
    await message.answer(f"✅ Товар {args[1]}: {state}")


@router.message(Command("togglevariant"))
async def cmd_togglevariant(message: Message, db: Database) -> None:
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Формат: /togglevariant variant_id")
        return
    async with db.session() as session:
        variant = await session.get(Variant, int(args[1]))
        if not variant:
            await message.answer("Номинал не найден.")
            return
        variant.is_active = not variant.is_active
        await session.commit()
        state = "включён 🟢" if variant.is_active else "выключен 🔴"
    await message.answer(f"✅ Номинал {args[1]}: {state}")


@router.message(Command("addstock"))
async def cmd_addstock(message: Message, db: Database, state: FSMContext) -> None:
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Формат: /addstock variant_id (затем пришлите коды).")
        return
    variant_id = int(args[1])
    async with db.session() as session:
        variant = await session.get(Variant, variant_id)
        if not variant:
            await message.answer("Номинал не найден.")
            return
    await state.set_state(AddStock.waiting_codes)
    await state.update_data(variant_id=variant_id)
    await message.answer(
        f"Пришлите коды для номинала <code>{variant_id}</code> — по одному в строке.\n"
        "Отмена: /cancel"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("emojiid"))
async def cmd_emojiid(message: Message) -> None:
    """Показать custom_emoji_id кастом-эмодзи в сообщении.

    Отправьте боту (ответом или в одном сообщении после /emojiid) текст с
    премиум-эмодзи — бот вернёт их id. Слать кастом-эмодзи может только
    аккаунт с Telegram Premium.
    """
    target = message.reply_to_message or message
    entities = (target.entities or []) + (target.caption_entities or [])
    ids = [e.custom_emoji_id for e in entities if getattr(e, "custom_emoji_id", None)]
    if not ids:
        await message.answer(
            "Не нашёл кастом-эмодзи. Отправьте /emojiid и в этом же сообщении "
            "(или ответом на сообщение) поставьте премиум-эмодзи.\n"
            "Слать кастом-эмодзи может только аккаунт с Telegram Premium."
        )
        return
    lines = ["Найденные custom_emoji_id:"]
    lines += [f"<code>{i}</code>" for i in ids]
    await message.answer("\n".join(lines))


@router.message(AddStock.waiting_codes)
async def receive_codes(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    variant_id = data["variant_id"]
    codes = [line for line in (message.text or "").splitlines() if line.strip()]
    if not codes:
        await message.answer("Пусто. Пришлите коды или /cancel.")
        return
    async with db.session() as session:
        added, skipped = await stock_service.add_codes(session, variant_id, codes)
        await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Добавлено кодов: {added}" + (f", пропущено дубликатов: {skipped}" if skipped else "")
    )


@router.message(Command("stock"))
async def cmd_stock(message: Message, db: Database) -> None:
    async with db.session() as session:
        rows = await session.execute(
            select(Variant.id, Variant.title, func.count(StockItem.id))
            .select_from(Variant)
            .join(
                StockItem,
                (StockItem.variant_id == Variant.id)
                & (StockItem.status == StockStatus.AVAILABLE),
                isouter=True,
            )
            .group_by(Variant.id)
            .order_by(Variant.id)
        )
        items = rows.all()
    if not items:
        await message.answer("Номиналов нет.")
        return
    lines = ["<b>Сток (доступно):</b>"]
    for vid, title, cnt in items:
        lines.append(f"<code>{vid}</code> · {title}: <b>{cnt or 0}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("orders"))
async def cmd_orders(message: Message, db: Database, config: BotConfig) -> None:
    async with db.session() as session:
        orders = list(
            await session.scalars(
                select(Order).order_by(Order.id.desc()).limit(15)
            )
        )
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    lines = ["<b>Последние заказы:</b>"]
    for o in orders:
        lines.append(
            f"<code>{o.id}</code> · {o.status} · "
            f"{texts.money(Decimal(o.price_usd), config.currency)} · "
            f"user {o.user_id}"
        )
    await message.answer("\n".join(lines))



# ── Скидочные товары (FazerCard Instant use): включить / скрыть кандидата ─────
@router.callback_query(F.data.startswith("inst_on:"))
async def cb_instant_activate(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    if not (call.from_user and config.is_admin(call.from_user.id)):
        await call.answer("Нет доступа", show_alert=True)
        return
    cand_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        cand = await session.get(InstantCandidate, cand_id)
        if cand is None:
            await call.answer("Кандидат не найден", show_alert=True)
            return
        v = await session.get(Variant, cand.variant_id) if cand.variant_id else None
        if v is not None:
            v.is_active = True
        cand.status = "active"
        await session.commit()
    try:
        await call.message.edit_text(
            (call.message.text or "") + "\n\n✅ Активирован. Появится в разделе, "
            "когда вы включите «🔥 Скидочные товары» в админке."
        )
    except Exception:  # noqa: BLE001
        pass
    await call.answer("Активирован ✅")


@router.callback_query(F.data.startswith("inst_off:"))
async def cb_instant_dismiss(call: CallbackQuery, db: Database, config: BotConfig) -> None:
    if not (call.from_user and config.is_admin(call.from_user.id)):
        await call.answer("Нет доступа", show_alert=True)
        return
    cand_id = int(call.data.split(":", 1)[1])
    async with db.session() as session:
        cand = await session.get(InstantCandidate, cand_id)
        if cand is None:
            await call.answer("Кандидат не найден", show_alert=True)
            return
        v = await session.get(Variant, cand.variant_id) if cand.variant_id else None
        if v is not None and not v.is_active:
            await session.delete(v)
        cand.status = "dismissed"
        cand.variant_id = None
        await session.commit()
    try:
        await call.message.edit_text(
            (call.message.text or "") + "\n\n🚫 Скрыто. Больше не предложу этот товар."
        )
    except Exception:  # noqa: BLE001
        pass
    await call.answer("Скрыто")


# ── Ручная проверка низкой наценки у плавающих товаров (SMM + FazerCard) ──────
@router.message(Command("lowmargin"))
async def cmd_low_margin(message: Message, db: Database, config: BotConfig) -> None:
    """Список товаров с плавающей ценой (SMM + FazerCard-smart) и наценкой ниже
    порога (по умолчанию 7%). Запускается вручную.

    Можно задать свой порог: /lowmargin 5
    """
    threshold = LOW_MARGIN_THRESHOLD
    raw = (message.text or "").replace("/lowmargin", "", 1).strip()
    if raw:
        try:
            threshold = Decimal(raw.replace(",", ".").split()[0])
        except (InvalidOperation, IndexError):
            threshold = LOW_MARGIN_THRESHOLD

    from ..services import smartprice as smartprice_service
    rows = await smartprice_service.low_margin_variants(
        db, config.default_markup_percent, threshold
    )
    for msg in smartprice_service.format_low_margin(rows, threshold):
        await message.answer(msg)
