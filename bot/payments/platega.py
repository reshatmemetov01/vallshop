"""Платёжный провайдер Platega — приём ₽ (СБП QR / карта / SberPay).

Док: https://docs.platega.io/

Авторизация: заголовки ``X-MerchantId`` (UUID магазина) и ``X-Secret``
(API-ключ) на КАЖДОМ запросе. Эти же заголовки Platega присылает в колбэке —
по ним и проверяем подлинность (отдельной HMAC-подписи у Platega нет).

Создание счёта:
  POST {base}/transaction/process
  тело: {paymentMethod (int), paymentDetails:{amount, currency:"RUB"},
         description, return (success-redirect), failedUrl, payload (=client_ref),
         metadata:{user_email, userId}}
  ответ: {transactionId (uuid), redirect (ссылка оплаты), status, ...}
Статус:
  GET {base}/transaction/{id} -> {status: PENDING|CONFIRMED|CANCELED|CHARGEBACKED}
Колбэк (Platega -> мерчант), URL задаётся в кабинете «Настройки → Callback URLs»:
  заголовки X-MerchantId / X-Secret (сверяем со своими);
  тело: {id (=transactionId), payload (=client_ref), amount, currency,
         status (CONFIRMED=оплачено), paymentMethod}. Ответ: 200 OK.

Коды методов Platega: 2=СБП(QR), 11=карта, 13=крипта, 14=SberPay.
"""

from __future__ import annotations

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

log = logging.getLogger("vallshop.platega")

# Наш код способа -> целочисленный paymentMethod Platega.
_METHOD_INT = {"sbp": 2, "card": 11, "crypto": 13, "sberpay": 14}

# Статусы Platega -> наши (pending | paid | failed).
_PAID = {"confirmed"}
_FAILED = {"canceled", "cancelled", "chargebacked", "declined",
           "partially_refunded", "disputed", "failed", "expired"}


def _norm_status(raw: Optional[str]) -> str:
    s = str(raw or "").strip().lower()
    if s in _PAID:
        return "paid"
    if s in _FAILED:
        return "failed"
    return "pending"


class PlategaProvider(PaymentProvider):
    name = "platega"

    def __init__(
        self,
        *,
        base_url: str = "https://app.platega.io",
        merchant_id: Optional[str] = None,
        secret: Optional[str] = None,
        method: str = "sbp",
        from_email: str = "noreply@vallshop.com",
        timeout: float = 30.0,
        mock: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.merchant_id = merchant_id or ""
        self.secret = secret or ""          # API-ключ = X-Secret (и для колбэка)
        self.method = method or "sbp"
        self.from_email = from_email or "noreply@vallshop.com"
        self.timeout = timeout
        self.mock = mock

    # ── Транспорт ────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-MerchantId": self.merchant_id,
            "X-Secret": self.secret,
        }

    async def _request(self, method: str, path: str, *, json_body=None) -> dict:
        if httpx is None:
            raise RuntimeError("Пакет 'httpx' не установлен")
        url = self.base_url + path
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(
                method, url,
                content=json.dumps(json_body).encode() if json_body is not None else None,
                headers=self._headers(),
            )
            if resp.status_code >= 400:
                log.error("Platega %s %s -> HTTP %s: %s", method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _to_amount(amount: Decimal):
        """Platega принимает число; целые суммы шлём int, иначе — с копейками."""
        q = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return int(q) if q == q.to_integral_value() else float(q)

    def _method_int(self, code: Optional[str]) -> int:
        c = (code or self.method or "sbp").lower()
        return _METHOD_INT.get(c, _METHOD_INT["sbp"])

    # ── Создание счёта ─────────────────────────────────────────────────────────
    async def create_invoice(
        self,
        *,
        amount: Decimal,
        client_ref: str,
        description: str,
        notify_url: Optional[str] = None,  # URL колбэка задаётся в кабинете Platega
        success_url: Optional[str] = None,
        network: Optional[str] = None,     # наш код способа: sbp|card|sberpay
    ) -> Invoice:
        pm = self._method_int(network)
        ret = success_url or "https://vallshop.com"
        body = {
            "paymentMethod": pm,
            "paymentDetails": {"amount": self._to_amount(amount), "currency": "RUB"},
            "description": description or "Пополнение баланса",
            "return": ret,
            "failedUrl": ret,
            "payload": client_ref,
            "metadata": {"user_email": self.from_email, "userId": client_ref},
        }

        if self.mock:
            full = {
                "transactionId": f"pg-mock-{client_ref}",
                "redirect": (ret + f"?mock_platega={client_ref}"),
                "status": "PENDING",
            }
        else:
            full = await self._request("POST", "/transaction/process", json_body=body)
            try:
                log.info("Platega invoice: %s", json.dumps(full, ensure_ascii=False)[:600])
            except Exception:  # noqa: BLE001
                pass

        tid = str(full.get("transactionId") or full.get("id") or client_ref)
        return Invoice(
            provider_order_id=tid,
            checkout_url=full.get("redirect") or full.get("url"),
            address=None,
            network=(network or self.method),
            amount=Decimal(amount),
            currency="RUB",
            expires_at=None,
            raw=full,
        )

    async def get_status(self, ref: str) -> PaymentUpdate:
        """Статус по transactionId (храним его в topup.provider_order_id)."""
        if self.mock:
            return PaymentUpdate(provider_order_id=ref, status="paid", client_ref=None)
        d = await self._request("GET", f"/transaction/{ref}")
        return PaymentUpdate(
            provider_order_id=str(d.get("id") or ref),
            status=_norm_status(d.get("status")),
            client_ref=d.get("payload") or d.get("externalId"),
            amount=None,
            raw=d,
        )

    # ── Вебхук (колбэк) ───────────────────────────────────────────────────────
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """У Platega нет HMAC — сверяем X-Secret (и X-MerchantId) со своими."""
        if self.mock:
            return True
        if not self.secret:
            return False
        lower = {k.lower(): v for k, v in headers.items()}
        sec = lower.get("x-secret") or ""
        if not hmac.compare_digest(str(sec), self.secret):
            return False
        mid = lower.get("x-merchantid")
        if self.merchant_id and mid is not None:
            if not hmac.compare_digest(str(mid), self.merchant_id):
                return False
        return True

    def parse_webhook(self, raw_body: bytes) -> PaymentUpdate:
        data = json.loads(raw_body.decode("utf-8"))
        d = data if isinstance(data, dict) else {}
        return PaymentUpdate(
            provider_order_id=str(d.get("id")) if d.get("id") else None,
            status=_norm_status(d.get("status")),
            client_ref=d.get("payload"),
            amount=None,
            raw=data,
        )
