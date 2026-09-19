"""Configuration — tout vient de l'environnement, rien n'est codé en dur."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    database_url: str
    user_agent: str
    min_delay: float
    cache_dir: Path
    cache_ttl: int
    slack_webhook: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        ua = os.getenv("GCB_USER_AGENT", "").strip()
        if not ua or "VOTRE-DOMAINE" in ua:
            raise RuntimeError(
                "GCB_USER_AGENT doit être renseigné avec une URL de contact joignable. "
                "Un crawler anonyme est ce qui déclenche un blocage — et c'est mérité."
            )
        return cls(
            database_url=os.getenv("DATABASE_URL", "postgresql://gcb:gcb@localhost:5432/gcb"),
            user_agent=ua,
            min_delay=float(os.getenv("GCB_MIN_DELAY", "3.0")),
            cache_dir=Path(os.getenv("GCB_CACHE_DIR", ".httpcache")),
            cache_ttl=int(os.getenv("GCB_CACHE_TTL", "86400")),
            slack_webhook=os.getenv("GCB_SLACK_WEBHOOK") or None,
        )


MIGRATIONS_DIR = ROOT / "db" / "migrations"
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
