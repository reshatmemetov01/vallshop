"""Проверка связи с NS.Gifts: токен + баланс + первые позиции каталога.

Запускать НА БОЕВОМ СЕРВЕРЕ (его IP должен быть в белом списке NS.Gifts):
    cd /opt/vallshop && python3 scripts/nsgifts_probe.py

Читает ключи из .env (NSGIFTS_*). Ничего не покупает — только GET-запросы.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nsgifts import NSGiftsAPIError, NSGiftsClient, NSGiftsConfigError  # noqa: E402


def _load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    _load_env(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    cli = NSGiftsClient.from_env()
    if not cli.configured:
        print("❌ NS.Gifts не настроен — заполните NSGIFTS_* в .env")
        return 1

    try:
        print("→ get_token + check_balance …")
        bal = cli.check_balance()
        print(f"✅ Баланс: {bal.get('balance')} USD")
    except NSGiftsConfigError as e:
        print(f"❌ Конфигурация: {e}")
        return 1
    except NSGiftsAPIError as e:
        print(f"❌ API ошибка (HTTP {e.http_status}): {e}")
        print(f"   Тело: {e.body}")
        if e.http_status == 403:
            print("   → Похоже, IP сервера не в белом списке NS.Gifts. Добавьте его в кабинете.")
        if e.http_status == 401:
            print("   → Подпись/таймстамп/секрет. Проверьте NSGIFTS_API_SECRET и время на сервере (NTP).")
        return 1

    try:
        print("→ stock (каталог) …")
        stock = cli.stock()
        cats = stock.get("categories") or []
        print(f"✅ Категорий: {len(cats)}")
        shown = 0
        for c in cats:
            svcs = c.get("services") or []
            print(f"  • {c.get('category_name')} (id={c.get('category_id')}) — услуг: {len(svcs)}")
            for s in svcs[:3]:
                print(f"      - {s.get('service_id')}: {s.get('service_name')} | "
                      f"{s.get('price')} {s.get('currency')} | в наличии: {s.get('in_stock')}")
            shown += 1
            if shown >= 5:
                print("  … (показаны первые 5 категорий)")
                break
    except NSGiftsAPIError as e:
        print(f"❌ stock ошибка (HTTP {e.http_status}): {e}")
        return 1

    print("\n✅ Связь с NS.Gifts работает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
