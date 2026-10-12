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
