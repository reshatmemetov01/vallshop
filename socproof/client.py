"""Автономный клиент SMM-панели soc-proof.su (Perfect Panel API v2).

Особенности панели (проверено на живом ответе):
* База: ``https://soc-proof.su/api/v2``, метод POST, тело form-urlencoded.
* Авторизация: параметр ``key`` в теле запроса.
* Деньги — в РУБЛЯХ. ``balance`` → ``{"balance","currency":"RUB"}``;
  у услуги ``rate`` — цена за 1000 в рублях. Курс/USD тут не участвуют.
* Список услуг: ``action=services`` → список
  ``{"service","name","type","rate","min","max","dripfeed","refill",
     "cancel","category"}``. Кратности (step) в ответе нет — только min/max.
* Заказ: ``action=add`` + ``service`` + ``link`` + ``quantity`` →
  ``{"order": <id>}``. Статус: ``action=status`` + ``order`` →
  ``{"charge","start_count","status","remains","currency"}``.

В первой версии поддерживаем услуги типа ``Default`` (ссылка + количество).

Единственная зависимость — ``requests`` (только для реальных вызовов; в
mock-режиме сеть не нужна).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://soc-proof.su/api/v2"

# Статусы заказа (Perfect Panel). Сравниваем в нижнем регистре.
SUCCESS_STATUSES = frozenset({"completed", "complete"})
PARTIAL_STATUSES = frozenset({"partial"})
FAILURE_STATUSES = frozenset({"canceled", "cancelled", "refunded", "fail", "failed", "error"})
IN_PROGRESS_STATUSES = frozenset(
    {"pending", "in progress", "processing", "in_progress", "queue", "queued"}
)


class SocProofError(Exception):
    pass


class SocProofConfigError(SocProofError):
    pass


class SocProofAPIError(SocProofError):
    def __init__(self, message, *, http_status=None, body=None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.body = body


class SocProofClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        mock: bool = False,
        timeout: float = 30.0,
        session: Any = None,
        cache_ttl: float = 300.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.mock = mock
        self.timeout = timeout
        self._session = session
        self._cache_ttl = cache_ttl
        self._services_cache: Optional[List[Dict[str, Any]]] = None
        self._services_at: float = 0.0

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **overrides: Any) -> "SocProofClient":
        import os

        env = env if env is not None else os.environ

        def truthy(v):
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        kwargs: Dict[str, Any] = {
            "api_key": env.get("SOCPROOF_API_KEY") or env.get("SOCPROOF_KEY") or None,
            "base_url": env.get("SOCPROOF_BASE_URL", DEFAULT_BASE_URL),
            "mock": truthy(env.get("SOCPROOF_MOCK")),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def configured(self) -> bool:
        return self.mock or bool(self.api_key)

    # ── Транспорт ────────────────────────────────────────────────────────────
    def _request(self, action: str, **params: Any) -> Any:
        if self.mock:
            return self._mock(action, params)
        if not self.api_key:
            raise SocProofConfigError("SOCPROOF_API_KEY не задан")
        if requests is None:
            raise SocProofConfigError("Пакет 'requests' не установлен, а mock выключен")

        http = self._session or requests
        data = {"key": self.api_key, "action": action}
        data.update({k: v for k, v in params.items() if v is not None})
        resp = http.request("POST", self.base_url, data=data, timeout=self.timeout)
        return self._handle(resp)

    @staticmethod
    def _handle(resp) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise SocProofAPIError(
                f"Ответ не JSON (HTTP {resp.status_code})",
                http_status=resp.status_code,
                body=getattr(resp, "text", None),
            )
        if isinstance(data, dict) and data.get("error"):
            raise SocProofAPIError(
                str(data.get("error")), http_status=resp.status_code, body=data
            )
        if not (200 <= resp.status_code < 300):
            raise SocProofAPIError(
                f"Ошибка soc-proof (HTTP {resp.status_code})",
                http_status=resp.status_code, body=data,
            )
        return data

    # ── Аккаунт ──────────────────────────────────────────────────────────────
    def balance(self) -> Dict[str, Any]:
        """action=balance -> {balance, currency}."""
        return self._request("balance")

    # ── Каталог услуг ────────────────────────────────────────────────────────
    def services(self, *, force: bool = False) -> List[Dict[str, Any]]:
        """action=services -> список всех услуг (с кэшем на cache_ttl секунд)."""
        now = time.monotonic()
        if (
            not force
            and self._services_cache is not None
            and (now - self._services_at) < self._cache_ttl
        ):
            return self._services_cache
        data = self._request("services")
        rows = [r for r in (data or []) if isinstance(r, Mapping)]
        out = [self._norm_service(r) for r in rows]
        self._services_cache = out
        self._services_at = now
        return out

    @staticmethod
    def _norm_service(r: Mapping[str, Any]) -> Dict[str, Any]:
        def _int(v, default=0):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return default

        return {
            "id": _int(r.get("service")),
            "name": str(r.get("name") or "").strip(),
            "type": str(r.get("type") or "").strip(),
            "rate": str(r.get("rate") or "0"),          # ₽ за 1000 (строкой)
            "min": _int(r.get("min")),
            "max": _int(r.get("max")),
            "category": str(r.get("category") or "").strip(),
            "dripfeed": bool(r.get("dripfeed")),
            "refill": bool(r.get("refill")),
            "cancel": bool(r.get("cancel")),
        }

    def get_service(self, service_id: int, *, force: bool = False) -> Optional[Dict[str, Any]]:
        """Найти услугу по id (из кэша/свежего списка)."""
        sid = int(service_id)
        for s in self.services(force=force):
            if s["id"] == sid:
                return s
        return None

    def search(
        self, query: str, *, only_default: bool = True, limit: int = 40
    ) -> List[Dict[str, Any]]:
        """Поиск услуг. Число → по ID услуги (вхождение), текст → по названию/категории."""
        q = (query or "").strip().lower()
        numeric = q.isdigit()
        out: List[Dict[str, Any]] = []
        for s in self.services():
            if only_default and s["type"] != "Default":
                continue
            if not q:
                match = True
            elif numeric:
                match = q in str(s["id"])
            else:
                match = q in s["name"].lower() or q in s["category"].lower()
            if match:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    # ── Заказы ───────────────────────────────────────────────────────────────
    def add(self, *, service: int, link: str, quantity: int, **extra: Any) -> Dict[str, Any]:
        """action=add -> {order: <id>}."""
        return self._request(
            "add", service=int(service), link=link, quantity=int(quantity), **extra
        )

    def status(self, order: Any) -> Dict[str, Any]:
        """action=status -> {charge, start_count, status, remains, currency}."""
        return self._request("status", order=order)

    def multi_status(self, orders: List[Any]) -> Dict[str, Any]:
        """action=status с несколькими id -> {order_id: {...}}."""
        joined = ",".join(str(o) for o in orders)
        return self._request("status", orders=joined)

    # ── Разбор статуса ─────────────────────────────────────────────────────────
    @staticmethod
    def norm_status(raw: Any) -> str:
        return str(raw or "").strip().lower()

    @classmethod
    def is_completed(cls, raw: Any) -> bool:
        return cls.norm_status(raw) in SUCCESS_STATUSES

    @classmethod
    def is_partial(cls, raw: Any) -> bool:
        return cls.norm_status(raw) in PARTIAL_STATUSES

    @classmethod
    def is_failed(cls, raw: Any) -> bool:
        return cls.norm_status(raw) in FAILURE_STATUSES

    @classmethod
    def is_terminal(cls, raw: Any) -> bool:
        return cls.is_completed(raw) or cls.is_partial(raw) or cls.is_failed(raw)

    # ── Mock ─────────────────────────────────────────────────────────────────
    def _mock(self, action: str, params: Mapping[str, Any]) -> Any:
        if action == "balance":
            return {"balance": "1000.00", "currency": "RUB"}
        if action == "services":
            return [
                {"service": 707, "name": "TG подписчики", "type": "Default",
                 "rate": "9.00", "min": 10, "max": 60000, "dripfeed": True,
                 "refill": True, "cancel": True, "category": "Telegram подписчики"},
                {"service": 500, "name": "TG просмотры", "type": "Default",
                 "rate": "3.00", "min": 100, "max": 150000, "dripfeed": False,
                 "refill": False, "cancel": True, "category": "Telegram просмотры"},
                {"service": 900, "name": "TG премиум пакет", "type": "Package",
                 "rate": "500.00", "min": 1, "max": 1, "dripfeed": False,
                 "refill": False, "cancel": False, "category": "Telegram премиум"},
            ]
        if action == "add":
            return {"order": 1234567}
        if action == "status":
            return {
                "charge": "9.00", "start_count": "100", "status": "In progress",
                "remains": "0", "currency": "RUB",
            }
        return {}
