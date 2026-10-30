"""Реестр SMM-панелей (поставщиков накрутки).

soc-proof и 1xpanel используют один и тот же стандартный API панелей
(POST, параметры ``key``+``action``, действия services/add/status/balance),
поэтому оба обслуживаются одним классом-клиентом ``SocProofClient`` — отличие
лишь в ``base_url`` и ключе.

У номинала поле ``Variant.source`` хранит ИМЯ панели: ``"socproof"`` или
``"onexpanel"``. Весь код (оформление, досыл, возвраты, поллинг статусов,
авто-переоценка, цены, наличие) выбирает нужную панель по этому имени через
``is_smm`` и словарь клиентов из ``build_clients``.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

from socproof import SocProofClient

# Конфигурация панелей. Добавить третью — дописать сюда запись.
PANELS: Dict[str, dict] = {
    "socproof": {
        "label": "soc-proof",
        "base_url": "https://soc-proof.su/api/v2",
        "key_envs": ("SOCPROOF_API_KEY", "SOCPROOF_KEY"),
        "base_env": "SOCPROOF_BASE_URL",
        "mock_env": "SOCPROOF_MOCK",
    },
    "onexpanel": {
        "label": "1xpanel",
        "base_url": "https://1xpanel.com/api/v2",
        "key_envs": ("ONEXPANEL_API_KEY", "ONEXPANEL_KEY"),
        "base_env": "ONEXPANEL_BASE_URL",
        "mock_env": "ONEXPANEL_MOCK",
    },
}

SMM_SOURCES = frozenset(PANELS)
DEFAULT_PANEL = "socproof"


def is_smm(source: Optional[str]) -> bool:
    """True, если ``source`` — одна из SMM-панелей."""
    return source in SMM_SOURCES


def label(source: Optional[str]) -> str:
    cfg = PANELS.get(source or "")
    return cfg["label"] if cfg else (source or "—")


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def build_clients(env: Optional[Mapping[str, str]] = None) -> Dict[str, SocProofClient]:
    """Собрать клиентов для всех панелей из окружения: {имя: SocProofClient}."""
    env = env if env is not None else os.environ
    out: Dict[str, SocProofClient] = {}
    for name, cfg in PANELS.items():
        key = next((env.get(k) for k in cfg["key_envs"] if env.get(k)), None)
        out[name] = SocProofClient(
            api_key=key,
            base_url=env.get(cfg["base_env"], cfg["base_url"]),
            mock=_truthy(env.get(cfg["mock_env"])),
        )
    return out


def configured_panels(clients: Mapping[str, SocProofClient]) -> Dict[str, SocProofClient]:
    """Только настроенные панели (есть ключ или включён mock)."""
    return {n: c for n, c in clients.items() if getattr(c, "configured", False)}


def client_for(clients: Optional[Mapping[str, SocProofClient]], source: Optional[str],
               fallback: Optional[SocProofClient] = None) -> Optional[SocProofClient]:
    """Клиент панели по имени ``source`` (с запасным вариантом)."""
    if clients and source in clients:
        return clients[source]
    return fallback
