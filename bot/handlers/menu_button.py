"""Постоянная нижняя кнопка «🏠 Меню» — быстрый возврат в главное меню.

Роутер подключается ПЕРВЫМ, поэтому нажатие «🏠 Меню» перехватывается в
любом состоянии (в том числе во время ввода суммы/логина в FSM-сценариях):
сбрасывает состояние и открывает главное меню.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..config import BotConfig
from ..services import menu as menu_service
from ..ui import render
from .. import keyboards as kb
from .. import texts

router = Router(name="menu_button")


@router.message(F.text == kb.MENU_BUTTON_TEXT)
async def msg_menu_button(
    message: Message, config: BotConfig, state: FSMContext
) -> None:
    await state.clear()
    is_admin = config.is_admin(message.from_user.id)
    await render(
        message,
        banner="main",
        caption=menu_service.caption("main", texts.START),
        reply_markup=kb.main_menu_kb(is_admin),
    )
