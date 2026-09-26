"""Types partagés entre adaptateurs, normalisation et persistance."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, Iterator

Unit = Literal["percent", "fixed_eur"]
Kind = Literal["purchase", "signup_bonus", "giftcard", "category", "unknown"]


@dataclass(frozen=True)
class RawMerchant:
    """Un marchand tel que la plateforme le désigne, avant appariement."""
    raw_slug: str
    raw_name: str
    raw_url: str
    lastmod: str | None = None


@dataclass
class RawOffer:
    """Un taux relevé sur une page. `raw_text` est ce qui a été lu, verbatim :
    c'est la pièce à conviction quand un chiffre est contesté."""
    raw_text: str
    value: Decimal | None = None
    value_base: Decimal | None = None
    unit: Unit = "percent"
    kind: Kind = "purchase"
    category_label: str | None = None
    conditions_text: str | None = None
    is_upto: bool = False
    is_new_customer_only: bool = False
    is_sale_excluded: bool | None = None
    is_marketplace_excluded: bool | None = None
    effective_value: Decimal | None = None


class ProviderAdapter(Protocol):
    """Contrat que tout adaptateur de plateforme respecte.

    `list_merchants` énumère — de préférence via le sitemap, qui est la source
    autorisée et stable. `parse_offer` lit une page et rend zéro ou plusieurs
    offres : zéro est un résultat valide (marchand référencé sans taux actif).
    """

    slug: str
    base_url: str

    def list_merchants(self) -> Iterator[RawMerchant]: ...

    def parse_offer(self, merchant: RawMerchant, html: str) -> list[RawOffer]: ...
