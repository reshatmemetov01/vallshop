"""Клиент soc-proof: форма запроса, разбор ответа, фильтр Default, статусы."""

import pytest

from socproof import SocProofClient, SocProofAPIError


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class _Session:
    """Фейковый requests-like: отдаёт заранее заданный ответ, пишет запросы."""

    def __init__(self, data, status=200):
        self._data = data
        self._status = status
        self.calls = []

    def request(self, method, url, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "data": data})
        return _Resp(self._data, self._status)


def _client(data, status=200):
    sess = _Session(data, status)
    return SocProofClient(api_key="k", session=sess), sess


def test_balance_sends_key_and_action():
    c, sess = _client({"balance": "51.58", "currency": "RUB"})
    assert c.balance() == {"balance": "51.58", "currency": "RUB"}
    sent = sess.calls[0]
    assert sent["method"] == "POST"
    assert sent["data"] == {"key": "k", "action": "balance"}


def test_services_normalizes_and_caches():
    rows = [
        {"service": "707", "name": "TG", "type": "Default", "rate": "9.00",
         "min": "10", "max": "60000", "category": "Telegram", "refill": True},
        {"service": 900, "name": "Пакет", "type": "Package", "rate": "500",
         "min": 1, "max": 1, "category": "Premium"},
    ]
    c, sess = _client(rows)
    svc = c.services()
    assert svc[0] == {
        "id": 707, "name": "TG", "type": "Default", "rate": "9.00",
        "min": 10, "max": 60000, "category": "Telegram",
        "dripfeed": False, "refill": True, "cancel": False,
    }
    # второй вызов — из кэша, без нового запроса
    c.services()
    assert len(sess.calls) == 1
    # force=True — обновляет
    c.services(force=True)
    assert len(sess.calls) == 2


def test_search_default_only_and_query():
    rows = [
        {"service": 1, "name": "TG подписчики", "type": "Default", "rate": "9",
         "min": 10, "max": 100, "category": "Telegram"},
        {"service": 2, "name": "TG пакет премиум", "type": "Package", "rate": "500",
         "min": 1, "max": 1, "category": "Premium"},
        {"service": 3, "name": "IG лайки", "type": "Default", "rate": "5",
         "min": 50, "max": 500, "category": "Instagram"},
    ]
    c, _ = _client(rows)
    # Package отфильтрован, поиск по подстроке
    res = c.search("tg")
    assert [s["id"] for s in res] == [1]
    # поиск по категории
    assert [s["id"] for s in c.search("instagram")] == [3]
    # с only_default=False пакет виден
    assert {s["id"] for s in c.search("премиум", only_default=False)} == {2}


def test_search_by_id_numeric():
    rows = [
        {"service": 707, "name": "TG подписчики", "type": "Default", "rate": "9",
         "min": 10, "max": 100, "category": "Telegram"},
        {"service": 452, "name": "TG просмотры 707", "type": "Default", "rate": "3",
         "min": 10, "max": 100, "category": "Telegram"},
    ]
    c, _ = _client(rows)
    # число → поиск по ID (вхождение), не по названию
    assert [s["id"] for s in c.search("707")] == [707]
    # частичный id
    assert {s["id"] for s in c.search("7")} == {707}
    # текст → по названию
    assert {s["id"] for s in c.search("просмотры")} == {452}


def test_add_and_status_params():
    c, sess = _client({"order": 42})
    assert c.add(service=707, link="https://t.me/x", quantity=100) == {"order": 42}
    assert sess.calls[0]["data"] == {
        "key": "k", "action": "add", "service": 707,
        "link": "https://t.me/x", "quantity": 100,
    }
    c2, sess2 = _client({"status": "Completed", "remains": "0", "start_count": "10"})
    c2.status(42)
    assert sess2.calls[0]["data"] == {"key": "k", "action": "status", "order": 42}


def test_api_error_raised():
    c, _ = _client({"error": "Not enough funds"})
    with pytest.raises(SocProofAPIError) as e:
        c.balance()
    assert "Not enough funds" in str(e.value)


def test_status_helpers():
    assert SocProofClient.is_completed("Completed")
    assert SocProofClient.is_partial("Partial")
    assert SocProofClient.is_failed("Canceled")
    assert SocProofClient.is_terminal("Completed")
    assert not SocProofClient.is_terminal("In progress")
    assert not SocProofClient.is_completed("In progress")


def test_mock_mode_no_network():
    c = SocProofClient(mock=True)
    assert c.configured
    assert c.balance()["currency"] == "RUB"
    svc = c.services()
    assert any(s["type"] == "Default" for s in svc)
    # поиск в mock отфильтрует Package
    assert all(s["type"] == "Default" for s in c.search(""))
    assert c.add(service=1, link="l", quantity=10)["order"] == 1234567
