"""Tests de l'adaptateur Widilo.

Deux niveaux :
  1. sur fixture synthétique — verrouille la logique, tourne toujours ;
  2. sur fixture réelle — ne tourne que si vous avez lancé
     `gcb snapshot widilo <slug>`. C'est ce test qui valide les SELECTORS.
"""
from decimal import Decimal
from pathlib import Path

import pytest

from gcb.adapters.widilo import WidiloAdapter
from gcb.types import RawMerchant

FIXTURES = Path(__file__).parent / "fixtures" / "widilo"


def _adapter() -> WidiloAdapter:
    return WidiloAdapter(fetch=lambda url: "")


def test_sitemap_ne_garde_que_les_pages_marchands():
    xml = (FIXTURES / "_synthetic_sitemap.xml").read_text(encoding="utf-8")
    merchants = list(WidiloAdapter.parse_sitemap(xml))
    slugs = [m.raw_slug for m in merchants]
    assert slugs == ["1001-pneus", "fnac", "darty", "orange"]
    assert "categories" not in slugs


def test_sitemap_capture_lastmod_quand_il_existe():
    xml = (FIXTURES / "_synthetic_sitemap.xml").read_text(encoding="utf-8")
    by_slug = {m.raw_slug: m for m in WidiloAdapter.parse_sitemap(xml)}
    assert by_slug["fnac"].lastmod == "2026-09-17"
    assert by_slug["orange"].lastmod is None


def test_page_synthetique_separe_les_trois_types_de_taux():
    html = (FIXTURES / "_synthetic_merchant.html").read_text(encoding="utf-8")
    merchant = RawMerchant("fnac", "Fnac", "https://www.widilo.fr/code-promo/fnac")
    offers = _adapter().parse_offer(merchant, html)

    purchase = [o for o in offers if o.kind == "purchase" and o.category_label is None]
    assert len(purchase) == 1
    assert purchase[0].value == Decimal("7.2")
    assert purchase[0].value_base == Decimal("2")   # le <del> a été isolé
    assert purchase[0].effective_value == Decimal("7.2")

    categories = [o for o in offers if o.kind == "category"]
    assert {o.category_label for o in categories} == {"High-tech", "Livres"}
    assert all(o.effective_value is None for o in categories)


def test_les_conditions_sont_rattachees_a_l_offre():
    html = (FIXTURES / "_synthetic_merchant.html").read_text(encoding="utf-8")
    merchant = RawMerchant("fnac", "Fnac", "https://www.widilo.fr/code-promo/fnac")
    offer = _adapter().parse_offer(merchant, html)[0]
    assert offer.is_sale_excluded is True
    assert offer.is_marketplace_excluded is True


# --- Fixtures réelles : c'est ici que les SELECTORS se valident ---------------

REAL = sorted(p for p in FIXTURES.glob("*.html") if not p.name.startswith("_"))


@pytest.mark.skipif(not REAL, reason="aucune fixture réelle — lancez `gcb snapshot widilo fnac`")
@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_page_reelle_produit_un_taux_d_achat(path):
    """Le test qui compte. Tant qu'il est rouge, les sélecteurs sont faux et
    aucune collecte ne doit tourner en production."""
    html = path.read_text(encoding="utf-8")
    merchant = RawMerchant(path.stem, path.stem, f"https://www.widilo.fr/code-promo/{path.stem}")
    offers = _adapter().parse_offer(merchant, html)
    purchase = [o for o in offers if o.kind == "purchase" and o.category_label is None]
    assert purchase, f"aucun taux d'achat extrait de {path.name} : ajustez SELECTORS"
    assert purchase[0].value is not None
    assert 0 < purchase[0].value <= 100 or purchase[0].unit == "fixed_eur"
