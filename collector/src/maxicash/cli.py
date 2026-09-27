"""CLI du collecteur.

    maxicash migrate                     applique les migrations
    maxicash discover widilo             énumère les marchands via le sitemap
    maxicash snapshot widilo fnac        fige une page réelle en fixture de test
    maxicash run widilo --limit 20       collecte et écrit en base (puis apparie)
    maxicash match                       rattache les alias à un marchand unique
    maxicash status                      dernières passes et fraîcheur
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from urllib.parse import urlsplit

from rich.console import Console
from rich.table import Table

from . import db, matching
from .adapters import get_adapter
from .config import FIXTURES_DIR, Settings
from .http import PoliteClient, RobotsDisallowed

console = Console()

# Au-delà de cette variation du nombre d'offres par rapport à la dernière passe
# réussie, on suspecte une casse de DOM plutôt qu'un mouvement de marché.
ANOMALY_THRESHOLD = 0.20


# Une base hors de la machine locale est, par construction, partagée : la
# branche `dev` de Neon sert à plusieurs postes. Appliquer des migrations
# dessus par réflexe est le genre d'accident qu'on ne voit qu'après coup.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def cmd_migrate(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    host = urlsplit(settings.database_url).hostname or "?"
    if host not in LOCAL_HOSTS and not args.yes:
        console.print(f"[yellow]Base distante :[/] {host}")
        if not sys.stdin.isatty():
            console.print("[red]Refus : base distante sans --yes hors mode interactif.[/]")
            return 1
        if input("Appliquer les migrations ? [oui/non] ").strip().lower() != "oui":
            console.print("[dim]Annulé.[/]")
            return 1
    with db.connect(settings) as conn:
        applied = db.migrate(conn)
    if applied:
        console.print(f"[green]Migrations appliquées :[/] {', '.join(applied)}")
    else:
        console.print("[dim]Schéma déjà à jour.[/]")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    with PoliteClient(settings) as client:
        adapter = get_adapter(args.provider, client.get)
        merchants = list(adapter.list_merchants())
    console.print(f"[green]{len(merchants)}[/] marchands trouvés chez {args.provider}.")
    for merchant in merchants[: args.limit]:
        console.print(f"  {merchant.raw_slug:<32} {merchant.raw_url}")
    if len(merchants) > args.limit:
        console.print(f"  [dim]… et {len(merchants) - args.limit} autres[/]")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Fige une page réelle en fixture. C'est ce qui transforme une casse de DOM
    en test rouge plutôt qu'en données silencieusement fausses."""
    settings = Settings.from_env()
    target = FIXTURES_DIR / args.provider
    target.mkdir(parents=True, exist_ok=True)
    with PoliteClient(settings, use_cache=False) as client:
        adapter = get_adapter(args.provider, client.get)
        url = f"{adapter.base_url}/code-promo/{args.slug}"
        try:
            html = client.get(url)
        except RobotsDisallowed as exc:
            console.print(f"[red]{exc}[/]")
            return 2
    path = target / f"{args.slug}.html"
    path.write_text(html, encoding="utf-8")
    console.print(f"[green]Fixture écrite :[/] {path} ({len(html):,} octets)")
    console.print("[dim]Ajoutez un test dans tests/test_<plateforme>.py, puis : pytest -q[/]")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    urls_fetched = offers_found = offers_written = errors = empty = 0

    with db.connect(settings) as conn, PoliteClient(settings) as client:
        adapter = get_adapter(args.provider, client.get)
        provider_id = db.upsert_provider(
            conn, adapter.slug, adapter.slug.capitalize(), adapter.base_url
        )
        run_id = db.start_run(conn, provider_id)
        previous = db.previous_offer_count(conn, provider_id)

        try:
            merchants = list(adapter.list_merchants())
        except Exception as exc:  # énumération cassée = passe inutilisable
            db.finish_run(
                conn, run_id, status="failed", urls_fetched=0, offers_found=0,
                offers_written=0, errors_count=1, notes=f"énumération : {exc}",
            )
            console.print(f"[red]Énumération impossible :[/] {exc}")
            return 1

        if args.limit:
            merchants = merchants[: args.limit]
        console.print(f"{len(merchants)} pages à lire chez {adapter.slug}…")

        for merchant in merchants:
            try:
                html = client.get(merchant.raw_url)
                urls_fetched += 1
                offers = adapter.parse_offer(merchant, html)
            except RobotsDisallowed:
                errors += 1
                continue
            except Exception as exc:
                errors += 1
                kind = "parse_error" if isinstance(exc, ValueError) else "fetch_error"
                db.queue_review(
                    conn, kind,
                    {"url": merchant.raw_url, "error": f"{type(exc).__name__}: {exc}"},
                )
                conn.commit()
                continue

            if not offers:
                # Zéro offre est un résultat valide (marchand sans cashback). Le
                # mettre en file de validation la noierait à chaque passe : on
                # le compte seulement. Une page illisible, elle, lève une erreur.
                empty += 1
                continue

            # Nom affiché et site du marchand, lus sur la page : meilleurs que
            # le slug du sitemap, et le domaine sert à l'appariement.
            details_of = getattr(adapter, "merchant_details", None)
            details = {k: v for k, v in (details_of(html) if details_of else {}).items() if v}
            if details:
                merchant = replace(merchant, **details)

            alias_id = db.upsert_alias(conn, provider_id, merchant)
            for offer in offers:
                offers_found += 1
                if db.record_offer(
                    conn, alias_id=alias_id, provider_id=provider_id,
                    run_id=run_id, offer=offer,
                ):
                    offers_written += 1
            conn.commit()

        status = "ok" if errors == 0 else ("partial" if offers_found else "failed")
        notes = f"{empty} marchands sans offre" if empty else None
        if previous and previous > 0:
            drift = abs(offers_found - previous) / previous
            if drift > ANOMALY_THRESHOLD:
                notes = (notes + " ; " if notes else "") + (
                    f"variation anormale : {offers_found} offres contre {previous} "
                    f"à la passe précédente ({drift:.0%}). Vérifier les sélecteurs."
                )
                console.print(f"[yellow]{notes}[/]")

        db.finish_run(
            conn, run_id, status=status, urls_fetched=urls_fetched,
            offers_found=offers_found, offers_written=offers_written,
            errors_count=errors, notes=notes,
        )
        # Rattache les nouveaux alias à un marchand unique : sans cela, la
        # comparaison entre plateformes reste vide.
        match_aliases(conn)

        try:
            db.refresh_current(conn)
        except Exception:
            pass  # la vue n'existe pas encore au tout premier run

    console.print(
        f"[green]Terminé.[/] {urls_fetched} pages, {offers_found} offres lues, "
        f"{offers_written} écrites (le reste inchangé), {empty} sans offre, {errors} erreurs."
    )
    return 0


def match_aliases(conn) -> dict[str, int]:
    """Rattache chaque alias non apparié à un marchand : par domaine, puis par
    nom ; crée le marchand s'il n'existe pas ; met le doute en validation."""
    merchants = db.load_merchants(conn)
    taken = {m.slug for m in merchants}
    counts = {"link": 0, "create": 0, "review": 0}

    for alias in db.unmatched_aliases(conn):
        decision = matching.decide(
            alias["provider_id"], alias["raw_name"], alias["raw_domain"], merchants
        )
        if decision.action == "review":
            if db.queue_review_once(conn, "merchant_match", {
                "alias_id": alias["id"], "raw_slug": alias["raw_slug"],
                "raw_name": alias["raw_name"], "raw_domain": alias["raw_domain"],
                "candidate_merchant_id": decision.merchant_id,
                "score": decision.confidence, "reason": decision.reason,
            }):
                counts["review"] += 1
            continue

        if decision.action == "create":
            slug = matching.unique_slug(alias["raw_name"], taken)
            domain = matching.domain_key(alias["raw_domain"])
            if domain and any(m.domain == domain for m in merchants):
                domain = None  # ne peut pas arriver après decide ; garde-fou UNIQUE
            merchant_id = db.create_merchant(conn, slug, alias["raw_name"], domain)
            taken.add(slug)
            merchants.append(matching.KnownMerchant(
                merchant_id, slug, alias["raw_name"], domain, frozenset({alias["provider_id"]})
            ))
            db.link_alias(conn, alias["id"], merchant_id, 1.0)
        else:
            merchant_id = decision.merchant_id
            db.link_alias(conn, alias["id"], merchant_id, decision.confidence or 0)
            merchants = [
                matching.KnownMerchant(m.id, m.slug, m.name, m.domain,
                                       m.provider_ids | {alias["provider_id"]})
                if m.id == merchant_id else m
                for m in merchants
            ]
        counts[decision.action] += 1

    conn.commit()
    console.print(
        f"Appariement : {counts['link']} rattachés, {counts['create']} marchands créés, "
        f"{counts['review']} en validation."
    )
    return counts


def cmd_match(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    with db.connect(settings) as conn:
        match_aliases(conn)
        try:
            db.refresh_current(conn)
        except Exception:
            pass
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    table = Table(title="Dernières passes")
    for column in ("Plateforme", "Statut", "Pages", "Lues", "Écrites", "Erreurs", "Fin"):
        table.add_column(column)
    with db.connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.slug, r.status, r.urls_fetched, r.offers_found,"
            " r.offers_written, r.errors_count, r.finished_at FROM scrape_run r"
            " JOIN provider p ON p.id = r.provider_id"
            " ORDER BY r.started_at DESC LIMIT 15"
        )
        for row in cur.fetchall():
            table.add_row(
                row["slug"], row["status"], str(row["urls_fetched"]),
                str(row["offers_found"]), str(row["offers_written"]),
                str(row["errors_count"]), str(row["finished_at"] or "—"),
            )
    console.print(table)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maxicash", description="Collecteur MAXICASH")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="applique les migrations")
    p.add_argument("--yes", action="store_true",
                   help="ne pas demander confirmation sur une base distante")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("discover", help="énumère les marchands d'une plateforme")
    p.add_argument("provider")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("snapshot", help="fige une page réelle en fixture")
    p.add_argument("provider")
    p.add_argument("slug")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("run", help="collecte et écrit en base")
    p.add_argument("provider")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_run)

    sub.add_parser("match", help="rattache les alias à un marchand unique").set_defaults(
        func=cmd_match)

    sub.add_parser("status", help="dernières passes").set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
