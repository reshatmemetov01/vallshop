"""TON-кошелёк для оплаты Fragment: подпись перевода и отправка через tonapi.

Подпись делается офлайн (tonsdk), отправка готового BOC и чтение seqno/баланса —
через tonapi (tonconsole). tonsdk импортируется лениво, чтобы mock-режим и тесты
работали без установленного пакета.

Статус оплаты определяем по seqno кошелька: внешнее сообщение принимается сетью
только когда seqno увеличивается, поэтому «seqno вырос с момента отправки» =
платёж ушёл (Fragment зачисляет звёзды почти сразу после оплаты).
"""

from __future__ import annotations

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
        # tonapi: {"success":true,"stack":[{"type":"num","num":"0x5"}], "decoded":{"state":5}}
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


def load_wallet(mnemonic: str, version: str = "v4r2", workchain: int = 0) -> Dict[str, Any]:
    """Кошелёк из seed-фразы (офлайн). Возвращает объект + адреса + state_init."""
    from tonsdk.contract.wallet import Wallets, WalletVersionEnum
    from tonsdk.utils import bytes_to_b64str

    ver = {
        "v3r2": WalletVersionEnum.v3r2, "v4r1": WalletVersionEnum.v4r1,
        "v4r2": WalletVersionEnum.v4r2,
    }.get(version.lower(), WalletVersionEnum.v4r2)
    words = [w for w in (mnemonic or "").replace(",", " ").split() if w]
    _m, pub, _priv, wallet = Wallets.from_mnemonics(words, ver, workchain)
    state_init = wallet.create_init_external_message()["state_init"]
    return {
        "wallet": wallet,
        "address_raw": wallet.address.to_string(False, False, False),
        "address": wallet.address.to_string(True, True, True),
        "public_key": pub.hex(),
        "state_init_b64": bytes_to_b64str(state_init.to_boc(False)),
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
    """Подписать и отправить оплату Fragment. Возвращает {ext_hash, seqno_at_send}.

    ``messages`` — из ответа getBuyStarsLink: ``[{address, amount, payload}]``
    (amount — строка нанотонов, payload — base64 BOC тела сообщения). Fragment
    для звёзд отдаёт одно сообщение.
    """
    from tonsdk.boc import Cell
    from tonsdk.utils import bytes_to_b64str

    if not messages:
        raise TonError("Fragment не вернул платёжное сообщение")
    msg = messages[0]
    to_addr = msg.get("address")
    amount = int(str(msg.get("amount") or "0"))
    payload_b64 = msg.get("payload")
    if not to_addr or amount <= 0:
        raise TonError("Некорректное платёжное сообщение Fragment")
    payload_cell = Cell.one_from_boc(base64.b64decode(payload_b64)) if payload_b64 else None

    seqno = tonapi.seqno(wi["address"])
    wallet = wi["wallet"]
    query = wallet.create_transfer_message(
        to_addr=to_addr, amount=amount, seqno=seqno, payload=payload_cell,
    )
    signed = query["message"]
    tonapi.send_boc(bytes_to_b64str(signed.to_boc(False)))
    return {"ext_hash": signed.bytes_hash().hex(), "seqno_at_send": seqno}
