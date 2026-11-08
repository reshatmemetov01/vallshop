"""Собственные таблицы сайта (не трогают схему бота).

Регистрируются в общей Base.metadata, создаются через create_all в lifespan.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base


class SiteProductDesc(Base):
    """Описание товара/раздела для показа на сайте (site-only).

    Хранится отдельно от бота: редактируется в админке сайта, схему/данные
    бота не затрагивает. Ключ — product_id из общего каталога.
    """

    __tablename__ = "site_product_desc"

    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=func.now(), onupdate=func.now()
    )
