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
* Le taux s'affiche « 7,2% remboursés sur vos achats ». En campagne boostée,
  le listing montre deux valeurs accolées (« 2%6% ») : un taux barré suivi du
  taux courant. Certains marchands sont en montant fixe (« 31€62€ », Orange).
* Une prime d'inscription (« +5€ à l'inscription ») et un taux de bon d'achat
  (« 3,5% remboursés immédiatement ») cohabitent sur la même page. Les trois
  doivent être distingués, sans quoi le comparateur affiche n'importe quoi.

XPATHS ci-dessous est le SEUL endroit à ajuster quand le DOM change. Lancez
`maxicash snapshot widilo fnac` pour figer une page réelle en fixture, puis
`pytest tests/test_widilo.py` : le test reste rouge tant que les expressions
ne sont pas justes — c'est exactement ce qu'on veut, plutôt que des données
silencieusement fausses.
"""
from __future__ import annotations

import re
from typing import Iterator

from lxml import html as lxml_html

from ..normalize import parse_offer
from ..types import RawMerchant, RawOffer

BASE_URL = "https://www.widilo.fr"
SITEMAP_URL = f"{BASE_URL}/shop-sitemap.xml"
MERCHANT_PATH = "/code-promo/"


def _cls(*fragments: str) -> str:
    """Prédicat XPath « l'attribut class contient l'un de ces fragments »."""
    tests = " or ".join(f"contains(@class, '{f}')" for f in fragments)
    return f"[{tests}]"


# --- Le seul endroit à ajuster quand le DOM change ---------------------------
XPATHS = {
    # Bloc portant le taux d'achat courant.
    "rate_block": (
        "//*[@data-testid='cashback-rate']"
        f" | //*{_cls('cashback-rate')}"
        f" | //*{_cls('cashbackRate')}"
    ),
    # Taux barré (campagne boostée). La rature est la seule indication fiable
    # de savoir lequel des deux montants est l'ancien.
    "rate_base": (
        ".//del | .//s"
        f" | .//*{_cls('line-through', 'strikethrough', 'lineThrough')}"
    ),
    # Conditions et exclusions.
    "conditions": f"//*{_cls('condition', 'terms', 'exclusion')}",
    # Lignes d'un éventuel tableau de taux par catégorie.
    "category_rows": (
        f"//*{_cls('category-rates', 'categoryRates')}//tr"
        " | //*[@data-testid='category-rate']"
    ),
    # Bloc de prime d'inscription, s'il est isolé dans le DOM.
    "signup_block": f"//*{_cls('signup-bonus', 'signupBonus', 'welcome-bonus')}",
    # Bloc de taux sur bons d'achat.
    "giftcard_block": f"//*{_cls('giftcard', 'gift-card', 'bon-achat')}",
    "merchant_name": "//h1",
}

_SITEMAP_ENTRY = re.compile(r"<url>(?:(?!</url>).)*?</url>", re.IGNORECASE | re.DOTALL)
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)
_LASTMOD = re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>", re.IGNORECASE)


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.text_content()).strip()


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

    def parse_offer(self, merchant: RawMerchant, html: str) -> list[RawOffer]:
        tree = lxml_html.fromstring(html)
        offers: list[RawOffer] = []

        conditions_nodes = tree.xpath(XPATHS["conditions"])
        conditions = _text(conditions_nodes[0]) if conditions_nodes else None

        for block in tree.xpath(XPATHS["rate_block"])[:1]:
            full = _text(block)
            base_nodes = block.xpath(XPATHS["rate_base"])
            base_text = _text(base_nodes[0]) if base_nodes else None
            current = full.replace(base_text, " ", 1) if base_text else full
            offer = parse_offer(full, current=current, base=base_text,
                                conditions_text=conditions)
            if offer:
                offers.append(offer)

        offers.extend(self._parse_simple(tree, XPATHS["signup_block"], conditions))
        offers.extend(self._parse_simple(tree, XPATHS["giftcard_block"], conditions))
        offers.extend(self._parse_categories(tree, conditions))

        # Repli : aucune expression n'a mordu. On lit le titre, ce qui donne au
        # moins un signal — et l'appelant met la page en file de validation.
        if not offers:
            names = tree.xpath(XPATHS["merchant_name"])
            offer = parse_offer(_text(names[0]) if names else "", conditions_text=conditions)
            if offer:
                offers.append(offer)

        return offers

    @staticmethod
    def _parse_simple(tree, xpath: str, conditions: str | None) -> list[RawOffer]:
        out: list[RawOffer] = []
        for node in tree.xpath(xpath):
            offer = parse_offer(_text(node), conditions_text=conditions)
            if offer:
                out.append(offer)
        return out

    @staticmethod
    def _parse_categories(tree, conditions: str | None) -> list[RawOffer]:
        out: list[RawOffer] = []
        for node in tree.xpath(XPATHS["category_rows"]):
            text = _text(node)
            if not text or ("%" not in text and "€" not in text):
                continue
            label = re.split(r"\d", text, maxsplit=1)[0].strip(" :-") or None
            offer = parse_offer(text, category_label=label, conditions_text=conditions)
            if offer:
                out.append(offer)
        return out
