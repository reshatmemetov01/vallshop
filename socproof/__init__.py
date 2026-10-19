"""Чистый клиент SMM-панели soc-proof.su (Perfect Panel API v2)."""

from .client import (
    SocProofClient,
    SocProofError,
    SocProofAPIError,
    SocProofConfigError,
    SUCCESS_STATUSES,
    PARTIAL_STATUSES,
    FAILURE_STATUSES,
    IN_PROGRESS_STATUSES,
)

__all__ = [
    "SocProofClient",
    "SocProofError",
    "SocProofAPIError",
    "SocProofConfigError",
    "SUCCESS_STATUSES",
    "PARTIAL_STATUSES",
    "FAILURE_STATUSES",
    "IN_PROGRESS_STATUSES",
]

__version__ = "0.1.0"
