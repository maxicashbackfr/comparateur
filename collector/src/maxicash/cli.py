"""CLI du collecteur.

    maxicash migrate                     applique les migrations
    maxicash discover widilo             énumère les marchands via le sitemap
    maxicash snapshot widilo fnac        fige une page réelle en fixture de test
    maxicash snapshot igraal URL…        idem par URL, même sans adaptateur
    maxicash run widilo --limit 20       collecte et écrit en base (puis apparie)
    maxicash match                       rattache les alias à un marchand unique
    maxicash seed marchands.csv          importe la liste prioritaire
    maxicash run ebuyclub --priority P1  ne collecte que la liste prioritaire
    maxicash status                      dernières passes et fraîcheur
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
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


# Gabarit d'URL des pages marchands, par plateforme. Sert à `snapshot`, qui
# doit pouvoir figer des pages AVANT que l'adaptateur existe : c'est avec elles
# qu'on l'écrit.
PAGE_URLS = {
    "widilo": "https://www.widilo.fr/code-promo/{slug}",
    "igraal": "https://fr.igraal.com/codes-promo/{slug}",
    "ebuyclub": "https://www.ebuyclub.com/reduction-{slug}",   # slug = <nom>-<id>
}


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Fige des pages réelles en fixtures. C'est ce qui transforme une casse de
    structure en test rouge plutôt qu'en données silencieusement fausses.

    Accepte des slugs (« fnac ») ou des URL complètes copiées du navigateur."""
    settings = Settings.from_env()
    target = FIXTURES_DIR / args.provider
    target.mkdir(parents=True, exist_ok=True)
    template = PAGE_URLS.get(args.provider)
    status = 0
    with PoliteClient(settings, use_cache=False) as client:
        for item in args.pages:
            if item.startswith(("http://", "https://")):
                url = item
                slug = urlsplit(item).path.rstrip("/").rsplit("/", 1)[-1] or "page"
            elif template:
                url, slug = template.format(slug=item), item
            else:
                console.print(f"[red]Plateforme inconnue : {args.provider}. "
                              "Donnez l'URL complète de la page.[/]")
                return 2
            try:
                html = client.get(url)
            except RobotsDisallowed as exc:
                console.print(f"[red]{exc}[/]")
                status = 2
                continue
            except Exception as exc:
                console.print(f"[red]{url} : {exc}[/]")
                status = 1
                continue
            path = target / f"{slug}.html"
            path.write_text(html, encoding="utf-8")
            console.print(f"[green]Fixture écrite :[/] {path} ({len(html):,} octets)")
    return status


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    urls_fetched = offers_found = offers_written = errors = empty = 0

    with db.connect(settings) as conn, PoliteClient(settings) as client:
        adapter = get_adapter(args.provider, client.get)
        provider_id = db.upsert_provider(
            conn, adapter.slug, getattr(adapter, "display_name", adapter.slug.capitalize()),
            adapter.base_url,
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

        if args.priority:
            targets = db.load_targets(conn, args.priority)
            if not targets:
                console.print("[red]Aucun marchand prioritaire en base : lancez d'abord "
                              "`maxicash seed <fichier.csv>`.[/]")
                db.finish_run(conn, run_id, status="failed", urls_fetched=0, offers_found=0,
                              offers_written=0, errors_count=1, notes="liste prioritaire vide")
                return 1
            linked = db.aliases_of_targets(conn, provider_id, args.priority)
            total = len(merchants)
            merchants = [
                m for m in merchants
                if m.raw_slug in linked or matching.wanted(m.raw_name, m.raw_slug, targets)
            ]
            console.print(
                f"Liste prioritaire ≤ {args.priority} : {len(merchants)} pages retenues "
                f"sur {total} ({len(targets)} marchands visés)."
            )

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
                # S'il avait des offres à la passe précédente, elles ont pris fin.
                known_alias = db.find_alias(conn, provider_id, merchant.raw_slug)
                if known_alias:
                    db.touch_alias(conn, known_alias)
                    offers_written += db.close_missing_offers(
                        conn, alias_id=known_alias, provider_id=provider_id,
                        run_id=run_id, seen=set(),
                    )
                    conn.commit()
                continue

            # Nom affiché et site du marchand, lus sur la page : meilleurs que
            # le slug du sitemap, et le domaine sert à l'appariement.
            details_of = getattr(adapter, "merchant_details", None)
            details = {
                k: v for k, v in (details_of(merchant, html) if details_of else {}).items() if v
            }
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
            offers_written += db.close_missing_offers(
                conn, alias_id=alias_id, provider_id=provider_id, run_id=run_id,
                seen={(o.kind, o.category_label) for o in offers},
            )
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
        refresh_and_publish(conn)

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
        refresh_and_publish(conn)
    return 0


def refresh_and_publish(conn) -> None:
    """Rafraîchit les vues d'état courant, puis publie les marchands qui ont
    au moins une offre en cours. Ne fait jamais l'inverse."""
    db.refresh_current(conn)
    published = db.publish_merchants_with_offers(conn)
    if published:
        console.print(f"Publication : {published} marchands publiés.")


def cmd_seed(args: argparse.Namespace) -> int:
    """Importe la liste prioritaire exportée en CSV depuis Google Sheets."""
    rows = matching.parse_seed_csv(Path(args.csv_path).read_text(encoding="utf-8"))
    if not rows:
        console.print("[red]Aucune ligne lisible (colonnes attendues : Slug, Marchand, "
                      "Domaine, Categorie, Prio).[/]")
        return 1
    settings = Settings.from_env()
    with db.connect(settings) as conn:
        counts = db.seed_merchants(conn, rows)
    by_prio = {p: sum(r["priority"] == p for r in rows) for p in matching.PRIORITIES}
    console.print(
        f"[green]Liste importée :[/] {counts['created']} marchands créés, "
        f"{counts['updated']} complétés ({by_prio['P1']} P1, {by_prio['P2']} P2, "
        f"{by_prio['P3']} P3)."
    )
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

    p = sub.add_parser("snapshot", help="fige des pages réelles en fixtures")
    p.add_argument("provider")
    p.add_argument("pages", nargs="+", help="slugs ou URL complètes")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("run", help="collecte et écrit en base")
    p.add_argument("provider")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--priority", choices=matching.PRIORITIES,
                   help="ne collecter que la liste prioritaire, jusqu'à ce niveau inclus")

    p = sub.add_parser("seed", help="importe la liste prioritaire (CSV)")
    p.add_argument("csv_path")
    p.set_defaults(func=cmd_seed)
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
