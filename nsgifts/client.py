"""Автономный клиент NS.Gifts Wholesale API v2.

Док: https://wholesale.ns.gifts/api-docs
Базовый URL: ``https://api.ns.gifts``, все пути под ``/api/v2``.

Авторизация двухслойная:
  * ``api_secret`` — выдаётся один раз оператором вместе с ``user_id`` и
    логином/паролем (повторно не показывается — храните надёжно);
  * сессионный ``token`` — из ``POST /api/v2/get_token`` (TTL 7200 c),
    кэшируется и обновляется автоматически.

Каждый запрос подписывается HMAC-SHA256:
  key  = base64decode(api_secret)
  BODY_HASH = sha256_hex(тело запроса, для GET — пустая строка)
  string_to_sign (get_token):  METHOD\\nPATH\\nQUERY\\nTS\\nBODY_HASH
  string_to_sign (остальное):  METHOD\\nPATH\\nQUERY\\nTS\\nTOKEN\\nBODY_HASH
  X-Signature = base64(HMAC)
Заголовки: X-User-Id, X-Timestamp (unix, ±60 c от сервера), X-Signature, X-Token.
PATH включает префикс ``/api/v2/...``. Query-параметры в API не используются —
QUERY в подписи всегда пустой. Подпись одноразовая (nonce-кэш 120 c), поэтому на
каждый запрос генерируем свежий X-Timestamp и не пересылаем подписанный запрос.

ВАЖНО: NS.Gifts фильтрует по IP — боевой сервер должен быть в белом списке в
кабинете NS.Gifts, иначе любой запрос вернёт 403.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Union

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

log = logging.getLogger("vallshop.nsgifts")

DEFAULT_BASE_URL = "https://api.ns.gifts"
API_PREFIX = "/api/v2"

# Статусы заказа (order_info.status — целое число).
STATUS_CREATED = 0        # создан, не оплачен
STATUS_IN_PROGRESS = 10   # в обработке (напр. Steam Gift — асинхронно)
STATUS_COMPLETED = 2      # выполнен, коды выданы
STATUS_REFUNDED = 7       # возврат
STATUS_CANCELED = 5       # отменён (15 мин без оплаты)


class NSGiftsError(Exception):
    """Базовая ошибка клиента NS.Gifts."""


class NSGiftsConfigError(NSGiftsError):
    """Не заданы учётные данные / не установлен requests."""


class NSGiftsAPIError(NSGiftsError):
    """Ошибка ответа API (HTTP >= 400 или нераспознанный ответ)."""

    def __init__(self, message: str, *, http_status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


class NSGiftsClient:
    def __init__(
        self,
        *,
        api_secret: Optional[str] = None,
        user_id: Optional[Union[str, int]] = None,
        login: Optional[str] = None,
        password: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        mock: bool = False,
    ):
        self.api_secret = api_secret
        self.user_id = str(user_id) if user_id not in (None, "") else None
        self.login = login
        self.password = password
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.mock = mock
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._lock = threading.Lock()
        self._stock_cache: Optional[Dict[str, Any]] = None
        self._stock_cache_exp: float = 0.0

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **overrides: Any) -> "NSGiftsClient":
        env = env or os.environ
        params: Dict[str, Any] = dict(
            api_secret=env.get("NSGIFTS_API_SECRET") or None,
            user_id=env.get("NSGIFTS_USER_ID") or None,
            login=env.get("NSGIFTS_LOGIN") or None,
            password=env.get("NSGIFTS_PASSWORD") or None,
            base_url=env.get("NSGIFTS_BASE_URL", DEFAULT_BASE_URL),
            mock=_truthy(env.get("NSGIFTS_MOCK")),
        )
        params.update(overrides)
        return cls(**params)

    @property
    def configured(self) -> bool:
        return bool(
            self.mock
            or (self.api_secret and self.user_id and self.login and self.password)
        )

    # ── Подпись и токен ──────────────────────────────────────────────────────
    def _sign(self, method: str, path: str, ts: int, body_str: str, token: Optional[str]) -> str:
        if not self.api_secret:
            raise NSGiftsConfigError("NSGIFTS_API_SECRET не задан")
        body_hash = hashlib.sha256(body_str.encode("utf-8")).hexdigest()
        parts = [method.upper(), path, "", str(ts)]  # QUERY всегда пустой
        if token:
            parts.append(token)
        parts.append(body_hash)
        string_to_sign = "\n".join(parts)
        try:
            key = base64.b64decode(self.api_secret)
        except Exception as e:  # noqa: BLE001
            raise NSGiftsConfigError(f"NSGIFTS_API_SECRET не base64: {e}")
        mac = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac).decode("ascii")

    def _login(self) -> str:
        if requests is None:
            raise NSGiftsConfigError("Пакет 'requests' не установлен")
        if not (self.api_secret and self.user_id and self.login and self.password):
            raise NSGiftsConfigError(
                "NS.Gifts не настроен (NSGIFTS_API_SECRET / NSGIFTS_USER_ID / "
                "NSGIFTS_LOGIN / NSGIFTS_PASSWORD)"
            )
        path = API_PREFIX + "/get_token"
        body_str = json.dumps({"login": self.login, "password": self.password})
        ts = int(time.time())
        sig = self._sign("POST", path, ts, body_str, token=None)
        headers = {
            "X-User-Id": self.user_id,
            "X-Timestamp": str(ts),
            "X-Signature": sig,
            "Content-Type": "application/json",
        }
        resp = requests.post(
            self.base_url + path, data=body_str.encode("utf-8"),
            headers=headers, timeout=self.timeout,
        )
        data = self._handle(resp)
        tok = data.get("token") if isinstance(data, dict) else None
        if not tok:
            raise NSGiftsAPIError("get_token вернул ответ без token", body=data)
        self._token = tok
        # Обновляемся на 120 c раньше истечения.
        self._token_exp = time.time() + int(data.get("expires_in", 7200)) - 120
        return tok

    def _ensure_token(self) -> str:
        if not self._token or time.time() >= self._token_exp:
            self._login()
        return self._token  # type: ignore[return-value]

    # ── Базовый запрос ───────────────────────────────────────────────────────
    def _request(
        self, method: str, endpoint: str, *, body: Any = None, _retry: bool = True
    ) -> Dict[str, Any]:
        if requests is None:
            raise NSGiftsConfigError("Пакет 'requests' не установлен")
        path = API_PREFIX + endpoint
        with self._lock:
            token = self._ensure_token()
        body_str = json.dumps(body) if body is not None else ""
        ts = int(time.time())
        sig = self._sign(method, path, ts, body_str, token=token)
        headers = {
            "X-User-Id": self.user_id,
            "X-Timestamp": str(ts),
            "X-Signature": sig,
            "X-Token": token,
            "Content-Type": "application/json",
        }
        resp = requests.request(
            method.upper(), self.base_url + path,
            data=(body_str.encode("utf-8") if body_str else None),
            headers=headers, timeout=self.timeout,
        )
        if resp.status_code == 401 and _retry:
            # Токен протух/ревокнут — сбрасываем и логинимся заново (один раз).
            with self._lock:
                self._token = None
                self._token_exp = 0.0
            return self._request(method, endpoint, body=body, _retry=False)
        return self._handle(resp)

    @staticmethod
    def _handle(resp) -> Dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            raise NSGiftsAPIError(
                f"не-JSON ответ (HTTP {resp.status_code})",
                http_status=resp.status_code,
                body=(resp.text or "")[:500],
            )
        if resp.status_code >= 400:
            msg = f"HTTP {resp.status_code}"
            if isinstance(data, dict):
                msg = str(
                    data.get("message") or data.get("error")
                    or data.get("detail") or msg
                )
            raise NSGiftsAPIError(msg, http_status=resp.status_code, body=data)
        return data

    # ── Методы API ───────────────────────────────────────────────────────────
    def check_balance(self) -> Dict[str, Any]:
        """Баланс кабинета (USD, строка с 4 знаками): {"balance": "305.2403"}."""
        if self.mock:
            return {"balance": "1000.0000"}
        return self._request("GET", "/check_balance")

    def stock(self, *, use_cache: bool = False, ttl: int = 300) -> Dict[str, Any]:
        """Каталог: categories[] -> services[] (service_id, name, price, currency,
        in_stock) + fields[] (схема полей заказа).

        ``use_cache`` — отдать закэшированный ответ, если моложе ``ttl`` секунд
        (каталог большой, при переходах по админке не дёргаем сеть повторно)."""
        if self.mock:
            return {"categories": []}
        if use_cache and self._stock_cache is not None and time.time() < self._stock_cache_exp:
            return self._stock_cache
        data = self._request("GET", "/stock")
        self._stock_cache = data
        self._stock_cache_exp = time.time() + ttl
        return data

    # Классификация категории по схеме полей заказа → тип выдачи у нас.
    @staticmethod
    def category_kind(cat: Mapping[str, Any]) -> str:
        """code | steam | account | gift — по набору полей заказа категории."""
        keys = {str(f.get("key")) for f in (cat.get("fields") or [])}
        if keys == {"quantity"} or (not keys):
            return "code"
        if "friendLink" in keys or "sub_id" in keys:
            return "gift"
        if "amount" in keys and "account" in keys:
            return "steam"
        return "account"

    def create_order(
        self,
        *,
        service_id: Union[int, str],
        fields: Union[Mapping[str, Any], List[Dict[str, Any]]],
        custom_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Создать заказ (без оплаты). Возвращает {custom_id, total_to_pay, status}.

        ``fields`` — dict {key: value} или список [{key, value}]; схема полей
        берётся из /stock для нужного service_id.
        """
        custom_id = custom_id or str(uuid.uuid4())
        if isinstance(fields, Mapping):
            fields_list = [{"key": k, "value": v} for k, v in fields.items()]
        else:
            fields_list = list(fields)
        if self.mock:
            return {"custom_id": custom_id, "total_to_pay": "0.0000", "status": "created"}
        return self._request(
            "POST", "/create_order",
            body={"service_id": int(service_id), "custom_id": custom_id, "fields": fields_list},
        )

    def pay_order(self, *, custom_id: str, totp_code: Optional[str] = None) -> Dict[str, Any]:
        """Оплатить заказ: списывает баланс и запускает выдачу.
        Возвращает {status, balance, pins[], note}. Повторный вызов — 409."""
        if self.mock:
            return {"custom_id": custom_id, "status": "completed", "balance": "1000.0000",
                    "pins": ["MOCK-CODE-0000"], "note": "mock"}
        body: Dict[str, Any] = {"custom_id": custom_id}
        if totp_code:
            body["totp_code"] = totp_code
        return self._request("POST", "/pay_order", body=body)

    def order_info(self, custom_id: str) -> Dict[str, Any]:
        """Статус и выданные коды заказа. status: 0 создан, 10 в обработке,
        2 выполнен, 7 возврат, 5 отменён. Коды — в ``pins``."""
        if self.mock:
            return {"custom_id": custom_id, "status": STATUS_COMPLETED,
                    "status_message": "completed", "pins": ["MOCK-CODE-0000"]}
        return self._request("GET", f"/order_info/{custom_id}")

    def exchange_rate(self, service_id: Union[int, str] = 1) -> Dict[str, Any]:
        """Курсы (rub/kzt/uah за 1 USD). Пока поддерживается только service_id=1 (Steam)."""
        if self.mock:
            return {"rates": {"rub": 95.0, "kzt": 500.0, "uah": 41.0}}
        return self._request("POST", "/exchange_rate", body={"service_id": int(service_id)})

    def steam_check_user(self, steam_id: str) -> Dict[str, Any]:
        """Проверить логин Steam: {"accountStatus": true|false}."""
        if self.mock:
            return {"accountStatus": True}
        return self._request("POST", "/steam/check_user", body={"steam_id": steam_id})

    def steam_gift_apps(self) -> Dict[str, Any]:
        """Каталог игр для Steam Gift: apps[] (app_id, name, data_json.subs)."""
        if self.mock:
            return {"apps": []}
        return self._request("GET", "/steam_gift/get_apps")

    # ── Помощники ────────────────────────────────────────────────────────────
    @staticmethod
    def extract_pins(resp: Mapping[str, Any]) -> List[str]:
        """Достать выданные коды из ответа pay_order / order_info."""
        pins = resp.get("pins") if isinstance(resp, Mapping) else None
        if not pins:
            return []
        return [str(p) for p in pins if p not in (None, "")]
