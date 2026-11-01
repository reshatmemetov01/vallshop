"""Клиент Fragment для покупки Telegram Stars (неофициальный API + оплата TON).

У Fragment нет официального API — используется тот же поток, что и в вебе:
авторизация по кукам сессии, формирование TON-транзакции и её оплата с нашего
кошелька. Поток покупки звёзд:

    searchStarsRecipient(username, qty) -> recipient
    initBuyStarsRequest(recipient, qty) -> req_id
    getBuyStarsLink(account, req_id)    -> transaction (messages для оплаты)
    подписать и отправить оплату в TON  -> ждём зачисления

Статус определяем по seqno кошелька (см. ton.send_payment/poll). Включается
mock-режим (FRAGMENT_MOCK=1) — без сети, для тестов и разработки.

ВНИМАНИЕ: точные имена методов/полей/кук у Fragment меняются и не
документированы. Места, которые стоит сверить с рабочим примером/куками,
помечены комментарием «CALIBRATE».
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from . import ton as ton_mod

FRAGMENT_URL = "https://fragment.com"

# Дескриптор устройства для getBuyStarsLink (как у TON Connect кошелька). CALIBRATE
_DEVICE = {
    "platform": "iphone",
    "appName": "Tonkeeper",
    "appVersion": "4.10.0",
    "maxProtocolVersion": 2,
    "features": ["SendTransaction", {"name": "SendTransaction", "maxMessages": 4}],
}


def _find_messages(obj) -> list:
    """Найти список transaction-сообщений в ответе getBuyStarsLink.

    Ответ Fragment: {... "transaction"?: {"messages": [...]}, "confirm_method",
    "confirm_params"}. Структура обёртки может меняться — ищем ключ "messages"
    рекурсивно (список словарей с address/amount).
    """
    if isinstance(obj, dict):
        m = obj.get("messages")
        if isinstance(m, list) and m and isinstance(m[0], dict) and m[0].get("address"):
            return m
        for v in obj.values():
            found = _find_messages(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_messages(v)
            if found:
                return found
    return []


class FragmentError(Exception):
    pass


class FragmentConfigError(FragmentError):
    pass


class FragmentClient:
    def __init__(self, *, cookies: Optional[str] = None, mnemonic: Optional[str] = None,
                 tonapi_key: Optional[str] = None, tonapi_base: str = ton_mod.DEFAULT_TONAPI,
                 wallet_version: str = "v5r1", api_hash: Optional[str] = None,
                 mock: bool = False, timeout: float = 30.0):
        self.cookies = cookies
        self.mnemonic = mnemonic
        self.wallet_version = wallet_version
        # можно задать вручную (из /api?hash=...); терпим лишний префикс «api?hash=»/«hash=»
        self.api_hash = re.sub(r"^.*?hash=", "", api_hash.strip()) if api_hash else None
        self.mock = mock
        self.timeout = timeout
        self.tonapi = ton_mod.TonApi(api_key=tonapi_key, base_url=tonapi_base, timeout=timeout)
        self._hash: Optional[str] = None
        self._hash_ts: float = 0.0
        self._wallet: Optional[Dict[str, Any]] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **overrides) -> "FragmentClient":
        import os
        env = env if env is not None else os.environ

        def truthy(v):
            return str(v).strip().lower() in {"1", "true", "yes", "on"}

        kwargs = {
            "cookies": env.get("FRAGMENT_COOKIES") or None,
            "mnemonic": env.get("TON_WALLET_MNEMONIC") or env.get("FRAGMENT_TON_MNEMONIC") or None,
            "tonapi_key": env.get("TONAPI_KEY") or env.get("TON_API_KEY") or None,
            "tonapi_base": env.get("TONAPI_BASE_URL", ton_mod.DEFAULT_TONAPI),
            "wallet_version": env.get("TON_WALLET_VERSION", "v5r1"),
            "api_hash": env.get("FRAGMENT_API_HASH") or None,
            "mock": truthy(env.get("FRAGMENT_MOCK")),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def configured(self) -> bool:
        return self.mock or bool(self.cookies and self.mnemonic)

    # ── Кошелёк (офлайн) ─────────────────────────────────────────────────────
    def _wallet_info(self) -> Dict[str, Any]:
        if self._wallet is None:
            if not self.mnemonic:
                raise FragmentConfigError("TON_WALLET_MNEMONIC не задан")
            self._wallet = ton_mod.load_wallet(self.mnemonic, self.wallet_version)
        return self._wallet

    def balance(self) -> Dict[str, Any]:
        """Баланс TON-кошелька (для мониторинга в админке)."""
        if self.mock:
            return {"balance": Decimal("100"), "currency": "TON"}
        wi = self._wallet_info()
        return {"balance": self.tonapi.balance_ton(wi["address"]), "currency": "TON",
                "address": wi["address"]}

    # ── Fragment API ─────────────────────────────────────────────────────────
    def _http(self):
        if requests is None:
            raise FragmentConfigError("Пакет 'requests' не установлен, а mock выключен")
        return requests

    def _api_hash(self) -> str:
        """hash для /api: сначала заданный вручную (FRAGMENT_API_HASH), иначе со страницы."""
        if self.api_hash:
            return self.api_hash
        now = time.time()
        if self._hash and (now - self._hash_ts) < 600:
            return self._hash
        http = self._http()
        r = http.get(f"{FRAGMENT_URL}/stars", headers=self._page_headers(),
                     cookies=None, timeout=self.timeout)
        m = re.search(r'/api\?hash=([0-9a-f]+)', r.text) or re.search(r'"apiHash":"([0-9a-f]+)"', r.text)
        if not m:
            raise FragmentError("Не удалось получить api hash (куки протухли?)")
        self._hash, self._hash_ts = m.group(1), now
        return self._hash

    def _page_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
            "Cookie": self.cookies or "",
            "Accept": "text/html,application/xhtml+xml",
        }

    def _api(self, method: str, params: Dict[str, Any]) -> dict:
        http = self._http()
        data = dict(params)
        data["method"] = method
        headers = {
            **self._page_headers(),
            "X-Requested-With": "XMLHttpRequest",
            "Origin": FRAGMENT_URL,
            "Referer": f"{FRAGMENT_URL}/stars",
            "Accept": "application/json",
        }
        r = http.post(f"{FRAGMENT_URL}/api?hash={self._api_hash()}",
                      data=data, headers=headers, timeout=self.timeout)
        try:
            payload = r.json()
        except ValueError:
            raise FragmentError(f"Fragment вернул не JSON (HTTP {r.status_code})")
        if isinstance(payload, dict) and payload.get("error"):
            raise FragmentError(str(payload.get("error")))
        if r.status_code >= 400:
            raise FragmentError(f"Fragment HTTP {r.status_code}")
        return payload

    # ── Покупка звёзд ────────────────────────────────────────────────────────
    def buy_stars(self, *, username: str, quantity: int,
                  idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        """Купить ``quantity`` звёзд на @``username``. Оплата уходит в TON.

        Возвращает состояние для сохранения в заказ:
        ``{ok, order, status, seqno_at_send, ton_amount}``. Бросает FragmentError.
        """
        uname = (username or "").lstrip("@").strip()
        qty = int(quantity)
        if not uname or qty <= 0:
            raise FragmentError("Не указан получатель или количество звёзд")

        if self.mock:
            return {"ok": True, "order": f"frag-mock-{idempotency_key or uname}-{qty}",
                    "status": "completed", "seqno_at_send": 0, "ton_amount": "0"}

        # 1) получатель
        rec = self._api("searchStarsRecipient", {"query": uname, "quantity": qty})  # CALIBRATE
        recipient = (rec.get("found") or {}).get("recipient") or rec.get("recipient")
        if not recipient:
            raise FragmentError(f"Получатель @{uname} не найден на Fragment")
        # 2) инициализация запроса (обязателен payment_method=ton — иначе Access denied)
        init = self._api("initBuyStarsRequest", {
            "recipient": recipient, "quantity": qty, "payment_method": "ton",
        })
        req_id = init.get("req_id") or init.get("id")
        if not req_id:
            raise FragmentError("Fragment не вернул req_id")
        # 3) платёжная транзакция (кошелёк Fragment берёт из сессии по stel_ton_token).
        #    Браузер шлёт ровно id/show_sender/transaction — без account/device.
        wi = self._wallet_info()
        link = self._api("getBuyStarsLink", {
            "id": req_id, "show_sender": 1, "transaction": 1,
        })
        messages = _find_messages(link)
        if not messages:
            raise FragmentError("Fragment не вернул платёжную транзакцию")
        # 4) подписать и оплатить с нашего W5-кошелька
        sent = ton_mod.send_payment(wi, messages, self.tonapi)
        ton_amount = str(messages[0].get("amount")) if messages else "0"
        # 5) подтвердить запрос у Fragment (confirm_method/params из ответа)
        confirm_method = link.get("confirm_method")
        confirm_params = link.get("confirm_params") or {"id": req_id}
        if confirm_method:
            try:
                self._api(confirm_method, dict(confirm_params))
            except FragmentError:
                pass  # оплата уже ушла; подтверждение подхватит и поллер
        return {"ok": True, "order": sent["ext_hash"], "status": "pending",
                "seqno_at_send": sent["seqno_at_send"], "ton_amount": ton_amount,
                "req_id": req_id}

    def poll(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        """Проверить статус оплаты по seqno кошелька. {status: pending|completed}."""
        if self.mock:
            return {"status": "completed"}
        try:
            wi = self._wallet_info()
            cur = self.tonapi.seqno(wi["address"])
        except Exception as e:  # noqa: BLE001 — сеть недоступна, попробуем позже
            raise FragmentError(str(e))
        at_send = int(state.get("seqno_at_send") or 0)
        # seqno вырос → внешнее сообщение принято сетью → оплата ушла, звёзды зачислены
        return {"status": "completed" if cur > at_send else "pending"}

    # Совместимость с интерфейсом «статус по ref» (не используется для seqno-режима).
    @staticmethod
    def norm_status(raw) -> str:
        return str(raw or "pending").lower()
