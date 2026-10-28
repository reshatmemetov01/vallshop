"""Настройки сайта из окружения (отдельно от бота, но БД общая)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


def _get(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v is not None and v != "" else default


@dataclass
class Settings:
    # Та же БД, что у бота (общий баланс/заказы). На старте — общий SQLite-том,
    # рекомендованно — Postgres: postgresql+asyncpg://user:pass@db:5432/vallshop
    database_url: str = _get("DATABASE_URL", "sqlite+aiosqlite:///vallshop.db")
    # Головной магазин (сайт — только головной, merchant_id = 0).
    merchant_id: int = 0
    default_markup_percent: Decimal = Decimal(_get("DEFAULT_MARKUP_PERCENT", "0"))
    currency: str = _get("SHOP_CURRENCY", "USDT")
    base_url: str = _get("WEB_BASE_URL", "https://shop.vallshop.com")
    # Phase 1 (заполним позже): JWT, SMTP, токен бота для Telegram Login Widget.
    jwt_secret: str = _get("WEB_JWT_SECRET", "change-me-in-prod")
    telegram_bot_token: str = _get("BOT_TOKEN", "")
    # Имя бота (без @) для Telegram Login Widget. Домен сайта должен быть
    # привязан к этому боту через @BotFather → /setdomain.
    telegram_bot_username: str = _get("TELEGRAM_BOT_USERNAME", "")
    # SMTP для писем (подтверждение email, сброс пароля). Пусто → письма не шлём.
    smtp_host: str = _get("SMTP_HOST", "")
    smtp_port: int = int(_get("SMTP_PORT", "465"))
    smtp_user: str = _get("SMTP_USER", "")
    smtp_password: str = _get("SMTP_PASSWORD", "")
    smtp_from: str = _get("SMTP_FROM", "") or _get("SMTP_USER", "")
    # Отображаемое имя отправителя (как видит получатель): «VallShop».
    smtp_from_name: str = _get("SMTP_FROM_NAME", "VallShop")
    smtp_ssl: bool = _get("SMTP_SSL", "1").strip().lower() not in ("0", "false", "no")

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)


settings = Settings()
