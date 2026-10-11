"""Хендлеры бота. admin подключается первым (у его сообщений свой фильтр)."""

from aiogram import Router

from . import (
    activation,
    admin,
    admin_appearance,
    admin_codes,
    admin_orders,
    admin_referral,
    admin_reviews,
    admin_stats,
    admin_ui,
    menu_button,
    profile,
    referral,
    reviews,
    user,
)


def build_root_router() -> Router:
    root = Router()
    # Постоянная кнопка «🏠 Меню» — первой, чтобы работала поверх любого FSM.
    root.include_router(menu_button.router)
    root.include_router(admin.router)
    root.include_router(admin_ui.router)
    root.include_router(admin_codes.router)
    root.include_router(admin_orders.router)
    root.include_router(admin_reviews.router)
    root.include_router(admin_appearance.router)
    root.include_router(admin_referral.router)
    root.include_router(admin_stats.router)
    root.include_router(activation.router)
    root.include_router(referral.router)
    root.include_router(reviews.router)
    root.include_router(profile.router)
    root.include_router(user.router)
    return root
