"""Хендлеры бота. admin подключается первым (у его сообщений свой фильтр)."""

from aiogram import Router
from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from ..db.models import HEAD_MERCHANT_ID

from . import (
    activation,
    admin,
    admin_appearance,
    admin_codes,
    admin_merchants,
    admin_orders,
    admin_referral,
    admin_reviews,
    admin_stats,
    admin_ui,
    bonus_user,
    franchise,
    menu_button,
    merchant_admin,
    profile,
    referral,
    reviews,
    user,
)


class _HeadOnly(BaseFilter):
    """Пропускает апдейт только в головном боте (merchant_id == 0).

    merchant_id проставляется внешним MerchantMiddleware ДО фильтров роутеров,
    поэтому админ-панель платформы недоступна в ботах франшиз — даже прямым
    колбэком (не только скрытием кнопки)."""

    async def __call__(self, event: TelegramObject, merchant_id: int = HEAD_MERCHANT_ID) -> bool:
        return merchant_id == HEAD_MERCHANT_ID


def build_root_router() -> Router:
    root = Router()
    # Постоянная кнопка «🏠 Меню» — первой, чтобы работала поверх любого FSM.
    root.include_router(menu_button.router)

    # Функции платформы-владельца — только в головном боте. Оборачиваем admin-
    # роутеры и создание франшизы в группу с фильтром merchant_id == 0: в ботах
    # франшиз их хендлеры не срабатывают вовсе (даже прямым колбэком).
    head_group = Router(name="head_only")
    head_group.message.filter(_HeadOnly())
    head_group.callback_query.filter(_HeadOnly())
    for _r in (
        admin.router, admin_ui.router, admin_codes.router, admin_orders.router,
        admin_reviews.router, admin_appearance.router, admin_referral.router,
        admin_merchants.router, admin_stats.router, franchise.router,
        activation.router,  # «Активировать Код» — только у головного
    ):
        head_group.include_router(_r)
    root.include_router(head_group)

    root.include_router(referral.router)
    root.include_router(merchant_admin.router)
    root.include_router(reviews.router)
    root.include_router(bonus_user.router)  # промокод + клик по бонус-дропу (все боты)
    root.include_router(profile.router)
    root.include_router(user.router)
    return root
