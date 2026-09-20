"""Normalisation des taux — le cœur métier, et la partie la plus facile à rater.

Un taux affiché n'est pas une donnée : c'est une phrase en français qui mélange
pourcentages, montants fixes, primes d'inscription, taux catégoriels et bons
d'achat. Les confondre produit un comparateur faux, ce qui coûte la confiance
avant de coûter le trafic.

Règle retenue (§4 de la spéc) : seul le taux de base grand public, achat
classique, alimente `effective_value`. Tout le reste est conservé mais ne trie
pas.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .types import Kind, RawOffer, Unit

# Un nombre français ou anglais suivi de % ou €. Le groupe d'unité est capturé
# pour que « 5€ » et « 5% » ne soient jamais confondus.
_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,4}(?:[.,]\d{1,2})?)\s*(%|€|euros?\b)",
    re.IGNORECASE,
)

_UPTO = ("jusqu'a", "jusqu a", "jusqu’a", "up to", "max ")
_SIGNUP = (
    "a l'inscription", "a l inscription", "a l’inscription",
    "offert a l'inscription", "bonus de bienvenue", "prime de bienvenue",
    "pour votre inscription", "en vous inscrivant", "de bienvenue",
)
_GIFTCARD = ("bon d'achat", "bon d achat", "bons d'achat", "carte cadeau", "e-carte", "gift card")
_NEW_CUSTOMER = ("nouveau client", "nouveaux clients", "premiere commande", "1re commande")
_SALE_EXCLUDED = ("hors soldes", "soldes exclues", "hors promotions", "hors promo")
_MARKETPLACE_EXCLUDED = ("hors marketplace", "marketplace exclue", "hors place de marche")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _fold(text: str) -> str:
    """Minuscules, sans accents, espaces normalisés — pour les tests de mots-clés."""
    return re.sub(r"\s+", " ", strip_accents(text).lower()).strip()


def to_decimal(raw: str) -> Decimal | None:
    """« 3,2 » et « 3.2 » donnent tous deux Decimal('3.2')."""
    try:
        return Decimal(raw.replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return None


def classify(text: str) -> Kind:
    """Ce que le taux récompense. L'ordre compte : une prime d'inscription
    mentionnée dans une phrase parlant de bons d'achat reste une prime."""
    folded = _fold(text)
    if any(k in folded for k in _SIGNUP):
        return "signup_bonus"
    if any(k in folded for k in _GIFTCARD):
        return "giftcard"
    return "purchase"


def extract_amounts(text: str) -> list[tuple[Decimal, Unit]]:
    """Tous les montants d'une chaîne, dans l'ordre d'apparition.

    « 2%6% » rend [(2, percent), (6, percent)] — la page affiche un taux barré
    suivi du taux courant. Lequel est lequel ne se devine pas depuis le texte :
    c'est le DOM qui le dit (voir `widilo.SELECTORS`).
    """
    out: list[tuple[Decimal, Unit]] = []
    for raw, unit_token in _AMOUNT_RE.findall(text):
        value = to_decimal(raw)
        if value is None:
            continue
        unit: Unit = "percent" if unit_token == "%" else "fixed_eur"
        out.append((value, unit))
    return out


def parse_offer(
    raw_text: str,
    *,
    current: str | None = None,
    base: str | None = None,
    category_label: str | None = None,
    conditions_text: str | None = None,
) -> RawOffer | None:
    """Construit une offre à partir du texte lu.

    `current` et `base` sont les fragments que l'adaptateur a su distinguer dans
    le DOM (taux courant / taux barré). Quand ils sont absents, on retombe sur
    `raw_text` et on refuse de deviner l'ordre : un seul montant est retenu, et
    s'il y en a deux le plus élevé devient le courant, l'autre la base — ce qui
    correspond au cas « campagne boostée », le seul où deux valeurs coexistent.
    """
    context = " ".join(filter(None, (raw_text, category_label, conditions_text)))
    folded = _fold(context)
    kind = classify(context)

    if current is not None:
        amounts = extract_amounts(current)
        base_amounts = extract_amounts(base) if base else []
    else:
        amounts = extract_amounts(raw_text)
        base_amounts = []
        if len(amounts) >= 2 and amounts[0][1] == amounts[1][1]:
            pair = sorted(amounts[:2], key=lambda a: a[0])
            base_amounts, amounts = [pair[0]], [pair[1]]

    if not amounts:
        return None

    value, unit = amounts[0]
    value_base = base_amounts[0][0] if base_amounts else None

    offer = RawOffer(
        raw_text=raw_text.strip(),
        value=value,
        value_base=value_base,
        unit=unit,
        kind="category" if category_label else kind,
        category_label=category_label,
        conditions_text=conditions_text,
        is_upto=any(k in folded for k in _UPTO),
        is_new_customer_only=any(k in folded for k in _NEW_CUSTOMER),
        is_sale_excluded=True if any(k in folded for k in _SALE_EXCLUDED) else None,
        is_marketplace_excluded=(
            True if any(k in folded for k in _MARKETPLACE_EXCLUDED) else None
        ),
    )
    offer.effective_value = compute_effective_value(offer)
    return offer


def compute_effective_value(offer: RawOffer) -> Decimal | None:
    """La colonne de tri. Renvoie None dès qu'une comparaison serait trompeuse.

    Sont écartés : les primes d'inscription (versées une fois, pas un taux),
    les bons d'achat (autre produit), les taux catégoriels (dépendent du panier)
    et les offres réservées aux nouveaux clients (l'audience d'un comparateur
    cashback est déjà inscrite ailleurs — §11 de la spéc).

    Les montants fixes gardent leur valeur mais ne sont pas comparables à un
    pourcentage : le tri doit se faire par unité, jamais entre unités.
    """
    if offer.kind != "purchase":
        return None
    if offer.category_label is not None:
        return None
    if offer.is_new_customer_only:
        return None
    return offer.value
