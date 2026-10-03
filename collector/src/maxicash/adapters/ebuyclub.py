"""Adaptateur eBuyClub.

Ce que l'inspection du site a établi (03/10/2026, pages figées par le
workflow Snapshot) :

* robots.txt autorise les pages marchands et interdit notamment /ajax/,
  /service/, /bonus et les paramètres d'affiliation (?affilie=, ?aftb=).
  Aucun lien sortant n'est suivi.
* sitemap-reduction.xml énumère ~3 200 pages marchands
  « /reduction-<slug>-<id> ». L'identifiant numérique final est la clé
  stable : c'est lui qui désigne le marchand dans les données de la page.
* Le site est en Next.js (App Router). Les données de la page sont
  sérialisées dans les appels `self.__next_f.push([1, "…"])` (charge utile
  RSC). L'objet marchand y figure en JSON : {"id": 58, "name": "Fnac",
  "active": true, "channel": {"online": {...}, "ebon": {...}}, ...} ; le détail
  des taux est une liste de phrases sous la clé "discounts".
* channel.online : cashback en ligne — amount, currency (« % » ou « € »),
  insteadAmount (taux d'avant boost, « 0% » s'il n'y en a pas).
  channel.ebon : cashback sur bon d'achat. channel.alo (cashback « connecté »)
  et channel.offline (en magasin) sont hors périmètre du MVP.
* La page n'indique PAS le site du marchand : l'appariement se fait par nom.
* Plusieurs objets marchands coexistent sur une page (marchands similaires,
  encarts) : on retient celui dont l'id est celui de l'URL.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Iterator

from ..normalize import (
    _MARKETPLACE_EXCLUDED, _NEW_CUSTOMER, _fold, compute_effective_value, to_decimal,
)
from ..types import RawMerchant, RawOffer, Unit

BASE_URL = "https://www.ebuyclub.com"
SITEMAP_URL = f"{BASE_URL}/sitemap-reduction.xml"

_SITEMAP_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_MERCHANT_URL = re.compile(r"/reduction-(.+)-(\d+)/?$")
_RSC_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_CHANNEL = re.compile(r'"channel":\{')
# « 2,5% remboursés au lieu de 0,8% sur l'informatique pour… »
_TIER = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(%|€)\s*(?:rembours\w*)?\s*"
    r"(?:au lieu de\s*\d+(?:[.,]\d+)?\s*(?:%|€))?\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_UNITS: dict[str, Unit] = {"%": "percent", "€": "fixed_eur"}
_DECODER = json.JSONDecoder()


class EbuyclubParseError(ValueError):
    """La page ne contient pas les données attendues : structure changée."""


# --- lecture de la charge utile RSC -------------------------------------------

def rsc_payload(html: str) -> str:
    chunks = [json.loads(m.group(1)) for m in _RSC_CHUNK.finditer(html)]
    if not chunks:
        raise EbuyclubParseError("charge utile Next.js (__next_f) introuvable")
    return "".join(chunks)


def merchant_id_from_url(url: str) -> int:
    match = _MERCHANT_URL.search(url)
    if not match:
        raise EbuyclubParseError(f"URL marchand inattendue : {url}")
    return int(match.group(2))


def find_shop(payload: str, shop_id: int) -> dict[str, Any]:
    """Objet {"id": shop_id, ..., "channel": {...}} de la charge utile."""
    for match in _CHANNEL.finditer(payload):
        pos = match.start()
        start = payload.rfind('{"id":', max(0, pos - 30000), pos)
        while start != -1:
            try:
                obj, end = _DECODER.raw_decode(payload, start)
            except ValueError:
                obj, end = None, -1
            if end > pos and isinstance(obj, dict):
                if obj.get("id") == shop_id:
                    return obj
                break  # objet d'un autre marchand : passer au "channel" suivant
            start = payload.rfind('{"id":', max(0, pos - 30000), start)
    raise EbuyclubParseError(f"objet marchand {shop_id} introuvable")


def find_discounts(payload: str) -> list[str]:
    pos = payload.find('"discounts":')
    if pos == -1:
        return []
    try:
        value, _ = _DECODER.raw_decode(payload, pos + len('"discounts":'))
    except ValueError:
        return []
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def _channel_amount(channel: Any) -> tuple[Decimal, Unit] | None:
    if not isinstance(channel, dict):
        return None
    unit = _UNITS.get(channel.get("currency"))
    amount = channel.get("amount")
    if unit is None or not amount:
        return None
    return Decimal(str(amount)), unit


def _parse_tier(text: str) -> tuple[str, Decimal, Unit] | None:
    match = _TIER.match(text)
    if not match:
        return None
    value = to_decimal(match.group(1))
    if value is None:
        return None
    label = re.sub(r"\s+", " ", match.group(3)).strip(" .")
    return label, value, _UNITS[match.group(2)]


def _has(text: str, keywords: tuple[str, ...]) -> bool:
    folded = _fold(text)
    return any(k in folded for k in keywords)


class EbuyclubAdapter:
    slug = "ebuyclub"
    display_name = "eBuyClub"
    base_url = BASE_URL

    def __init__(self, fetch) -> None:
        self._fetch = fetch

    # -- énumération ----------------------------------------------------------

    def list_merchants(self) -> Iterator[RawMerchant]:
        yield from self.parse_sitemap(self._fetch(SITEMAP_URL))

    @staticmethod
    def parse_sitemap(xml: str) -> Iterator[RawMerchant]:
        for loc in _SITEMAP_LOC.findall(xml):
            match = _MERCHANT_URL.search(loc)
            if not match:
                continue
            slug, shop_id = match.groups()
            yield RawMerchant(
                raw_slug=f"{slug}-{shop_id}",
                raw_name=slug.replace("-", " ").title(),
                raw_url=loc,
            )

    # -- lecture d'une page ---------------------------------------------------

    def _shop(self, merchant_url: str, html: str) -> tuple[dict[str, Any], str]:
        payload = rsc_payload(html)
        return find_shop(payload, merchant_id_from_url(merchant_url)), payload

    def merchant_details(self, merchant: RawMerchant, html: str) -> dict[str, str | None]:
        """Nom affiché par eBuyClub. Le site du marchand n'est pas sur la page."""
        try:
            shop, _ = self._shop(merchant.raw_url, html)
        except EbuyclubParseError:
            return {}
        return {"raw_name": shop.get("name")}

    def parse_offer(self, merchant: RawMerchant, html: str) -> list[RawOffer]:
        shop, payload = self._shop(merchant.raw_url, html)
        if not shop.get("active", True):
            return []
        channel = shop.get("channel") or {}
        offers = self._purchase_offers(channel.get("online"), find_discounts(payload))
        giftcard = _channel_amount(channel.get("ebon"))
        if giftcard:
            offers.append(RawOffer(
                raw_text=str(channel["ebon"].get("discount") or giftcard[0]),
                value=giftcard[0], unit=giftcard[1], kind="giftcard",
            ))
        return offers

    @staticmethod
    def _purchase_offers(online: Any, discounts: list[str]) -> list[RawOffer]:
        main = _channel_amount(online)
        if main is None:
            return []  # pas de cashback en ligne (Booking.com au 03/10/2026)
        value, unit = main

        base = None
        if online.get("instead"):
            parsed = _parse_tier(str(online.get("insteadAmount") or ""))
            if parsed and parsed[1] > 0 and parsed[2] == unit:
                base = parsed[1]

        tiers = [t for t in (_parse_tier(d) for d in discounts) if t]
        distinct = {(t[1], t[2]) for t in tiers}
        main_label = next((t[0] for t in tiers if t[1] == value and t[2] == unit), "")
        marketplace = any(
            (t[1] == 0 and "marketplace" in _fold(t[0])) or _has(t[0], _MARKETPLACE_EXCLUDED)
            for t in tiers
        )

        purchase = RawOffer(
            raw_text=json.dumps({
                "discount": online.get("discount"),
                "insteadAmount": online.get("insteadAmount"),
                "label": main_label or None,
            }, ensure_ascii=False),
            value=value,
            value_base=base,
            unit=unit,
            kind="purchase",
            is_upto=len(distinct) > 1,
            is_new_customer_only=_has(main_label, _NEW_CUSTOMER),
            is_marketplace_excluded=True if marketplace else None,
        )
        purchase.effective_value = compute_effective_value(purchase)
        offers = [purchase]

        if len(distinct) > 1:
            for label, tier_value, tier_unit in tiers:
                offers.append(RawOffer(
                    raw_text=next(d for d in discounts if _parse_tier(d) == (
                        label, tier_value, tier_unit)).strip(),
                    value=tier_value, unit=tier_unit, kind="category",
                    category_label=label or None,
                    is_new_customer_only=_has(label, _NEW_CUSTOMER),
                ))
        return offers
