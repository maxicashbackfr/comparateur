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
from .matching import PRIORITIES, KnownMerchant, Target
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
            "INSERT INTO merchant_alias (provider_id, raw_slug, raw_name, raw_url, raw_domain)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (provider_id, raw_slug) DO UPDATE SET raw_name = EXCLUDED.raw_name,"
            " raw_url = EXCLUDED.raw_url,"
            " raw_domain = COALESCE(EXCLUDED.raw_domain, merchant_alias.raw_domain)"
            " RETURNING id",
            (provider_id, merchant.raw_slug, merchant.raw_name, merchant.raw_url,
             merchant.raw_domain),
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
            " ORDER BY collected_at DESC, id DESC LIMIT 1",
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


def find_alias(conn: psycopg.Connection, provider_id: int, raw_slug: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM merchant_alias WHERE provider_id = %s AND raw_slug = %s",
            (provider_id, raw_slug),
        )
        row = cur.fetchone()
    return row["id"] if row else None


def close_missing_offers(
    conn: psycopg.Connection,
    *,
    alias_id: int,
    provider_id: int,
    run_id: int,
    seen: set[tuple[str, str | None]],
) -> int:
    """Clôture les offres en cours absentes de la page lue à l'instant.

    Une clôture est un INSERT (value NULL, même kind et category_label) : la
    date de fin entre dans l'historique, et les vues cessent d'afficher
    l'offre. Rend le nombre d'offres clôturées.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (kind, COALESCE(category_label, ''))"
            "       kind, category_label, unit, value"
            " FROM offer_snapshot WHERE merchant_alias_id = %s"
            " ORDER BY kind, COALESCE(category_label, ''), collected_at DESC, id DESC",
            (alias_id,),
        )
        latest = cur.fetchall()
    closed = 0
    for row in latest:
        if row["value"] is None or (row["kind"], row["category_label"]) in seen:
            continue
        insert_snapshot(
            conn, alias_id=alias_id, provider_id=provider_id, run_id=run_id,
            offer=RawOffer(
                raw_text="offre absente de la page",
                value=None, unit=row["unit"], kind=row["kind"],
                category_label=row["category_label"],
            ),
        )
        closed += 1
    return closed


def refresh_current(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY offer_current")
        cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY offer_current_all")
    conn.commit()


def publish_merchants_with_offers(conn: psycopg.Connection) -> int:
    """Publie tout marchand ayant au moins une offre en cours, quel qu'en soit
    le type. Ne dépublie jamais : une page indexée qui disparaît perd son
    référencement ; une page sans offre du moment reste utile (historique)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE merchant SET is_published = TRUE"
            " WHERE NOT is_published"
            "   AND id IN (SELECT merchant_id FROM offer_current_all"
            "              WHERE merchant_id IS NOT NULL)"
        )
        count = cur.rowcount
    conn.commit()
    return count


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


# --- appariement -------------------------------------------------------------

def load_merchants(conn: psycopg.Connection) -> list[KnownMerchant]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.slug, m.name, m.canonical_domain,"
            " COALESCE(array_agg(DISTINCT a.provider_id)"
            "          FILTER (WHERE a.provider_id IS NOT NULL), '{}') AS provider_ids"
            " FROM merchant m LEFT JOIN merchant_alias a ON a.merchant_id = m.id"
            " GROUP BY m.id ORDER BY m.id"
        )
        return [
            KnownMerchant(r["id"], r["slug"], r["name"], r["canonical_domain"],
                          frozenset(r["provider_ids"]))
            for r in cur.fetchall()
        ]


def unmatched_aliases(conn: psycopg.Connection) -> list[dict]:
    """Alias non rattachés qui ont au moins un relevé : un marchand sans aucune
    offre n'a pas de page à publier, inutile de lui créer une fiche."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.provider_id, a.raw_slug, a.raw_name, a.raw_domain"
            " FROM merchant_alias a"
            " WHERE a.merchant_id IS NULL"
            "   AND EXISTS (SELECT 1 FROM offer_snapshot s WHERE s.merchant_alias_id = a.id)"
            " ORDER BY a.id"
        )
        return cur.fetchall()


def create_merchant(
    conn: psycopg.Connection, slug: str, name: str, domain: str | None
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO merchant (slug, name, canonical_domain) VALUES (%s, %s, %s)"
            " RETURNING id",
            (slug, name, domain),
        )
        return cur.fetchone()["id"]


def link_alias(
    conn: psycopg.Connection, alias_id: int, merchant_id: int, confidence: float
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE merchant_alias SET merchant_id = %s, confidence = %s WHERE id = %s",
            (merchant_id, confidence, alias_id),
        )


def queue_review_once(conn: psycopg.Connection, kind: str, payload: dict) -> bool:
    """Comme queue_review, mais n'ajoute rien si une demande ouverte existe déjà
    pour le même alias : relancer `match` ne doit pas empiler les doublons."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM review_queue WHERE kind = %s AND status = 'open'"
            " AND payload->>'alias_id' = %s",
            (kind, str(payload["alias_id"])),
        )
        if cur.fetchone():
            return False
    queue_review(conn, kind, payload)
    return True


# --- liste prioritaire -------------------------------------------------------

def seed_merchants(conn: psycopg.Connection, rows: list[dict]) -> dict[str, int]:
    """Importe la liste prioritaire dans `merchant`.

    Un marchand déjà créé par l'appariement est retrouvé par domaine, puis par
    slug, et complété (priorité, catégorie, volume, nom soigné) sans changer
    son slug. Les autres sont créés, non publiés : ils le seront à leur
    première offre. Rien n'est supprimé.
    """
    counts = {"created": 0, "updated": 0}
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                "SELECT id FROM ("
                "  SELECT id, 0 AS rank FROM merchant WHERE canonical_domain = %s"
                "  UNION ALL SELECT id, 1 FROM merchant WHERE slug = %s"
                ") found ORDER BY rank LIMIT 1",
                (row["domain"], row["slug"]),
            )
            found = cur.fetchone()
            if found:
                cur.execute(
                    "UPDATE merchant SET name = %s, category = %s, priority = %s,"
                    " search_volume = COALESCE(%s, search_volume),"
                    " canonical_domain = COALESCE(canonical_domain, %s)"
                    " WHERE id = %s",
                    (row["name"], row["category"], row["priority"], row["search_volume"],
                     row["domain"], found["id"]),
                )
                counts["updated"] += 1
            else:
                cur.execute(
                    "INSERT INTO merchant (slug, name, canonical_domain, category, priority,"
                    " search_volume) VALUES (%s, %s, %s, %s, %s, %s)",
                    (row["slug"], row["name"], row["domain"], row["category"],
                     row["priority"], row["search_volume"]),
                )
                counts["created"] += 1
    conn.commit()
    return counts


def load_targets(conn: psycopg.Connection, max_priority: str) -> list[Target]:
    """Marchands de priorité P1..max_priority (P2 inclut P1, etc.)."""
    allowed = list(PRIORITIES[: PRIORITIES.index(max_priority) + 1])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slug, name, priority FROM merchant WHERE priority = ANY(%s)"
            " ORDER BY priority, slug",
            (allowed,),
        )
        return [Target(r["slug"], r["name"], r["priority"]) for r in cur.fetchall()]


def aliases_of_targets(conn: psycopg.Connection, provider_id: int, max_priority: str) -> set[str]:
    """raw_slug des alias déjà rattachés à un marchand prioritaire."""
    allowed = list(PRIORITIES[: PRIORITIES.index(max_priority) + 1])
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.raw_slug FROM merchant_alias a JOIN merchant m ON m.id = a.merchant_id"
            " WHERE a.provider_id = %s AND m.priority = ANY(%s)",
            (provider_id, allowed),
        )
        return {r["raw_slug"] for r in cur.fetchall()}
