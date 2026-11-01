"""Проба Fragment: проверить куки/кошелёк и увидеть реальные ответы API.

Без оплаты (по умолчанию). Нужен, чтобы сверить точные имена методов/полей
Fragment с живой сессией и откалибровать клиент (места CALIBRATE в
fragment/client.py). Запускать на сервере, где заданы FRAGMENT_COOKIES,
TON_WALLET_MNEMONIC, TONAPI_KEY.

    docker compose exec bot python -m scripts.fragment_probe @username 100
    docker compose exec bot python -m scripts.fragment_probe @username 100 --pay   # реально купить!

Шаги без --pay: api hash → searchStarsRecipient → initBuyStarsRequest →
getBuyStarsLink (сырой JSON), но БЕЗ подписи/оплаты. Баланс TON — показывается.
"""

from __future__ import annotations

import json
import sys

from fragment import FragmentClient, FragmentError
from fragment import ton as ton_mod


def _show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:3000])


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    pay = "--pay" in sys.argv
    username = (args[0] if args else "@durov").lstrip("@")
    quantity = int(args[1]) if len(args) > 1 else 50

    c = FragmentClient.from_env()
    print("configured:", c.configured, "| mock:", c.mock)
    if not c.cookies:
        print("⚠ FRAGMENT_COOKIES не заданы");
    if not c.mnemonic:
        print("⚠ TON_WALLET_MNEMONIC не задан")

    # Кошелёк и баланс
    try:
        wi = c._wallet_info()
        print("TON адрес:", wi["address"])
        print("Баланс TON:", c.tonapi.balance_ton(wi["address"]))
    except Exception as e:  # noqa: BLE001
        print("Кошелёк/баланс: ошибка:", e)

    if pay:
        print(f"\n>>> РЕАЛЬНАЯ покупка {quantity}⭐ для @{username} …")
        try:
            print(json.dumps(c.buy_stars(username=username, quantity=quantity), ensure_ascii=False, indent=2))
        except FragmentError as e:
            print("Ошибка покупки:", e)
        return

    # Без оплаты: пройти шаги и показать сырые ответы.
    try:
        print("\napi hash:", c._api_hash())
    except FragmentError as e:
        print("api hash: ошибка:", e); return
    try:
        rec = c._api("searchStarsRecipient", {"query": username, "quantity": quantity})
        _show("searchStarsRecipient", rec)
        recipient = (rec.get("found") or {}).get("recipient") or rec.get("recipient")
        if not recipient:
            print("⚠ recipient не найден — сверь ключи ответа"); return
        init = c._api("initBuyStarsRequest", {
            "recipient": recipient, "quantity": quantity, "payment_method": "ton"})
        _show("initBuyStarsRequest", init)
        req_id = init.get("req_id") or init.get("id")
        if not req_id:
            print("⚠ req_id не найден — сверь ключи ответа"); return
        # как в браузере: только id/show_sender/transaction, без account/device
        link = c._api("getBuyStarsLink", {"id": req_id, "show_sender": 1, "transaction": 1})
        _show("getBuyStarsLink (транзакция для оплаты, НЕ оплачено)", link)
        print("\nЕсли messages[] есть — всё готово к оплате. Запусти с --pay для реальной покупки.")
    except FragmentError as e:
        print("Ошибка шага:", e)


if __name__ == "__main__":
    main()
