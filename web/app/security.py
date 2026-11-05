"""Пароли (bcrypt) и JWT-токены для аккаунтов сайта."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from jose import JWTError, jwt

from .config import settings

_ALGO = "HS256"
TOKEN_TTL = timedelta(days=30)


def hash_password(password: str) -> str:
    # bcrypt учитывает только первые 72 байта пароля — обрезаем сами.
    pw = (password or "").encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        pw = (password or "").encode("utf-8")[:72]
        return bcrypt.checkpw(pw, (password_hash or "").encode("utf-8"))
    except (ValueError, TypeError):  # битый хеш не должен ронять вход
        return False


def create_token(account_id: int) -> str:
    now = datetime.utcnow()
    payload = {"sub": str(int(account_id)), "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGO)


def decode_token(token: str) -> Optional[int]:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
        return int(data["sub"])
    except (JWTError, KeyError, ValueError, TypeError):
        return None


# ── Админ-панель (отдельный вход) ────────────────────────────────────────────
ADMIN_TOKEN_TTL = timedelta(days=7)


def verify_admin_credentials(username: str, password: str) -> bool:
    """Сверка логина/пароля админа с настройками сервера (constant-time)."""
    import hmac
    u, p = settings.admin_user, settings.admin_password
    if not u or not p:
        return False
    ok_u = hmac.compare_digest((username or ""), u)
    ok_p = hmac.compare_digest((password or ""), p)
    return ok_u and ok_p


def create_admin_token() -> str:
    now = datetime.utcnow()
    payload = {"adm": True, "iat": now, "exp": now + ADMIN_TOKEN_TTL}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGO)


def is_admin_token(token: str) -> bool:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGO])
        return bool(data.get("adm")) is True
    except (JWTError, KeyError, ValueError, TypeError):
        return False
