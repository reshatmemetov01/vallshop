"""Пер-мерчантные настройки оформления (таблица merchant_settings).

Ключ-значение в разрезе магазина (``merchant_id``): тексты экранов, баннеры
(file_id), контакты и ссылки. Глобальные операционные настройки (курс, наценки
поставщиков, автовозврат, способы оплаты) живут в обычном ``Setting``.
"""

from __future__ import annotations

from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import HEAD_MERCHANT_ID, MerchantSetting

# ── Ключи оформления ──────────────────────────────────────────────────────────
CAP_MAIN = "cap_main"        # текст главного экрана (приветствие)
CAP_FAQ = "cap_faq"          # текст FAQ / правил
SHOP_TITLE = "shop_title"    # название магазина (для заголовков)
SUPPORT = "support"          # контакт поддержки (url или @username)
URL_OFFER = "url_offer"
URL_AGREEMENT = "url_agreement"
URL_PRIVACY = "url_privacy"


def banner_key(name: str) -> str:
    """Ключ персонального баннера экрана (main/catalog/profile/reviews/faq)."""
    return f"banner_{name}"


async def get(
    session: AsyncSession, merchant_id: int, key: str, default: Optional[str] = None
) -> Optional[str]:
    row = await session.get(MerchantSetting, {"merchant_id": merchant_id, "key": key})
    return row.value if row is not None and row.value is not None else default


async def set(
    session: AsyncSession, merchant_id: int, key: str, value: Optional[str]
) -> None:
    row = await session.get(MerchantSetting, {"merchant_id": merchant_id, "key": key})
    if row is None:
        session.add(MerchantSetting(merchant_id=merchant_id, key=key, value=value))
    else:
        row.value = value
    await session.flush()


async def all_for(session: AsyncSession, merchant_id: int) -> Dict[str, str]:
    rows = await session.scalars(
        select(MerchantSetting).where(MerchantSetting.merchant_id == merchant_id)
    )
    return {r.key: r.value for r in rows if r.value is not None}


async def all_grouped(session: AsyncSession) -> Dict[int, Dict[str, str]]:
    """Все настройки, сгруппированные по merchant_id (для прогрева кэшей)."""
    rows = await session.scalars(select(MerchantSetting))
    out: Dict[int, Dict[str, str]] = {}
    for r in rows:
        if r.value is not None:
            out.setdefault(r.merchant_id, {})[r.key] = r.value
    return out
