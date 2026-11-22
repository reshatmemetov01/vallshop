"""Клиент оптового API NS.Gifts (api.ns.gifts, v2)."""

from .client import (
    NSGiftsClient,
    NSGiftsError,
    NSGiftsConfigError,
    NSGiftsAPIError,
    STATUS_CREATED,
    STATUS_IN_PROGRESS,
    STATUS_COMPLETED,
    STATUS_REFUNDED,
    STATUS_CANCELED,
)

__all__ = [
    "NSGiftsClient",
    "NSGiftsError",
    "NSGiftsConfigError",
    "NSGiftsAPIError",
    "STATUS_CREATED",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "STATUS_REFUNDED",
    "STATUS_CANCELED",
]
