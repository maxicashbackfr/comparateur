"""Accès base. Volontairement minimal : psycopg brut, pas d'ORM.

Le schéma est petit et les requêtes sont peu nombreuses ; un ORM ajouterait une
couche à entretenir pour un gain nul à cette échelle.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import MIGRATIONS_DIR, Settings
from .normalize import COMPARED_FIELDS, same_offer
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
    offers_written: int,
    errors_count: int,
    notes: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scrape_run SET finished_at = now(), status = %s, urls_fetched = %s,"
            " offers_found = %s, offers_written = %s, errors_count = %s, notes = %s"
            " WHERE id = %s",
            (status, urls_fetched, offers_found, offers_written, errors_count,
             notes, run_id),
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


def latest_snapshot(
    conn: psycopg.Connection, alias_id: int, offer: RawOffer
) -> dict | None:
    """Dernier relevé conservé pour cette offre, ou None.

    La clé n'est pas l'alias seul : une page porte plusieurs offres de natures
    différentes (achat, prime, bon d'achat, catégories). On compare chacune à
    celle qui lui correspond, sans quoi un taux catégoriel écraserait le taux
    d'achat à chaque passe.
    """
    fields = ", ".join(COMPARED_FIELDS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {fields} FROM offer_snapshot"
            " WHERE merchant_alias_id = %s AND kind = %s"
            "   AND category_label IS NOT DISTINCT FROM %s"
            " ORDER BY collected_at DESC LIMIT 1",
            (alias_id, offer.kind, offer.category_label),
        )
        return cur.fetchone()


def touch_alias(conn: psycopg.Connection, alias_id: int) -> None:
    """Enregistre le fait d'avoir vérifié. C'est ce qui rend la fraîcheur
    affichable même quand aucun taux n'a bougé depuis des semaines."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE merchant_alias SET last_checked_at = now() WHERE id = %s",
            (alias_id,),
        )


def record_offer(
    conn: psycopg.Connection,
    *,
    alias_id: int,
    provider_id: int,
    run_id: int,
    offer: RawOffer,
) -> bool:
    """Écrit le relevé s'il diffère du précédent. Rend True si une ligne a été
    insérée.

    Un taux bouge rarement : réécrire l'identique quatre fois par jour remplit
    la base sans rien ajouter à l'historique (voir migration 003). Le principe
    reste tenu — aucun UPDATE sur un taux, offer_snapshot est en ajout seul.
    """
    previous = latest_snapshot(conn, alias_id, offer)
    touch_alias(conn, alias_id)
    if same_offer(previous, offer):
        return False
    insert_snapshot(
        conn, alias_id=alias_id, provider_id=provider_id, run_id=run_id, offer=offer
    )
    return True


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
