"""Adaptateur Widilo.

Ce que l'inspection du site a établi (19/09/2026) :

* robots.txt autorise « / » et publie un sitemap index. Il INTERDIT /out/,
  /redirect/ et /l/ — c'est-à-dire précisément les chemins de tracking. La
  résolution d'un lien sortant pour obtenir le domaine canonique est donc
  exclue sur cette plateforme : ni autorisée, ni souhaitable (chaque
  résolution génère un clic dans les statistiques du réseau d'affiliation).
* shop-sitemap.xml énumère ~800-900 pages marchands, avec <lastmod>. C'est la
  source d'énumération : autorisée, stable, et qui évite de crawler des pages
  de listing paginées.
* Une page marchand vit sur /code-promo/<slug>.

Ce que les pages réelles ont montré (26/09/2026, 5 fixtures) :

* Widilo est une application Angular rendue côté serveur. Toutes les données
  de la page sont sérialisées dans <script id="ng-state" type="application/json">.
  On lit ce JSON, pas le DOM : les champs y sont typés (cashbackRate: 6.0,
  cashbackType: 1 = %, 2 = €, cashbackBeforeIncreaseValue: "3%" en boost…) et
  ne dépendent ni des classes CSS ni de la mise en page.
* Les clés de premier niveau de ng-state sont des hachages qui changent d'une
  page à l'autre. L'objet marchand est repéré par son contenu (présence de
  metaTitle, routeName et cashbackRate), jamais par sa clé.
* La prime d'inscription (« 5€ ») et le seuil de paiement (« 20€ ») sont des
  données de la PLATEFORME, pas du marchand : elles vivent dans une autre entrée
  de ng-state et relèvent de la table provider, pas d'offer_snapshot.

Si Widilo change de structure, `find_shop_payload` lève WidiloParseError :
une page devient une erreur visible (review_queue), jamais un taux inventé.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Iterator

from ..normalize import (
    _MARKETPLACE_EXCLUDED, _NEW_CUSTOMER, _SALE_EXCLUDED, _fold,
    compute_effective_value, extract_amounts,
)
from ..types import RawMerchant, RawOffer, Unit

BASE_URL = "https://www.widilo.fr"
SITEMAP_URL = f"{BASE_URL}/shop-sitemap.xml"
MERCHANT_PATH = "/code-promo/"

_SITEMAP_ENTRY = re.compile(r"<url>(?:(?!</url>).)*?</url>", re.IGNORECASE | re.DOTALL)
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_LASTMOD = re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>", re.IGNORECASE)
_NG_STATE = re.compile(r'<script[^>]+id="ng-state"[^>]*>(.*?)</script>', re.DOTALL)

# cashbackType côté Widilo
_UNITS: dict[int, Unit] = {1: "percent", 2: "fixed_eur"}
# « Marketplace, Livre : 0% » dans le détail des taux vaut exclusion.
_MARKETPLACE_TIER = re.compile(r"marketplace", re.IGNORECASE)


class WidiloParseError(ValueError):
    """La page ne contient pas les données attendues : structure changée."""


# --- lecture de ng-state -----------------------------------------------------

def extract_ng_state(html: str) -> dict[str, Any]:
    match = _NG_STATE.search(html)
    if not match:
        raise WidiloParseError("bloc ng-state introuvable")
    return json.loads(match.group(1))


def find_shop_payload(state: dict[str, Any]) -> dict[str, Any]:
    required = {"metaTitle", "routeName", "cashbackRate"}
    for key, entry in state.items():
        if key == "__nghData__" or not isinstance(entry, dict):
            continue
        body = entry.get("b")
        if not isinstance(body, str) or '"cashbackRate"' not in body:
            continue
        data = json.loads(body)
        if isinstance(data, dict) and required <= data.keys():
            return data
    raise WidiloParseError("objet marchand introuvable dans ng-state")


def _amount(raw: str | None) -> tuple[Decimal, Unit] | None:
    """« 4,5% » -> (4.5, percent). Réutilise l'extracteur commun."""
    found = extract_amounts(raw or "")
    return found[0] if len(found) == 1 else None


def _has(text: str, keywords: tuple[str, ...]) -> bool:
    folded = _fold(text)
    return any(k in folded for k in keywords)


def _tiers(data: dict[str, Any]) -> list[tuple[str, Decimal, Unit, str]]:
    """Détail des taux : (libellé, valeur, unité, texte brut)."""
    out = []
    for item in (data.get("cashbackDescription") or {}).get("dynamic") or []:
        parsed = _amount(item.get("value"))
        if parsed is None:
            continue
        label = " — ".join(
            s.strip() for s in item.get("descriptions") or [] if s and s.strip()
        )
        out.append((label, parsed[0], parsed[1], f"{item['value']} {label}".strip()))
    return out


def _conditions(data: dict[str, Any]) -> str | None:
    desc = data.get("cashbackDescription") or {}
    notes = [s["description"].strip() for s in desc.get("static") or [] if s.get("description")]
    rules = [
        c["value"].strip()
        for c in (data.get("cashbackConditions") or {}).get("conditionList") or []
        if c.get("value")
    ]
    return "\n".join(notes + rules) or None


class WidiloAdapter:
    slug = "widilo"
    base_url = BASE_URL

    def __init__(self, fetch) -> None:
        """`fetch` est un callable url -> texte. L'injecter permet de tester
        l'adaptateur sur fixtures, sans réseau."""
        self._fetch = fetch

    # -- énumération ----------------------------------------------------------

    def list_merchants(self) -> Iterator[RawMerchant]:
        yield from self.parse_sitemap(self._fetch(SITEMAP_URL))

    @staticmethod
    def parse_sitemap(xml: str) -> Iterator[RawMerchant]:
        for entry in _SITEMAP_ENTRY.findall(xml):
            loc = _SITEMAP_LOC.search(entry)
            if not loc:
                continue
            url = loc.group(1)
            if MERCHANT_PATH not in url:
                continue
            raw_slug = url.rstrip("/").rsplit("/", 1)[-1]
            if not raw_slug:
                continue
            lastmod = _LASTMOD.search(entry)
            yield RawMerchant(
                raw_slug=raw_slug,
                raw_name=raw_slug.replace("-", " ").title(),
                raw_url=url,
                lastmod=lastmod.group(1) if lastmod else None,
            )

    # -- lecture d'une page ---------------------------------------------------

    @staticmethod
    def merchant_name(html: str) -> str | None:
        """Nom affiché par Widilo (« New Balance »), plus fiable que le slug."""
        try:
            return find_shop_payload(extract_ng_state(html)).get("name")
        except WidiloParseError:
            return None

    def parse_offer(self, merchant: RawMerchant, html: str) -> list[RawOffer]:
        data = find_shop_payload(extract_ng_state(html))

        if not data.get("isCashback"):
            return []  # marchand référencé sans cashback : zéro offre est valide

        unit = _UNITS.get(data.get("cashbackType"))
        if unit is None:
            raise WidiloParseError(f"cashbackType inconnu : {data.get('cashbackType')!r}")

        value = Decimal(str(data["cashbackRate"]))
        base = _amount(data.get("cashbackBeforeIncreaseValue")) if data.get(
            "cashbackIsIncrease") else None
        conditions = _conditions(data)
        tiers = _tiers(data)

        # Le taux affiché correspond à l'une des lignes de détail ; c'est elle
        # qui dit s'il est réservé aux nouveaux clients (Audible, Recyclivre).
        main_label = next((t[0] for t in tiers if t[1] == value and t[2] == unit), "")
        distinct = {(t[1], t[2]) for t in tiers}
        marketplace_tier = any(t[1] == 0 and _MARKETPLACE_TIER.search(t[0]) for t in tiers)

        purchase = RawOffer(
            raw_text=json.dumps({
                "cashbackValue": data.get("cashbackValue"),
                "cashbackBeforeIncreaseValue": data.get("cashbackBeforeIncreaseValue"),
                "label": main_label or None,
            }, ensure_ascii=False),
            value=value,
            value_base=base[0] if base and base[1] == unit else None,
            unit=unit,
            kind="purchase",
            conditions_text=conditions,
            # Plusieurs taux : celui affiché est un maximum.
            is_upto=len(distinct) > 1,
            is_new_customer_only=_has(main_label, _NEW_CUSTOMER),
            is_sale_excluded=True if _has(conditions or "", _SALE_EXCLUDED) else None,
            is_marketplace_excluded=(
                True if marketplace_tier or _has(conditions or "", _MARKETPLACE_EXCLUDED)
                else None
            ),
        )
        purchase.effective_value = compute_effective_value(purchase)
        offers = [purchase]

        # Détail par catégorie, seulement s'il apporte quelque chose : une ligne
        # unique répète le taux principal.
        if len(distinct) > 1:
            for label, tier_value, tier_unit, raw in tiers:
                offers.append(RawOffer(
                    raw_text=raw,
                    value=tier_value,
                    unit=tier_unit,
                    kind="category",
                    category_label=label or None,
                    is_new_customer_only=_has(label, _NEW_CUSTOMER),
                ))

        # Bon d'achat Widilo : autre produit, conservé mais jamais trié.
        if data.get("isVoucher") and data.get("voucherCashbackRate"):
            lo, hi = data.get("freeAmountMinValue"), data.get("freeAmountMaxValue")
            offers.append(RawOffer(
                raw_text=str(data.get("voucherCashbackValue") or data["voucherCashbackRate"]),
                value=Decimal(str(data["voucherCashbackRate"])),
                unit="percent",
                kind="giftcard",
                conditions_text=(
                    f"Bon d'achat de {lo:g} à {hi:g} €" if lo is not None and hi is not None
                    else None
                ),
            ))
        return offers
