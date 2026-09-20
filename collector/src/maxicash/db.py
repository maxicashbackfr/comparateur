"""Accès base. Volontairement minimal : psycopg brut, pas d'ORM.

Le schéma est petit et les requêtes sont peu nombreuses ; un ORM ajouterait une
couche à entretenir pour un gain nul à cette échelle.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import MIGRATIONS_DIR, Settings
from .types import RawMerchant, RawOffer


@contextmanager
def connect(settings: Settings) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


def migrate(conn: psycopg.Connection) -> list[str]:
    """Applique les migrations dans l'ordre. Idempotent : les fichiers sont
    écrits en CREATE ... IF NOT EXISTS, et les appliqués sont mémorisés."""
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        done = {row["filename"] for row in cur.fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)
    conn.commit()
    return applied


def upsert_provider(conn: psycopg.Connection, slug: str, name: str, base_url: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO provider (slug, name, base_url) VALUES (%s, %s, %s)"
            " ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name,"
            " base_url = EXCLUDED.base_url RETURNING id",
            (slug, name, base_url),
        )
        return cur.fetchone()["id"]


def start_run(conn: psycopg.Connection, provider_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scrape_run (provider_id) VALUES (%s) RETURNING id", (provider_id,)
        )
        run_id = cur.fetchone()["id"]
    conn.commit()
    return run_id


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    status: str,
    urls_fetched: int,
    offers_found: int,
    errors_count: int,
    notes: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scrape_run SET finished_at = now(), status = %s, urls_fetched = %s,"
            " offers_found = %s, errors_count = %s, notes = %s WHERE id = %s",
            (status, urls_fetched, offers_found, errors_count, notes, run_id),
        )
    conn.commit()


def upsert_alias(conn: psycopg.Connection, provider_id: int, merchant: RawMerchant) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO merchant_alias (provider_id, raw_slug, raw_name, raw_url)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (provider_id, raw_slug) DO UPDATE SET raw_name = EXCLUDED.raw_name,"
            " raw_url = EXCLUDED.raw_url RETURNING id",
            (provider_id, merchant.raw_slug, merchant.raw_name, merchant.raw_url),
        )
        return cur.fetchone()["id"]


def insert_snapshot(
    conn: psycopg.Connection,
    *,
    alias_id: int,
    provider_id: int,
    run_id: int,
    offer: RawOffer,
) -> None:
    """Toujours un INSERT. Jamais d'UPDATE : c'est l'historique qui fait la
    valeur du produit, et il ne se reconstitue pas après coup."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO offer_snapshot (merchant_alias_id, provider_id, scrape_run_id,"
            " value, value_base, unit, kind, category_label, conditions_text, is_upto,"
            " is_new_customer_only, is_sale_excluded, is_marketplace_excluded,"
            " effective_value, raw_text)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                alias_id, provider_id, run_id,
                offer.value, offer.value_base, offer.unit, offer.kind,
                offer.category_label, offer.conditions_text, offer.is_upto,
                offer.is_new_customer_only, offer.is_sale_excluded,
                offer.is_marketplace_excluded, offer.effective_value, offer.raw_text,
            ),
        )


def queue_review(conn: psycopg.Connection, kind: str, payload: dict) -> None:
    import json

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_queue (kind, payload) VALUES (%s, %s)",
            (kind, json.dumps(payload, ensure_ascii=False)),
        )


def refresh_current(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY offer_current")
    conn.commit()


def previous_offer_count(conn: psycopg.Connection, provider_id: int) -> int | None:
    """Nombre d'offres de la dernière passe réussie — sert à détecter une
    variation anormale, qui signale presque toujours un DOM cassé plutôt qu'un
    vrai mouvement de marché."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT offers_found FROM scrape_run WHERE provider_id = %s AND status = 'ok'"
            " ORDER BY finished_at DESC LIMIT 1",
            (provider_id,),
        )
        row = cur.fetchone()
    return row["offers_found"] if row else None
