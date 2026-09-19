"""Tests de normalisation — écrits à partir des formulations réellement
observées sur Widilo le 19/09/2026."""
from decimal import Decimal

import pytest

from gcb.normalize import (
    classify, compute_effective_value, extract_amounts, parse_offer, to_decimal,
)


@pytest.mark.parametrize(
    "raw,expected",
    [("3,2", Decimal("3.2")), ("3.2", Decimal("3.2")), ("12", Decimal("12")), ("x", None)],
)
def test_to_decimal_accepte_la_virgule(raw, expected):
    assert to_decimal(raw) == expected


def test_extrait_un_pourcentage_a_virgule():
    assert extract_amounts("7,2% remboursés sur vos achats") == [(Decimal("7.2"), "percent")]


def test_extrait_une_paire_taux_barre_et_courant():
    # Formulation de la page de listing en campagne boostée.
    assert extract_amounts("2%6%cashback") == [
        (Decimal("2"), "percent"), (Decimal("6"), "percent")
    ]


def test_distingue_pourcentage_et_montant_fixe():
    assert extract_amounts("31€62€cashback") == [
        (Decimal("31"), "fixed_eur"), (Decimal("62"), "fixed_eur")
    ]


def test_une_prime_d_inscription_n_est_pas_un_taux():
    assert classify("+5€ à l'inscription") == "signup_bonus"


def test_un_bon_d_achat_n_est_pas_un_achat():
    assert classify("3,5% remboursés immédiatement sur les bons d'achat") == "giftcard"


def test_la_prime_ne_trie_pas():
    offer = parse_offer("+5€ à l'inscription")
    assert offer.kind == "signup_bonus"
    assert offer.effective_value is None


def test_le_bon_d_achat_ne_trie_pas():
    offer = parse_offer("3,5% remboursés immédiatement sur vos bons d'achat")
    assert offer.value == Decimal("3.5")
    assert offer.effective_value is None


def test_un_taux_d_achat_simple_alimente_le_tri():
    offer = parse_offer("7,2% remboursés sur vos achats")
    assert offer.value == Decimal("7.2")
    assert offer.unit == "percent"
    assert offer.effective_value == Decimal("7.2")


def test_paire_sans_indice_dom_le_plus_eleve_est_le_courant():
    offer = parse_offer("2%6%cashback")
    assert offer.value == Decimal("6")
    assert offer.value_base == Decimal("2")


def test_le_dom_prime_sur_la_deduction():
    # Quand l'adaptateur a su isoler le taux barré, on lui fait confiance,
    # même si le courant est plus bas que la base (fin de campagne).
    offer = parse_offer("12%1%", current="1%", base="12%")
    assert offer.value == Decimal("1")
    assert offer.value_base == Decimal("12")


def test_jusqu_a_est_marque_comme_plafond():
    offer = parse_offer("Jusqu'à 8% de cashback")
    assert offer.is_upto is True


def test_taux_categoriel_ne_trie_pas():
    offer = parse_offer("High-tech 2%", category_label="High-tech")
    assert offer.kind == "category"
    assert offer.effective_value is None


def test_offre_nouveau_client_ne_trie_pas():
    offer = parse_offer("10% pour les nouveaux clients")
    assert offer.is_new_customer_only is True
    assert offer.effective_value is None


def test_exclusions_detectees():
    offer = parse_offer("5% de cashback, hors soldes et hors marketplace")
    assert offer.is_sale_excluded is True
    assert offer.is_marketplace_excluded is True


def test_texte_sans_montant_ne_produit_rien():
    assert parse_offer("Cashback bientôt disponible") is None


def test_effective_value_ignore_l_unite_mais_pas_le_type():
    from gcb.types import RawOffer

    fixed = RawOffer(raw_text="20€", value=Decimal("20"), unit="fixed_eur")
    assert compute_effective_value(fixed) == Decimal("20")
