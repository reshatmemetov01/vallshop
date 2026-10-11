"""Платёжный провайдер Paydex — приём ₽ по СБП.

Док: https://paydex.pro/docs

Создание счёта:
  POST {base}/invoices   (Authorization: Bearer <secret_key>)
  тело: {amount ("1500.00"), orderId, method ("sbp"), description?,
         successUrl?, failUrl?}
  ответ (201): {id (uuid), url ("https://paydex.pro/pay/<id>"), status, amount,
         expiresAt}; повтор того же orderId → 200 с тем же счётом.
Статус:
  GET {base}/invoices/{id} -> {... status, orderId ...}
  статусы: created -> pending -> paid | failed | expired
           (после оплаты: refunded | partially_refunded)
Вебхук (Paydex -> мерчант), webhook_url настраивается в кабинете проекта:
  заголовки: ``X-Paydex-Signature: sha256=<hex>``, ``X-Paydex-Timestamp`` (unix),
             ``X-Paydex-Event``, ``X-Paydex-Delivery-Id``;
  подпись = HMAC-SHA256( raw_body , webhook_secret ), hex;
  допуск по времени ±5 минут; тело: {id, event ("invoice.paid" и т.д.),
  createdAt, data:{invoice:{id, orderId, status, ...}}}.
  Ответ эндпоинта: любой 2xx; при неверной подписи — 401.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from .base import Invoice, PaymentProvider, PaymentUpdate

log = logging.getLogger("vallshop.paydex")

# Статусы Paydex -> наши (pending | paid | failed | expired).
_PAID = {"paid", "succeeded"}
_FAILED = {"failed", "cancelled", "canceled", "expired", "refunded", "partially_refunded"}

_SIG_RE = re.compile(r"(?:sha256=)?([a-f0-9]{64})")
_TOLERANCE_SEC = 300  # ±5 минут


def _norm_status(raw: Optional[str]) -> str:
    s = str(raw or "").strip().lower()
    if s in _PAID:
        return "paid"
    if s in _FAILED:
        return "failed"
    return "pending"


class PaydexProvider(PaymentProvider):
    name = "paydex"

    def __init__(
        self,
        *,
        base_url: str = "https://paydex.pro/api/v1",
        api_key: Optional[str] = None,
        secret: Optional[str] = None,
        method: str = "sbp",
        timeout: float = 30.0,
        mock: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret = secret or ""  # webhook_secret (отличается от secret_key)
        self.method = method or "sbp"
        self.timeout = timeout
        self.mock = mock

    # ── Транспорт ────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    @staticmethod
    def _unwrap(data):
        if isinstance(data, dict):
            for key in ("data", "invoice"):
                if isinstance(data.get(key), dict):
                    return data[key]
        return data if isinstance(data, dict) else {}

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
                log.error("Paydex %s %s -> HTTP %s: %s", method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _to_amount(amount: Decimal) -> str:
        return str(Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    # ── Создание счёта ─────────────────────────────────────────────────────────
    async def create_invoice(
        self,
        *,
        amount: Decimal,
        client_ref: str,
        description: str,
        notify_url: Optional[str] = None,  # webhook_url настраивается в кабинете
        success_url: Optional[str] = None,
        network: Optional[str] = None,  # способ оплаты (по умолчанию sbp)
    ) -> Invoice:
        method = (network or self.method or "sbp").lower()
        body = {
            "amount": self._to_amount(amount),
            "orderId": client_ref,
            "method": method,
            "description": description,
        }
        if success_url:
            body["successUrl"] = success_url

        if self.mock:
            full = {
                "id": f"pdx-mock-{client_ref}",
                "url": (success_url or "https://paydex.pro/pay/mock") + f"?orderId={client_ref}",
                "status": "created",
            }
        else:
            full = await self._request("POST", "/invoices", json_body=body)
            try:
                log.info("Paydex invoice created: %s", json.dumps(full, ensure_ascii=False)[:800])
            except Exception:  # noqa: BLE001
                pass

        d = self._unwrap(full)
        return Invoice(
            provider_order_id=str(d.get("id") or full.get("id") or client_ref),
            checkout_url=d.get("url") or full.get("url"),
            address=None,
            network=method,
            amount=Decimal(amount),
            currency="RUB",
            expires_at=None,
            raw=full,
        )

    async def get_status(self, ref: str) -> PaymentUpdate:
        """Статус счёта по id Paydex (храним его в topup.provider_order_id)."""
        if self.mock:
            return PaymentUpdate(provider_order_id=ref, status="paid", client_ref=None)
        d = self._unwrap(await self._request("GET", f"/invoices/{ref}"))
        return PaymentUpdate(
            provider_order_id=str(d.get("id") or ref),
            status=_norm_status(d.get("status")),
            client_ref=d.get("orderId"),
            amount=None,
            raw=d,
        )

    # ── Вебхук ────────────────────────────────────────────────────────────────
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        if self.mock:
            return True
        lower = {k.lower(): v for k, v in headers.items()}
        if not self.secret:
            return False
        m = _SIG_RE.search(lower.get("x-paydex-signature") or "")
        if not m:
            return False
        provided = m.group(1)
        ts = lower.get("x-paydex-timestamp") or ""
        # Защита от повторов/подделки времени (если заголовок есть).
        if ts:
            try:
                if abs(int(time.time()) - int(ts)) > _TOLERANCE_SEC:
                    return False
            except ValueError:
                return False
        expected = hmac.new(self.secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided, expected)

    def parse_webhook(self, raw_body: bytes) -> PaymentUpdate:
        data = json.loads(raw_body.decode("utf-8"))
        inv = {}
        if isinstance(data, dict):
            d = data.get("data")
            if isinstance(d, dict) and isinstance(d.get("invoice"), dict):
                inv = d["invoice"]
            elif isinstance(d, dict):
                inv = d
        event = str(data.get("event") or "").lower() if isinstance(data, dict) else ""
        # Статус берём из объекта счёта; если его нет — выводим из типа события.
        status = _norm_status(inv.get("status"))
        if status == "pending" and event:
            if event.endswith(".paid"):
                status = "paid"
            elif event.endswith(".failed") or event.endswith(".expired"):
                status = "failed"
        return PaymentUpdate(
            provider_order_id=str(inv.get("id")) if inv.get("id") else None,
            status=status,
            client_ref=inv.get("orderId"),
            amount=None,
            raw=data,
        )
