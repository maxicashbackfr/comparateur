"""Tests de l'adaptateur Widilo, sur pages réelles figées le 26/09/2026.

Les cinq fixtures couvrent les cas qui piègent un comparateur :
  audible      montant fixe, réservé aux nouveaux clients
  fnac         taux boosté (3% -> 6%), 12 taux par catégorie, bon d'achat
  new-balance  cas simple : un seul taux
  orange       montant fixe boosté (30€ -> 65€), plusieurs offres
  recyclivre   taux nouveaux / anciens clients, mélange % et €, bon d'achat

Si Widilo change de structure, ces tests passent au rouge : aucune collecte ne
doit tourner tant qu'ils le sont.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from maxicash.adapters.widilo import WidiloAdapter, WidiloParseError
from maxicash.types import RawMerchant

FIXTURES = Path(__file__).parent / "fixtures" / "widilo"
REAL = sorted(p for p in FIXTURES.glob("*.html") if not p.name.startswith("_"))


def _adapter() -> WidiloAdapter:
    return WidiloAdapter(fetch=lambda url: "")


def _offers(slug: str):
    html = (FIXTURES / f"{slug}.html").read_text(encoding="utf-8")
    merchant = RawMerchant(slug, slug, f"https://www.widilo.fr/code-promo/{slug}")
    return _adapter().parse_offer(merchant, html)


def _purchase(offers):
    main = [o for o in offers if o.kind == "purchase" and o.category_label is None]
    assert len(main) == 1
    return main[0]


# --- sitemap -----------------------------------------------------------------

def test_sitemap_ne_garde_que_les_pages_marchands():
    xml = (FIXTURES / "_synthetic_sitemap.xml").read_text(encoding="utf-8")
    slugs = [m.raw_slug for m in WidiloAdapter.parse_sitemap(xml)]
    assert slugs == ["1001-pneus", "fnac", "darty", "orange"]
    assert "categories" not in slugs


def test_sitemap_capture_lastmod_quand_il_existe():
    xml = (FIXTURES / "_synthetic_sitemap.xml").read_text(encoding="utf-8")
    by_slug = {m.raw_slug: m for m in WidiloAdapter.parse_sitemap(xml)}
    assert by_slug["fnac"].lastmod == "2026-09-17"
    assert by_slug["orange"].lastmod is None


# --- pages réelles -----------------------------------------------------------

@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_page_reelle_produit_un_taux_d_achat(path):
    purchase = _purchase(_offers(path.stem))
    assert purchase.value is not None
    assert 0 < purchase.value <= 100 or purchase.unit == "fixed_eur"
    assert purchase.conditions_text


@pytest.mark.parametrize("slug,value,unit,base,effective", [
    ("audible",     "10.58", "fixed_eur", None, None),     # nouveaux clients seulement
    ("fnac",        "6",     "percent",   "3",  "6"),
    ("new-balance", "5",     "percent",   None, "5"),
    ("orange",      "65",    "fixed_eur", "30", "65"),
    ("recyclivre",  "2.5",   "percent",   None, None),     # nouveaux clients seulement
])
def test_taux_principal(slug, value, unit, base, effective):
    p = _purchase(_offers(slug))
    assert p.value == Decimal(value)
    assert p.unit == unit
    assert p.value_base == (Decimal(base) if base else None)
    assert p.effective_value == (Decimal(effective) if effective else None)


def test_fnac_detail_par_categorie_et_bon_d_achat():
    offers = _offers("fnac")
    p = _purchase(offers)
    assert p.is_upto is True
    assert p.is_marketplace_excluded is True       # « Marketplace, Livre : 0% »
    assert "Adhérents Fnac" in p.conditions_text

    categories = {o.category_label: o for o in offers if o.kind == "category"}
    assert len(categories) == 12
    assert categories["Fnac Photo"].value == Decimal("4.5")
    assert all(o.effective_value is None for o in categories.values())
    assert {o.unit for o in categories.values()} == {"percent", "fixed_eur"}

    giftcard = [o for o in offers if o.kind == "giftcard"]
    assert len(giftcard) == 1 and giftcard[0].value == Decimal("3.1")
    assert giftcard[0].effective_value is None


def test_un_seul_taux_ne_produit_pas_de_categorie():
    offers = _offers("new-balance")
    assert [o.kind for o in offers] == ["purchase"]
    assert _purchase(offers).is_upto is False


def test_audible_nouveaux_clients():
    p = _purchase(_offers("audible"))
    assert p.is_new_customer_only is True
    assert p.is_upto is False


def test_nom_du_marchand_lu_sur_la_page():
    html = (FIXTURES / "new-balance.html").read_text(encoding="utf-8")
    assert WidiloAdapter.merchant_name(html) == "New Balance"


def test_structure_inconnue_leve_une_erreur_explicite():
    merchant = RawMerchant("x", "x", "https://www.widilo.fr/code-promo/x")
    with pytest.raises(WidiloParseError):
        _adapter().parse_offer(merchant, "<html><body>rien</body></html>")
