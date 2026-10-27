"""Проверка данных Telegram Login Widget (подпись HMAC по токену бота).

Док: https://core.telegram.org/widgets/login#checking-authorization
secret_key = SHA256(bot_token); проверяем HMAC-SHA256 от data-check-string.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping


def verify_login_widget(data: Mapping, bot_token: str, max_age: int = 86400) -> bool:
    if not bot_token or not isinstance(data, Mapping):
        return False
    received = data.get("hash")
    if not received:
        return False
    # data-check-string: все поля кроме hash, "key=value", отсортированные, через \n.
    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    check_string = "\n".join(pairs)
    secret = hashlib.sha256(bot_token.encode()).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, str(received)):
        return False
    # Защита от повторного использования старых данных.
    if max_age:
        try:
            if time.time() - int(data.get("auth_date", 0)) > max_age:
                return False
        except (ValueError, TypeError):
            return False
    return True
