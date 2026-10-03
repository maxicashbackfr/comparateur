"""Tests de l'adaptateur eBuyClub, sur pages réelles figées le 03/10/2026
par le workflow Snapshot.

  1001-bijoux  cas simple, aussi présent chez Widilo (appariement)
  audible      montant fixe, nouveaux clients seulement
  booking      marchand référencé sans cashback en ligne : zéro offre
  darty        boost (4,1% -> 4,5%), 12 taux par catégorie, bon d'achat ;
               la page contient aussi l'objet d'un autre marchand (NordVPN)
  fnac         boost, 13 catégories, bon d'achat
  new-balance  cas simple, aussi présent chez Widilo
  orange       montants fixes par offre
  recyclivre   nouveaux / anciens clients, bon d'achat
"""
from decimal import Decimal
from pathlib import Path

import pytest

from maxicash.adapters.ebuyclub import EbuyclubAdapter, EbuyclubParseError
from maxicash.types import RawMerchant

FIXTURES = Path(__file__).parent / "fixtures" / "ebuyclub"
REAL = sorted(p for p in FIXTURES.glob("*.html") if not p.name.startswith("_"))


def _merchant(stem: str) -> RawMerchant:
    return RawMerchant(stem, stem, f"https://www.ebuyclub.com/reduction-{stem}")


def _offers(stem: str):
    html = (FIXTURES / f"{stem}.html").read_text(encoding="utf-8")
    return EbuyclubAdapter(fetch=lambda url: "").parse_offer(_merchant(stem), html)


def _purchase(offers):
    main = [o for o in offers if o.kind == "purchase"]
    assert len(main) == 1
    return main[0]


def test_sitemap_garde_les_pages_marchands_avec_leur_identifiant():
    xml = (FIXTURES / "_sitemap.xml").read_text(encoding="utf-8")
    merchants = list(EbuyclubAdapter.parse_sitemap(xml))
    assert merchants[0].raw_slug == "1001-bijoux-2837"
    assert merchants[0].raw_url == "https://www.ebuyclub.com/reduction-1001-bijoux-2837"
    assert all("/cashback/" not in m.raw_url for m in merchants)
    assert len(merchants) == 6


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_chaque_page_reelle_se_lit_sans_erreur(path):
    offers = _offers(path.stem)
    for o in offers:
        assert o.value is not None and o.value >= 0


@pytest.mark.parametrize("stem,value,unit,base,effective", [
    ("1001-bijoux-2837",  "3.3", "percent",   None,  "3.3"),
    ("audible-7491",      "11",  "fixed_eur", None,  None),    # nouveaux clients
    ("darty-846",         "4.5", "percent",   "4.1", "4.5"),
    ("fnac-58",           "4.5", "percent",   "4.1", "4.5"),
    ("new-balance-7344",  "2.5", "percent",   None,  "2.5"),
    ("orange-8370",       "48",  "fixed_eur", None,  "48"),
    ("recyclivre-14985",  "2.5", "percent",   None,  None),    # nouveaux clients
])
def test_taux_principal(stem, value, unit, base, effective):
    p = _purchase(_offers(stem))
    assert p.value == Decimal(value)
    assert p.unit == unit
    assert p.value_base == (Decimal(base) if base else None)
    assert p.effective_value == (Decimal(effective) if effective else None)


def test_booking_sans_cashback_rend_zero_offre():
    assert _offers("booking-972") == []


def test_darty_retient_le_bon_marchand_et_ses_categories():
    """La page Darty contient aussi l'objet NordVPN (50%) : il ne doit pas fuiter."""
    offers = _offers("darty-846")
    assert _purchase(offers).value == Decimal("4.5")
    categories = [o for o in offers if o.kind == "category"]
    assert len(categories) == 12
    assert all(o.effective_value is None for o in categories)
    assert [o.value for o in offers if o.kind == "giftcard"] == [Decimal("3")]


def test_fnac_categories_et_bon_d_achat():
    offers = _offers("fnac-58")
    assert _purchase(offers).is_upto is True
    labels = {o.category_label: o.value for o in offers if o.kind == "category"}
    assert labels["sur l'informatique pour les non adhérents à la carte Fnac"] == Decimal("2.5")
    assert [o.value for o in offers if o.kind == "giftcard"] == [Decimal("3")]


def test_recyclivre_nouveaux_et_anciens_clients():
    offers = _offers("recyclivre-14985")
    p = _purchase(offers)
    assert p.is_new_customer_only is True and p.is_upto is True
    old = [o for o in offers if o.kind == "category" and not o.is_new_customer_only]
    assert [o.value for o in old] == [Decimal("1.6")]


def test_cas_simple_sans_categorie():
    offers = _offers("new-balance-7344")
    assert [o.kind for o in offers] == ["purchase"]


def test_nom_lu_sur_la_page():
    html = (FIXTURES / "1001-bijoux-2837.html").read_text(encoding="utf-8")
    adapter = EbuyclubAdapter(fetch=lambda url: "")
    assert adapter.merchant_details(_merchant("1001-bijoux-2837"), html) == {
        "raw_name": "1001 bijoux",
    }


def test_structure_inconnue_leve_une_erreur_explicite():
    with pytest.raises(EbuyclubParseError):
        EbuyclubAdapter(fetch=lambda url: "").parse_offer(
            _merchant("x-1"), "<html><body>rien</body></html>")
