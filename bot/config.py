"""Конфигурация бота VallShop из переменных окружения.

Все секреты читаются только из окружения / .env (см. .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Обязательная переменная окружения не задана: {name}")
    return value


def _int_list(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    out: List[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


@dataclass
class BotConfig:
    """Настройки Telegram-бота и магазина."""

    bot_token: str
    admin_ids: List[int] = field(default_factory=list)

    # База данных: SQLAlchemy DSN. По умолчанию локальный SQLite-файл.
    database_url: str = "sqlite+aiosqlite:///vallshop.db"

    # Валюта отображения цен (внутренне всё в USD ≈ USDT).
    currency: str = "USDT"

    # Наценка по умолчанию (в процентах) поверх закупочной цены LioGames,
    # если у конкретной вариации не задана своя цена/наценка.
    default_markup_percent: Decimal = Decimal("0")

    # Платёжный провайдер.
    bolt_base_url: str = "https://api.boltutil.com"
    bolt_api_key: Optional[str] = None
    # Webhook Secret из кабинета BoltUtil — ключ HMAC для запросов и колбэков.
    bolt_secret: Optional[str] = None
    # Сеть по умолчанию (используется, если список сетей не задан).
    bolt_network: str = "TRC20"
    # Сети USDT, доступные покупателю на выбор. Если пусто — [bolt_network].
    bolt_networks: List[str] = field(default_factory=list)
    # Публичный URL сервиса — для callback (notifyUrl) и редиректа после оплаты.
    public_base_url: Optional[str] = None
    payment_timeout_minutes: int = 30

    # ── PayHot (приём ₽: карта / СБП / SberPay) ───────────────────────────────
    payhot_base_url: str = "https://app.pay.hot/api/v2"
    payhot_api_key: Optional[str] = None
    payhot_secret: Optional[str] = None  # секрет подписи вебхука PayHot
    payhot_methods: List[str] = field(default_factory=list)  # card,sbp,sberpay
    payhot_mock: bool = False

    # ── Paydex (приём ₽ по СБП) ────────────────────────────────────────────────
    paydex_base_url: str = "https://paydex.pro/api/v1"
    paydex_api_key: Optional[str] = None  # secret_key (sk_live_/sk_test_)
    paydex_secret: Optional[str] = None   # webhook_secret (подпись вебхука)
    paydex_mock: bool = False

    # Контакт поддержки (Telegram @username, ссылка или email) — для оферты/FAQ.
    support_contact: Optional[str] = None
    # Юр. данные для оферты (ИП/самозанятый/компания и т.п.) — опционально.
    legal_entity: Optional[str] = None

    # Mock-режим платежей/поставщика (без сети) — для локальной разработки.
    mock_payments: bool = False

    @property
    def notify_url(self) -> Optional[str]:
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/") + "/bolt/webhook"

    def _pub(self, path: str) -> Optional[str]:
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/") + path

    @property
    def offer_url(self) -> Optional[str]:
        """Публичная ссылка на оферту (если задан публичный домен)."""
        return self._pub("/offer")

    @property
    def agreement_url(self) -> Optional[str]:
        return self._pub("/agreement")

    @property
    def privacy_url(self) -> Optional[str]:
        return self._pub("/privacy")

    @property
    def networks(self) -> List[str]:
        """Список доступных сетей (нормализованный, с дефолтом)."""
        return self.bolt_networks or [self.bolt_network]

    @property
    def payhot_notify_url(self) -> Optional[str]:
        return self._pub("/payhot/webhook")

    @property
    def payhot_enabled(self) -> bool:
        return self.payhot_mock or bool(self.payhot_api_key and self.payhot_secret)

    @property
    def payhot_method_list(self) -> List[str]:
        """Способы PayHot для показа клиенту (с дефолтом card/sbp/sberpay)."""
        if not self.payhot_enabled:
            return []
        return self.payhot_methods or ["card", "sbp"]

    @property
    def paydex_notify_url(self) -> Optional[str]:
        return self._pub("/paydex/webhook")

    @property
    def paydex_enabled(self) -> bool:
        return self.paydex_mock or bool(self.paydex_api_key and self.paydex_secret)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @classmethod
    def from_env(cls) -> "BotConfig":
        def _truthy(v: Optional[str]) -> bool:
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            bot_token=_require("BOT_TOKEN"),
            admin_ids=_int_list(_get("ADMIN_IDS")),
            database_url=_get("DATABASE_URL", "sqlite+aiosqlite:///vallshop.db"),
            currency=_get("SHOP_CURRENCY", "USDT"),
            default_markup_percent=Decimal(_get("DEFAULT_MARKUP_PERCENT", "0")),
            bolt_base_url=_get("BOLT_BASE_URL", "https://api.boltutil.com"),
            bolt_api_key=_get("BOLT_API_KEY"),
            bolt_secret=_get("BOLT_SECRET"),
            bolt_network=_get("BOLT_NETWORK", "TRC20"),
            bolt_networks=[
                n.strip().upper()
                for n in (_get("BOLT_NETWORKS", "") or "").replace(";", ",").split(",")
                if n.strip()
            ],
            public_base_url=_get("PUBLIC_BASE_URL"),
            payment_timeout_minutes=int(_get("PAYMENT_TIMEOUT_MINUTES", "30")),
            payhot_base_url=_get("PAYHOT_BASE_URL", "https://app.pay.hot/api/v2"),
            payhot_api_key=_get("PAYHOT_API_KEY"),
            payhot_secret=_get("PAYHOT_SECRET"),
            payhot_methods=[
                m.strip().lower()
                for m in (_get("PAYHOT_METHODS", "") or "").replace(";", ",").split(",")
                if m.strip()
            ],
            payhot_mock=_truthy(_get("PAYHOT_MOCK")),
            paydex_base_url=_get("PAYDEX_BASE_URL", "https://paydex.pro/api/v1"),
            paydex_api_key=_get("PAYDEX_API_KEY"),
            paydex_secret=_get("PAYDEX_SECRET"),
            paydex_mock=_truthy(_get("PAYDEX_MOCK")),
            mock_payments=_truthy(_get("MOCK_PAYMENTS")),
            support_contact=_get("SUPPORT_CONTACT"),
            legal_entity=_get("LEGAL_ENTITY"),
        )
