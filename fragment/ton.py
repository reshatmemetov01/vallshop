"""TON-кошелёк для оплаты Fragment: подпись перевода (W5/v5r1) и отправка.

Кошелёк — Tonkeeper W5 (v5r1), поддерживает батчи (несколько сообщений в одной
транзакции). Подпись офлайн через tonutils, чтение seqno/баланса и отправка
готового BOC — через tonapi (tonconsole). tonutils импортируется лениво, чтобы
mock-режим и тесты работали без установленного пакета.

Статус оплаты определяем по seqno кошелька: внешнее сообщение принимается сетью
только когда seqno увеличивается, поэтому «seqno вырос с момента отправки» =
платёж ушёл (Fragment зачисляет звёзды почти сразу после оплаты).
"""

from __future__ import annotations

import asyncio as _asyncio
import base64
from decimal import Decimal
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

DEFAULT_TONAPI = "https://tonapi.io"


class TonError(Exception):
    pass


class TonApi:
    """Тонкая обёртка над tonapi (tonconsole): seqno, баланс, отправка BOC."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: str = DEFAULT_TONAPI,
                 timeout: float = 30.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _get(self, path: str) -> dict:
        if requests is None:
            raise TonError("Пакет 'requests' не установлен")
        r = requests.get(self.base_url + path, headers=self._headers(), timeout=self.timeout)
        if r.status_code >= 400:
            raise TonError(f"tonapi GET {path} → HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def seqno(self, address: str) -> int:
        """Текущий seqno кошелька (0, если контракт ещё не задеплоен)."""
        try:
            data = self._get(f"/v2/blockchain/accounts/{address}/methods/seqno")
        except TonError:
            return 0
        dec = data.get("decoded") or {}
        for k in ("state", "seqno"):
            if isinstance(dec.get(k), int):
                return dec[k]
        stack = data.get("stack") or []
        if stack and isinstance(stack[0], dict):
            num = stack[0].get("num")
            if num is not None:
                try:
                    return int(str(num), 16) if str(num).startswith("0x") else int(num)
                except ValueError:
                    pass
        return 0

    def balance_ton(self, address: str) -> Decimal:
        data = self._get(f"/v2/accounts/{address}")
        nano = Decimal(str(data.get("balance") or 0))
        return (nano / Decimal(10) ** 9).quantize(Decimal("0.000000001"))

    def send_boc(self, boc_b64: str) -> None:
        if requests is None:
            raise TonError("Пакет 'requests' не установлен")
        r = requests.post(
            self.base_url + "/v2/blockchain/message",
            json={"boc": boc_b64}, headers=self._headers(), timeout=self.timeout,
        )
        if r.status_code >= 400:
            raise TonError(f"tonapi отправка BOC → HTTP {r.status_code}: {r.text[:200]}")


def _b64_to_bytes(s: str) -> bytes:
    """Декодировать base64/base64url, добивая отсутствующий паддинг (Fragment его опускает)."""
    s = (s or "").strip()
    if "-" in s or "_" in s:                 # url-safe → стандартный алфавит
        s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)                 # добить паддинг до кратности 4
    return base64.b64decode(s)


def _wallet_class(version: str):
    from tonutils.contracts.wallet import WalletV5R1, WalletV4R2
    v = (version or "v5r1").lower()
    if v in ("v5r1", "v5", "w5"):
        return WalletV5R1
    if v in ("v4r2", "v4"):
        return WalletV4R2
    raise TonError(f"Неподдерживаемая версия кошелька: {version} (нужна v5r1/W5)")


def load_wallet(mnemonic: str, version: str = "v5r1") -> Dict[str, Any]:
    """Кошелёк из seed-фразы (офлайн). Адреса + state_init + объект для подписи."""
    from tonutils.clients import TonapiClient
    from ton_core import NetworkGlobalID

    cls = _wallet_class(version)
    words = [w for w in (mnemonic or "").replace(",", " ").split() if w]
    client = TonapiClient(NetworkGlobalID.MAINNET, api_key="offline")  # конструктор без сети
    wallet, pub, _priv, _m = cls.from_mnemonic(client, words)
    si = wallet.state_init
    si_boc = (si.serialize() if hasattr(si, "serialize") else si.to_cell()).to_boc()
    pub_hex = pub.as_hex() if callable(getattr(pub, "as_hex", None)) else getattr(pub, "as_hex", "")
    return {
        "wallet": wallet,
        "version": version,
        "address": wallet.address.to_str(is_user_friendly=True, is_bounceable=True),
        "address_raw": wallet.address.to_str(is_user_friendly=False),
        "public_key": pub_hex,
        "state_init_b64": base64.b64encode(si_boc).decode(),
    }


def account_payload(wi: Dict[str, Any], chain: str = "-239") -> Dict[str, str]:
    """Параметр ``account`` для Fragment getBuyStarsLink (как в TON Connect)."""
    return {
        "address": wi["address_raw"],
        "chain": chain,                      # -239 = mainnet
        "walletStateInit": wi["state_init_b64"],
        "publicKey": wi["public_key"],
    }


def send_payment(wi: Dict[str, Any], messages: List[dict], tonapi: TonApi) -> Dict[str, Any]:
    """Подписать и отправить оплату Fragment (батч W5). {ext_hash, seqno_at_send}.

    ``messages`` — из ответа getBuyStarsLink: ``[{address, amount, payload}]``
    (amount — строка нанотонов, payload — base64 BOC тела). W5 поддерживает
    несколько сообщений в одной транзакции.
    """
    from tonutils.contracts.wallet import TONTransferBuilder
    from tonutils.contracts.wallet.versions.v5 import WalletV5Params
    from ton_core import Address, Cell

    if not messages:
        raise TonError("Fragment не вернул платёжное сообщение")
    builders = []
    for m in messages:
        to_addr = m.get("address")
        amount = int(str(m.get("amount") or "0"))
        if not to_addr or amount <= 0:
            raise TonError("Некорректное платёжное сообщение Fragment")
        payload_b64 = m.get("payload")
        body = Cell.one_from_boc(_b64_to_bytes(payload_b64)) if payload_b64 else None
        builders.append(TONTransferBuilder(destination=Address(to_addr), amount=amount, body=body))

    seqno = tonapi.seqno(wi["address"])
    wallet = wi["wallet"]
    built = wallet.build_external_message(builders, WalletV5Params(seqno=seqno))
    if _asyncio.iscoroutine(built):            # tonutils: сборка сообщения — корутина
        built = _asyncio.run(built)
    boc_b64 = _attr(built, "as_b64")
    nh = _attr(built, "normalized_hash")
    ext_hash = nh.hex() if hasattr(nh, "hex") else str(nh)
    tonapi.send_boc(boc_b64)
    return {"ext_hash": ext_hash, "seqno_at_send": seqno}


def _attr(obj, name):
    """Значение атрибута независимо от того, метод это или свойство."""
    v = getattr(obj, name)
    return v() if callable(v) else v
