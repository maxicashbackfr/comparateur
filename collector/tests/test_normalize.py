"""Tests de normalisation — écrits à partir des formulations réellement
observées sur Widilo le 19/09/2026."""
from decimal import Decimal

import pytest

from maxicash.normalize import (
    classify, compute_effective_value, extract_amounts, parse_offer,
    same_offer, to_decimal,
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
    from maxicash.types import RawOffer

    fixed = RawOffer(raw_text="20€", value=Decimal("20"), unit="fixed_eur")
    assert compute_effective_value(fixed) == Decimal("20")


# --- Écriture au changement (migration 003) ----------------------------------

def _row(**kwargs):
    """Une ligne d'offer_snapshot telle que psycopg la rend."""
    base = {
        "value": Decimal("7.2"), "value_base": None, "unit": "percent",
        "kind": "purchase", "category_label": None, "conditions_text": None,
        "is_upto": False, "is_new_customer_only": False,
        "is_sale_excluded": None, "is_marketplace_excluded": None,
        "effective_value": Decimal("7.2"),
    }
    base.update(kwargs)
    return base


def test_premier_releve_est_toujours_ecrit():
    assert same_offer(None, parse_offer("7,2% remboursés")) is False


def test_taux_identique_n_est_pas_reecrit():
    assert same_offer(_row(), parse_offer("7,2% remboursés")) is True


def test_decimales_equivalentes_comptent_pour_identiques():
    # 7.20 et 7.2 sont le même taux : une différence de représentation ne doit
    # pas provoquer une écriture.
    assert same_offer(_row(value=Decimal("7.20")), parse_offer("7,2% remboursés")) is True


def test_changement_de_taux_declenche_une_ecriture():
    assert same_offer(_row(value=Decimal("5")), parse_offer("7,2% remboursés")) is False


def test_apparition_d_un_taux_barre_declenche_une_ecriture():
    # Passage en campagne boostée : la valeur courante ne bouge pas, mais
    # l'offre a bel et bien changé.
    offer = parse_offer("2% 7,2%", current="7,2%", base="2%")
    assert same_offer(_row(value_base=None), offer) is False


def test_changement_d_exclusion_declenche_une_ecriture():
    # Le pourcentage est identique, mais « hors soldes » vient d'apparaître :
    # la valeur réelle de l'offre a changé. C'est le cas que l'on raterait en
    # ne comparant que le nombre.
    offer = parse_offer("7,2% remboursés, hors soldes")
    assert same_offer(_row(), offer) is False


def test_conditions_vides_et_absentes_sont_equivalentes():
    assert same_offer(_row(conditions_text=""), parse_offer("7,2% remboursés")) is True
