"""Точка входа Telegram-бота VallShop."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from liogames import LioGamesClient
from fazercard import FazerCardClient

from .config import BotConfig
from .db import Database
from .handlers import build_root_router
from .payments import build_paydex, build_payhot, build_provider
from .services.poller import (
    run_fulfillment_poller,
    run_smartprice_poller,
    run_topup_poller,
)

log = logging.getLogger("vallshop")


async def _run_web(config: BotConfig, bot, db, provider, liog, payhot=None, paydex=None) -> None:
    """Запустить aiohttp-сервер вебхуков, если задан PUBLIC_BASE_URL."""
    from aiohttp import web

    from .web import build_app

    app = build_app(
        bot=bot, db=db, provider=provider, liog=liog, config=config,
        payhot=payhot, paydex=paydex,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Webhook-сервер слушает :%s (notifyUrl=%s)", port, config.notify_url)


async def _migrate_to_rub(db: Database, rate) -> None:
    """Разовая конвертация балансов USDT→₽ при переходе на рубли.

    Умножает балансы и реф-начисления на курс. Реф-баланс USDT переносится
    в реф-баланс ₽. Защищено флагом в settings — выполняется один раз.
    """
    from decimal import Decimal
    from sqlalchemy import update
    from .db.models import Order, User
    from .services import settings as settings_service

    async with db.session() as session:
        done = await settings_service.get(session, "rub_migrated")
        if done:
            return
        r = Decimal(rate)
        # Балансы пользователей и реф-балансы → рубли.
        await session.execute(update(User).values(
            balance=User.balance * r,
            ref_balance_rub=User.ref_balance_rub + User.ref_balance_usdt * r,
            ref_earned_rub=User.ref_earned_rub + User.ref_earned_usdt * r,
            ref_balance_usdt=Decimal(0),
            ref_earned_usdt=Decimal(0),
        ))
        # Суммы заказов (списанное) → рубли, чтобы история/статистика были в ₽.
        await session.execute(update(Order).values(price_usd=Order.price_usd * r))
        await settings_service.set(session, "rub_migrated", "1")
        await session.commit()
    log.info("Балансы сконвертированы в рубли по курсу %s", rate)


async def _seed_vpn_category(db: Database) -> None:
    """Разово создать категорию «VPN | VallShop» (красная, на всю строку)."""
    from .db.models import Product
    from .services import menu as _m  # noqa: F401 (не нужен, но держим стиль)
    from .services import settings as settings_service

    async with db.session() as session:
        if await settings_service.get(session, "vpn_category_seeded"):
            return
        p = Product(
            game="VPN",
            title="VPN | VallShop",
            parent_id=None,
            is_active=True,
            icon_emoji_id="6300855020531225173",
            button_style="danger",
            full_width=True,
            sort_order=50,  # внизу каталога
        )
        session.add(p)
        await settings_service.set(session, "vpn_category_seeded", "1")
        await session.commit()
    log.info("Категория «VPN | VallShop» создана")


async def _seed_steam_category(db: Database) -> None:
    """Разово создать кнопку «Пополнить Steam» (товар + номинал-сервис).

    Товар с game="STEAM_TOPUP" — спец-кнопка: тап ведёт в свой флоу
    (логин → валюта → сумма). Номинал нужен для привязки заказа и выдачи
    (source="fazercard", fzr_kind="steam").
    """
    from .db.models import Product, Variant
    from .services import settings as settings_service

    async with db.session() as session:
        if await settings_service.get(session, "steam_category_seeded"):
            return
        p = Product(
            game="STEAM_TOPUP",
            title="Пополнить Steam",
            parent_id=None,
            is_active=True,
            icon_emoji_id="5372817526801589587",
            button_style="primary",
            full_width=True,
            sort_order=40,
        )
        session.add(p)
        await session.flush()
        session.add(Variant(
            product_id=p.id,
            title="Пополнение Steam",
            liog_product_id=0,
            liog_variation_id=-1,
            cost_usd=0,
            source="fazercard",
            fzr_kind="steam",
            fzr_a="RUB",  # валюта по умолчанию (клиент выбирает при покупке)
            is_active=True,
        ))
        await settings_service.set(session, "steam_category_seeded", "1")
        await session.commit()
    log.info("Кнопка «Пополнить Steam» создана")


async def _seed_stars_category(db: Database) -> None:
    """Разово создать кнопку «Телеграм Звёзды» (товар + номинал-сервис).

    Товар с game="TG_STARS" — спец-кнопка: тап ведёт в свой флоу
    (@username → количество). Номинал нужен для привязки заказа и выдачи
    (source="fazercard", fzr_kind="stars", fzr_a="telegram_stars").
    """
    from .db.models import Product, Variant
    from .services import settings as settings_service

    async with db.session() as session:
        if await settings_service.get(session, "stars_category_seeded"):
            return
        p = Product(
            game="TG_STARS",
            title="Телеграм Звёзды",
            parent_id=None,
            is_active=True,
            icon_emoji_id="5886685105065300941",
            button_style="primary",
            full_width=False,  # в паре с «Телеграм Премиум»
            sort_order=10,
        )
        session.add(p)
        await session.flush()
        session.add(Variant(
            product_id=p.id,
            title="Telegram Звёзды",
            liog_product_id=0,
            liog_variation_id=-1,
            cost_usd=0,
            source="fazercard",
            fzr_kind="stars",
            fzr_a="telegram_stars",  # маршрутизация выдачи на эндпоинт звёзд
            is_active=True,
        ))
        await settings_service.set(session, "stars_category_seeded", "1")
        await session.commit()
    log.info("Кнопка «Телеграм Звёзды» создана")


async def _reorder_catalog_v1(db: Database) -> None:
    """Разово выстроить кнопки каталога в нужном порядке/раскладке.

    Целевая раскладка верхнего уровня:
      [Телеграм Звёзды] [Телеграм Премиум]   (в паре)
      [Игры и Сервисы]  (на всю строку)
      [Пополнить Steam] (на всю строку)
      [VPN | VallShop]  (на всю строку)

    Админка не умеет менять порядок (sort_order), поэтому правим прямо в БД.
    Системные кнопки ищем по game, пользовательские — по названию.
    """
    from sqlalchemy import select
    from .db.models import Product
    from .services import settings as settings_service

    async with db.session() as session:
        if await settings_service.get(session, "catalog_reorder_v1"):
            return
        products = list(
            await session.scalars(select(Product).where(Product.parent_id.is_(None)))
        )
        for p in products:
            title = (p.title or "").lower()
            if p.game == "TG_STARS":
                p.sort_order, p.full_width = 10, False
            elif "преми" in title or "premium" in title:
                p.sort_order, p.full_width = 20, False
            elif "игры и сервис" in title:
                p.sort_order, p.full_width = 30, True
            elif p.game == "STEAM_TOPUP":
                p.sort_order, p.full_width = 40, True
            elif p.game == "VPN":
                p.sort_order, p.full_width = 50, True
        await settings_service.set(session, "catalog_reorder_v1", "1")
        await session.commit()
    log.info("Каталог: кнопки переупорядочены (v1)")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = BotConfig.from_env()

    db = Database(config.database_url)
    await db.create_all()

    from .services import menu as menu_service
    from .services import rates as rates_service
    await menu_service.load(db)  # кэш оформления меню
    await rates_service.load(db)  # курс USDT→₽
    await _migrate_to_rub(db, rates_service.get_rate())  # разовая конвертация балансов
    await _seed_vpn_category(db)  # разовое создание категории «VPN | VallShop»
    await _seed_steam_category(db)  # разовое создание кнопки «Пополнить Steam»
    await _seed_stars_category(db)  # разовое создание кнопки «Телеграм Звёзды»
    await _reorder_catalog_v1(db)   # разовая раскладка кнопок каталога

    provider = build_provider(config)
    payhot = build_payhot(config)
    paydex = build_paydex(config)
    liog = LioGamesClient.from_env()
    fzr = FazerCardClient.from_env()
    from vpnresellers import VPNResellersClient
    vpn = VPNResellersClient.from_env()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Внедрение зависимостей в хендлеры (aiogram прокидывает по имени параметра).
    dp["config"] = config
    dp["db"] = db
    dp["provider"] = provider
    dp["payhot"] = payhot
    dp["paydex"] = paydex
    dp["liog"] = liog
    dp["fzr"] = fzr
    dp["vpn"] = vpn

    from .middlewares import BlockedMiddleware
    dp.message.middleware(BlockedMiddleware())
    dp.callback_query.middleware(BlockedMiddleware())

    dp.include_router(build_root_router())

    # Команды в меню бота (кнопка «/» у поля ввода).
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Запустить / главное меню"),
            BotCommand(command="menu", description="Открыть меню"),
        ])
    except Exception:  # noqa: BLE001
        log.warning("Не удалось установить команды меню")

    tasks = [
        asyncio.create_task(run_fulfillment_poller(bot, db, liog, fzr, config)),
        asyncio.create_task(run_topup_poller(bot, db, provider, payhot=payhot, paydex=paydex)),
        asyncio.create_task(run_smartprice_poller(bot, db, fzr, config)),
    ]
    if config.public_base_url:
        await _run_web(config, bot, db, provider, liog, payhot, paydex)

    log.info(
        "Старт бота. Провайдер оплаты: %s. Админы: %s",
        provider.name,
        config.admin_ids,
    )
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()
        await db.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
