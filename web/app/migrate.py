"""Лёгкие миграции схемы сайта (только таблицы, которыми владеет сайт).

Схемой бота владеет бот — сюда не лезем. Здесь чиним только web_accounts,
т.к. колонки email/password_hash стали nullable (аккаунты через Telegram
создаются без email), а create_all не меняет уже существующие таблицы.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from bot.db.models import WebAccount

# Колонки для переноса данных при пересборке web_accounts (порядок не важен).
_COLS = (
    "id, email, password_hash, email_verified, "
    "telegram_user_id, wallet_user_id, is_blocked, created_at, last_login_at"
)


def ensure_web_accounts_nullable(conn: Connection) -> None:
    """Сделать web_accounts.email/password_hash nullable (SQLite, идемпотентно).

    Вызывается через conn.run_sync внутри транзакции. Для не-SQLite — no-op
    (при переезде на Postgres сделаем ALTER ... DROP NOT NULL отдельно).
    """
    if conn.dialect.name != "sqlite":
        return

    info = conn.exec_driver_sql("PRAGMA table_info(web_accounts)").fetchall()
    if not info:
        return  # таблицы ещё нет — create_all создаст её уже правильной

    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    notnull = {row[1]: row[3] for row in info}
    if not notnull.get("email", 0) and not notnull.get("password_hash", 0):
        return  # уже nullable — ничего делать не нужно

    # SQLite не умеет ALTER COLUMN DROP NOT NULL — пересобираем таблицу.
    conn.exec_driver_sql("ALTER TABLE web_accounts RENAME TO _web_accounts_old")
    for ix in ("email", "telegram_user_id", "wallet_user_id"):
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS ix_web_accounts_{ix}")
    WebAccount.__table__.create(conn)  # новая таблица + её индексы
    conn.exec_driver_sql(
        f"INSERT INTO web_accounts ({_COLS}) SELECT {_COLS} FROM _web_accounts_old"
    )
    conn.exec_driver_sql("DROP TABLE _web_accounts_old")
