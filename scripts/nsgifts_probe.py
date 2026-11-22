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
        total_services = sum(len(c.get("services") or []) for c in cats)
        print(f"✅ Категорий: {len(cats)}, услуг всего: {total_services}")

        print("\n— Первые 5 категорий (примеры) —")
        for c in cats[:5]:
            svcs = c.get("services") or []
            print(f"  • {c.get('category_name')} (id={c.get('category_id')}) — услуг: {len(svcs)}")
            for s in svcs[:3]:
                print(f"      - {s.get('service_id')}: {s.get('service_name')} | "
                      f"{s.get('price')} {s.get('currency')} | в наличии: {s.get('in_stock')}")

        # Сводка по РАЗНЫМ схемам полей заказа — это главное для встройки выдачи.
        print("\n— Схемы полей заказа (уникальные наборы по всем категориям) —")
        seen = {}
        for c in cats:
            flds = c.get("fields") or []
            keyset = tuple((f.get("key"), f.get("type"), bool(f.get("required"))) for f in flds)
            if keyset not in seen:
                seen[keyset] = c.get("category_name")
                if not flds:
                    print(f"  [пусто]  (пример: {c.get('category_name')})")
                else:
                    desc = ", ".join(
                        f"{f.get('key')}:{f.get('type')}"
                        f"{'*' if f.get('required') else ''}"
                        f"{f' [{f.get('min')}..{f.get('max')}]' if (f.get('min') is not None or f.get('max') is not None) else ''}"
                        for f in flds
                    )
                    print(f"  {desc}   (пример категории: {c.get('category_name')})")
        print(f"\n  Всего уникальных схем полей: {len(seen)}")
    except NSGiftsAPIError as e:
        print(f"❌ stock ошибка (HTTP {e.http_status}): {e}")
        return 1

    print("\n✅ Связь с NS.Gifts работает. Скиньте этот вывод — встрою импорт и выдачу.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
