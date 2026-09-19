"""Registre des adaptateurs. Ajouter une plateforme = une entrée ici."""
from __future__ import annotations

from typing import Callable

from .widilo import WidiloAdapter

REGISTRY: dict[str, Callable] = {
    WidiloAdapter.slug: WidiloAdapter,
}


def get_adapter(slug: str, fetch):
    try:
        return REGISTRY[slug](fetch)
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "aucun"
        raise SystemExit(f"Adaptateur inconnu : {slug}. Connus : {known}") from None
