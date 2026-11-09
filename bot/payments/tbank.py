"""Платёжный провайдер T-Bank (Tinkoff) — интернет-эквайринг (EACQ), приём ₽.

Док: https://developer.tbank.ru/eacq/ (API эквайринга Tinkoff/T-Bank).

Авторизация запросов — подписью ``Token`` в теле (НЕ заголовком):
  1) взять корневые скалярные поля запроса (без вложенных Receipt/DATA и без
     самого Token);
  2) добавить пару Password=<пароль терминала>;
  3) отсортировать пары по имени ключа (A→Z);
  4) склеить ЗНАЧЕНИЯ в одну строку (bool → "true"/"false");
  5) SHA-256 от строки, hex в нижнем регистре.

Создание счёта:
  POST {base}/Init   тело JSON: TerminalKey, Amount(копейки,int), OrderId,
       Description, NotificationURL, SuccessURL, FailURL, Token
  ответ: {Success, Status, PaymentId, PaymentURL, ErrorCode, Message, ...}
Статус:
  POST {base}/GetState  тело: TerminalKey, PaymentId, Token
  ответ: {Success, Status, PaymentId, OrderId, Amount, ...}
Уведомление (T-Bank → мерчант) на NotificationURL:
  тело JSON: TerminalKey, OrderId, Success, Status, PaymentId, Amount, Token, …
  подпись проверяем тем же алгоритмом; в ответ нужно вернуть строку "OK".

Статус CONFIRMED = оплачено (одностадийная оплата). Для корректной работы
нужен ОДНОСТАДИЙНЫЙ терминал (без отдельного Confirm); двухстадийный даёт
AUTHORIZED (холд) — у нас считается как pending.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from .base import Invoice, PaymentProvider, PaymentUpdate

log = logging.getLogger("vallshop.tbank")

# Статусы T-Bank → наши (pending | paid | failed).
_PAID = {"CONFIRMED"}
_FAILED = {"REJECTED", "AUTH_FAIL", "CANCELED", "CANCELLED", "DEADLINE_EXPIRED",
           "REVERSED", "REVERSING", "REFUNDED", "PARTIAL_REFUNDED", "REFUNDING"}
# Всё прочее (NEW, FORM_SHOWED, AUTHORIZING, 3DS_*, AUTHORIZED, CONFIRMING…) — pending.


def _norm_status(raw: Optional[str]) -> str:
    s = str(raw or "").strip().upper()
    if s in _PAID:
        return "paid"
    if s in _FAILED:
        return "failed"
    return "pending"


def _tv(v) -> str:
    """Сериализация значения для подписи: bool → true/false, иначе str()."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class TBankProvider(PaymentProvider):
    name = "tbank"

    def __init__(
        self,
        *,
        base_url: str = "https://securepay.tinkoff.ru/v2",
        terminal_key: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
        mock: bool = False,
    ) -> None:
        self.base_url = (base_url or "https://securepay.tinkoff.ru/v2").rstrip("/")
        self.terminal_key = terminal_key or ""
        self.password = password or ""
        self.timeout = timeout
        self.mock = mock

    # ── Подпись ────────────────────────────────────────────────────────────────
    def token(self, params: Mapping) -> str:
        """Token по алгоритму T-Bank (см. модуль). Вложенные поля и Token — мимо."""
        pairs = {
            k: v for k, v in params.items()
            if k != "Token" and v is not None and not isinstance(v, (dict, list))
        }
        pairs["Password"] = self.password
        concat = "".join(_tv(pairs[k]) for k in sorted(pairs.keys()))
        return hashlib.sha256(concat.encode("utf-8")).hexdigest()

    # ── Транспорт ──────────────────────────────────────────────────────────────
    async def _request(self, path: str, body: dict) -> dict:
        if httpx is None:
            raise RuntimeError("Пакет 'httpx' не установлен")
        url = self.base_url + path
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            if resp.status_code >= 400:
                log.error("T-Bank %s -> HTTP %s: %s", path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _kopecks(amount: Decimal) -> int:
        q = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int((q * 100).to_integral_value(rounding=ROUND_HALF_UP))

    # ── Создание счёта ─────────────────────────────────────────────────────────
    async def create_invoice(
        self,
        *,
        amount: Decimal,
        client_ref: str,
        description: str,
        notify_url: Optional[str] = None,
        success_url: Optional[str] = None,
        network: Optional[str] = None,
    ) -> Invoice:
        body = {
            "TerminalKey": self.terminal_key,
            "Amount": self._kopecks(amount),
            "OrderId": client_ref,
            "Description": (description or "Пополнение баланса")[:140],
        }
        if notify_url:
            body["NotificationURL"] = notify_url
        if success_url:
            body["SuccessURL"] = success_url
            body["FailURL"] = success_url
        body["Token"] = self.token(body)

        if self.mock:
            full = {
                "Success": True, "Status": "NEW",
                "PaymentId": f"tb-mock-{client_ref}",
                "PaymentURL": (success_url or "https://vallshop.com") + f"?mock_tbank={client_ref}",
            }
        else:
            full = await self._request("/Init", body)
            try:
                log.info("T-Bank Init: %s", json.dumps(full, ensure_ascii=False)[:600])
            except Exception:  # noqa: BLE001
                pass
            if not full.get("Success"):
                raise RuntimeError(
                    f"T-Bank Init отклонён: ErrorCode={full.get('ErrorCode')} "
                    f"{full.get('Message') or ''} {full.get('Details') or ''}".strip()
                )

        return Invoice(
            provider_order_id=str(full.get("PaymentId") or client_ref),
            checkout_url=full.get("PaymentURL"),
            address=None,
            network=network,
            amount=Decimal(amount),
            currency="RUB",
            expires_at=None,
            raw=full,
        )

    async def get_status(self, ref: str) -> PaymentUpdate:
        """Статус по PaymentId (храним его в topup.provider_order_id)."""
        if self.mock:
            return PaymentUpdate(provider_order_id=ref, status="paid", client_ref=None)
        body = {"TerminalKey": self.terminal_key, "PaymentId": str(ref)}
        body["Token"] = self.token(body)
        d = await self._request("/GetState", body)
        return PaymentUpdate(
            provider_order_id=str(d.get("PaymentId") or ref),
            status=_norm_status(d.get("Status")),
            client_ref=d.get("OrderId"),
            amount=None,
            raw=d,
        )

    # ── Уведомление (вебхук) ───────────────────────────────────────────────────
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """Проверить Token в теле уведомления (заголовки не используются)."""
        if self.mock:
            return True
        if not self.password:
            return False
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(data, dict):
            return False
        got = str(data.get("Token") or "")
        if not got:
            return False
        expected = self.token(data)
        return hmac.compare_digest(got, expected)

    def parse_webhook(self, raw_body: bytes) -> PaymentUpdate:
        data = json.loads(raw_body.decode("utf-8"))
        d = data if isinstance(data, dict) else {}
        return PaymentUpdate(
            provider_order_id=str(d.get("PaymentId")) if d.get("PaymentId") else None,
            status=_norm_status(d.get("Status")),
            client_ref=d.get("OrderId"),
            amount=None,
            raw=data,
        )
