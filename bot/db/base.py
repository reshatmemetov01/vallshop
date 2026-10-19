"""Инфраструктура БД: async-движок, фабрика сессий, Base."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""


class Database:
    """Обёртка над async-движком и фабрикой сессий."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.engine = create_async_engine(url, echo=echo, future=True)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def create_all(self) -> None:
        """Создать таблицы и до-мигрировать недостающие колонки (без Alembic).

        create_all создаёт новые таблицы, но НЕ добавляет колонки в уже
        существующие. Для простых миграций (ADD COLUMN) делаем это вручную —
        безопасно для SQLite и Postgres.
        """
        from . import models  # noqa: F401  (регистрация моделей в метаданных)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(self._add_missing_columns)

    @staticmethod
    def _add_missing_columns(conn) -> None:
        from sqlalchemy import inspect, text

        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())

        # (таблица, колонка, DDL-тип с дефолтом)
        wanted = [
            ("users", "balance", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("orders", "quantity", "INTEGER NOT NULL DEFAULT 1"),
            ("products", "icon_emoji_id", "VARCHAR(32)"),
            ("variants", "icon_emoji_id", "VARCHAR(32)"),
            ("variants", "source", "VARCHAR(16) NOT NULL DEFAULT 'stock'"),
            ("variants", "fzr_kind", "VARCHAR(16)"),
            ("variants", "fzr_a", "VARCHAR(64)"),
            ("variants", "fzr_b", "VARCHAR(64)"),
            ("orders", "supplier", "VARCHAR(16)"),
            ("orders", "supplier_order_id", "VARCHAR(64)"),
            ("products", "parent_id", "INTEGER"),
            ("variants", "price_rub", "NUMERIC(12,2)"),
            ("variants", "price_mode", "VARCHAR(12)"),
            ("users", "block_reason", "VARCHAR(255)"),
            ("products", "pending_text", "TEXT"),
            ("products", "delivered_text", "TEXT"),
            ("variants", "pending_text", "TEXT"),
            ("variants", "delivered_text", "TEXT"),
            ("products", "banner_file_id", "VARCHAR(255)"),
            ("orders", "fields_json", "TEXT"),
            ("orders", "fail_reason", "VARCHAR(255)"),
            ("orders", "cost_usd", "NUMERIC(12,4)"),
            ("users", "referred_by", "INTEGER"),
            ("users", "ref_balance_usdt", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("users", "ref_balance_rub", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("users", "ref_earned_usdt", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("users", "ref_earned_rub", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("products", "button_style", "VARCHAR(16)"),
            ("products", "full_width", "BOOLEAN NOT NULL DEFAULT 0"),
            # Франшиза (Фаза 1): принадлежность записи магазину (0 = головной).
            ("users", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("topups", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("reviews", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("referral_earnings", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("activation_requests", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("vpn_subscriptions", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            # Франшиза (P1): наценка по умолчанию и баланс заработка мерчанта.
            ("merchants", "default_markup", "NUMERIC(6,2) NOT NULL DEFAULT 0"),
            ("merchants", "earnings_rub", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            ("merchants", "earnings_total_rub", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            # Франшиза (P1.2): маржа мерчанта по заказу (₽).
            ("orders", "merchant_margin", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
            # Франшиза (Оформление O1): пер-мерчантные правки кнопок меню.
            ("menu_buttons", "merchant_id", "INTEGER NOT NULL DEFAULT 0"),
            ("menu_buttons", "style", "VARCHAR(16)"),
            # Франшиза: товар только для головного (скрыт у франшиз).
            ("products", "head_only", "BOOLEAN NOT NULL DEFAULT 0"),
            # Рассылка: достижимость клиента (активен/неактивен). См. User.is_active.
            ("users", "is_active", "BOOLEAN NOT NULL DEFAULT 1"),
            # Канал: показан ли разовый мягкий промо-линк. См. User.channel_notified.
            ("users", "channel_notified", "BOOLEAN NOT NULL DEFAULT 0"),
            # SMM-поставщик soc-proof (source="socproof"): услуга, лимиты, закуп ₽/1000.
            ("variants", "smm_service", "INTEGER"),
            ("variants", "smm_min", "INTEGER"),
            ("variants", "smm_max", "INTEGER"),
            ("variants", "smm_rate", "NUMERIC(12,4)"),
        ]
        added_source = False
        for table, column, ddl in wanted:
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                if (table, column) == ("variants", "source"):
                    added_source = True

        # Бэкофилл источника для уже существующих номиналов LioGames:
        # всё, что было привязано к реальному product_id (>0), — это liogames.
        if added_source and "variants" in existing_tables:
            conn.execute(
                text(
                    "UPDATE variants SET source='liogames' "
                    "WHERE liog_product_id IS NOT NULL AND liog_product_id > 0"
                )
            )

        # Фаза 2: составной ключ users (id, merchant_id). Один Telegram-юзер
        # может быть клиентом у разных мерчантов со своим балансом у каждого.
        # Перестройка старой таблицы (одностолбцовый ключ id) — атомарно, в той
        # же транзакции create_all; защита: не трогаем, если ключ уже составной.
        if "users" in set(inspect(conn).get_table_names()):
            ucols = inspect(conn).get_columns("users")
            pk_cols = {c["name"] for c in ucols if c.get("primary_key")}
            if pk_cols == {"id"}:  # старый ключ → перестроить
                new_cols = [
                    ("id", "INTEGER NOT NULL"),
                    ("merchant_id", "INTEGER NOT NULL DEFAULT 0"),
                    ("username", "VARCHAR(64)"),
                    ("full_name", "VARCHAR(255)"),
                    ("is_blocked", "BOOLEAN NOT NULL DEFAULT 0"),
                    ("block_reason", "VARCHAR(255)"),
                    ("is_active", "BOOLEAN NOT NULL DEFAULT 1"),
                    ("channel_notified", "BOOLEAN NOT NULL DEFAULT 0"),
                    ("balance", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
                    ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
                    ("referred_by", "INTEGER"),
                    ("ref_balance_usdt", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
                    ("ref_balance_rub", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
                    ("ref_earned_usdt", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
                    ("ref_earned_rub", "NUMERIC(12,2) NOT NULL DEFAULT 0"),
                ]
                existing = {c["name"] for c in ucols}
                copy = ", ".join(n for n, _ in new_cols if n in existing)
                ddl = ", ".join(f"{n} {t}" for n, t in new_cols)
                conn.execute(text(f"CREATE TABLE users_new ({ddl}, PRIMARY KEY (id, merchant_id))"))
                conn.execute(text(f"INSERT INTO users_new ({copy}) SELECT {copy} FROM users"))
                conn.execute(text("DROP TABLE users"))
                conn.execute(text("ALTER TABLE users_new RENAME TO users"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_referred_by ON users (referred_by)"))

        # Оформление O1: составной ключ menu_buttons (key, merchant_id) — у каждого
        # магазина свой слой правок кнопок. Перестройка старой таблицы (ключ key)
        # в той же транзакции; защита: не трогаем, если ключ уже составной.
        if "menu_buttons" in set(inspect(conn).get_table_names()):
            mcols = inspect(conn).get_columns("menu_buttons")
            pk = {c["name"] for c in mcols if c.get("primary_key")}
            if pk == {"key"}:  # старый одностолбцовый ключ → перестроить
                new_cols = [
                    ("key", "VARCHAR(64) NOT NULL"),
                    ("merchant_id", "INTEGER NOT NULL DEFAULT 0"),
                    ("label", "VARCHAR(255)"),
                    ("emoji_id", "VARCHAR(32)"),
                    ("enabled", "BOOLEAN NOT NULL DEFAULT 1"),
                    ("sort_order", "INTEGER"),
                    ("style", "VARCHAR(16)"),
                ]
                existing = {c["name"] for c in mcols}
                copy = ", ".join(n for n, _ in new_cols if n in existing)
                ddl = ", ".join(f"{n} {t}" for n, t in new_cols)
                conn.execute(text(f"CREATE TABLE menu_buttons_new ({ddl}, PRIMARY KEY (key, merchant_id))"))
                conn.execute(text(f"INSERT INTO menu_buttons_new ({copy}) SELECT {copy} FROM menu_buttons"))
                conn.execute(text("DROP TABLE menu_buttons"))
                conn.execute(text("ALTER TABLE menu_buttons_new RENAME TO menu_buttons"))

        # Оформление O1: перенести тексты экранов головного (cap_main/cap_faq) из
        # глобального settings в merchant_settings(0) — дальше оформление читается
        # оттуда единообразно для всех магазинов. Идемпотентно (OR IGNORE).
        _tables = set(inspect(conn).get_table_names())
        if "merchant_settings" in _tables and "settings" in _tables:
            conn.execute(text(
                "INSERT OR IGNORE INTO merchant_settings (merchant_id, key, value) "
                "SELECT 0, key, value FROM settings "
                "WHERE key IN ('cap_main', 'cap_faq') AND value IS NOT NULL"
            ))

        # Индексы по merchant_id (идемпотентно) — для будущих пер-мерчант выборок.
        index_targets = [
            "users", "orders", "topups", "reviews",
            "referral_earnings", "activation_requests", "vpn_subscriptions",
        ]
        existing_tables = set(inspect(conn).get_table_names())
        for table in index_targets:
            if table in existing_tables:
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_{table}_merchant_id "
                        f"ON {table} (merchant_id)"
                    )
                )

        # Сид головного магазина (merchant_id = 0): запись-владелец платформы.
        if "merchants" in existing_tables:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO merchants (id, title, status) "
                    "VALUES (0, 'VallShop (головной)', 'active')"
                )
            )

    async def dispose(self) -> None:
        await self.engine.dispose()

    def session(self) -> AsyncSession:
        return self.session_factory()
