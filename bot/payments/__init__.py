"""Платёжный слой магазина VallShop."""

from .base import Invoice, PaymentProvider, PaymentUpdate
from .boltutil import BoltUtilProvider
from .mock import MockProvider
from .paydex import PaydexProvider
from .payhot import PayHotProvider


def build_payhot(config):
    """Собрать PayHot-провайдера (₽: карта/СБП/SberPay) или None, если не настроен."""
    if not getattr(config, "payhot_enabled", False):
        return None
    return PayHotProvider(
        base_url=config.payhot_base_url,
        api_key=config.payhot_api_key,
        secret=config.payhot_secret,
        methods=config.payhot_method_list,
        mock=config.payhot_mock,
    )


def build_paydex(config):
    """Собрать Paydex-провайдера (₽ по СБП) или None, если не настроен."""
    if not getattr(config, "paydex_enabled", False):
        return None
    return PaydexProvider(
        base_url=config.paydex_base_url,
        api_key=config.paydex_api_key,
        secret=config.paydex_secret,
        mock=config.paydex_mock,
    )


def build_provider(config) -> PaymentProvider:
    """Собрать провайдера по конфигу."""
    if config.mock_payments or not (config.bolt_api_key and config.bolt_secret):
        return MockProvider(
            network=config.bolt_network,
            expire_minutes=config.payment_timeout_minutes,
        )
    return BoltUtilProvider(
        base_url=config.bolt_base_url,
        api_key=config.bolt_api_key,
        secret=config.bolt_secret,  # Webhook Secret — ключ HMAC для всего
        network=config.bolt_network,
        expire_minutes=config.payment_timeout_minutes,
    )


__all__ = [
    "Invoice",
    "PaymentProvider",
    "PaymentUpdate",
    "BoltUtilProvider",
    "MockProvider",
    "PayHotProvider",
    "PaydexProvider",
    "build_provider",
    "build_payhot",
    "build_paydex",
]
