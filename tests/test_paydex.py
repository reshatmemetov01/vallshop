"""Paydex: подпись вебхука, парсинг, создание счёта (mock), маппинг статусов."""

import hashlib
import hmac
import json
import time
from decimal import Decimal

import pytest

from bot.payments.paydex import PaydexProvider, _norm_status


SECRET = "whsec_paydex_123"


def _sign(raw: bytes) -> str:
    mac = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def test_status_map():
    assert _norm_status("paid") == "paid"
    assert _norm_status("failed") == "failed"
    assert _norm_status("expired") == "failed"
    assert _norm_status("refunded") == "failed"
    assert _norm_status("partially_refunded") == "failed"
    assert _norm_status("created") == "pending"
    assert _norm_status("pending") == "pending"


def test_verify_webhook_ok_and_bad():
    p = PaydexProvider(secret=SECRET)
    raw = b'{"event":"invoice.paid","data":{"invoice":{"id":"x","status":"paid"}}}'
    ts = int(time.time())
    good = {"X-Paydex-Signature": _sign(raw), "X-Paydex-Timestamp": str(ts)}
    assert p.verify_webhook(raw, good) is True
    # подпись без префикса sha256= тоже валидна
    bare = {"X-Paydex-Signature": _sign(raw).split("=", 1)[1], "X-Paydex-Timestamp": str(ts)}
    assert p.verify_webhook(raw, bare) is True
    # подделка тела
    assert p.verify_webhook(raw + b" ", good) is False
    # неверная подпись
    assert p.verify_webhook(raw, {"X-Paydex-Signature": f"sha256={'0'*64}"}) is False
    # нет заголовка
    assert p.verify_webhook(raw, {}) is False
    # просроченное время (> 5 мин)
    old = {"X-Paydex-Signature": _sign(raw), "X-Paydex-Timestamp": str(ts - 1000)}
    assert p.verify_webhook(raw, old) is False


def test_parse_webhook():
    p = PaydexProvider(secret=SECRET)
    body = {
        "id": "evt-1",
        "event": "invoice.paid",
        "data": {"invoice": {"id": "inv-123", "orderId": "tu-abc", "status": "paid"}},
    }
    upd = p.parse_webhook(json.dumps(body).encode())
    assert upd.status == "paid"
    assert upd.client_ref == "tu-abc"
    assert upd.provider_order_id == "inv-123"


def test_parse_webhook_status_from_event():
    # Если в объекте счёта статуса нет — выводим из типа события.
    p = PaydexProvider(secret=SECRET)
    body = {"event": "invoice.failed", "data": {"invoice": {"id": "inv-9", "orderId": "tu-z"}}}
    upd = p.parse_webhook(json.dumps(body).encode())
    assert upd.status == "failed"


@pytest.mark.asyncio
async def test_create_invoice_mock():
    p = PaydexProvider(secret=SECRET, mock=True)
    inv = await p.create_invoice(
        amount=Decimal("500"), client_ref="tu-xyz",
        description="top-up", success_url="https://shop.vallshop.com", network="sbp",
    )
    assert inv.currency == "RUB"
    assert inv.provider_order_id
    assert inv.checkout_url
    assert inv.network == "sbp"
    assert inv.amount == Decimal("500")


@pytest.mark.asyncio
async def test_get_status_mock_paid():
    p = PaydexProvider(secret=SECRET, mock=True)
    upd = await p.get_status("pdx-mock-tu-xyz")
    assert upd.status == "paid"


def test_to_amount():
    assert PaydexProvider._to_amount(Decimal("500")) == "500.00"
    assert PaydexProvider._to_amount(Decimal("78.4")) == "78.40"
    assert PaydexProvider._to_amount(Decimal("1500.005")) == "1500.01"
