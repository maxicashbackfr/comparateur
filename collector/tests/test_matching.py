from maxicash.matching import best_match, domain_root, normalize_name, slugify


def test_slugify_supprime_les_accents():
    assert slugify("Bébé9") == "bebe9"
    assert slugify("L'Occitane") == "l-occitane"


def test_normalize_name_retire_le_bruit_de_plateforme():
    assert normalize_name("Nike FR") == "nike"
    assert normalize_name("Nike.com") == "nike"
    assert normalize_name("Nike Store en ligne") == "nike"


def test_domain_root_retire_le_www():
    assert domain_root("https://www.zalando.fr/femme") == "zalando.fr"


def test_best_match_trouve_la_bonne_enseigne():
    candidates = {"nike": "Nike", "new-balance": "New Balance", "puma": "Puma"}
    slug, score = best_match("Nike FR", candidates)
    assert slug == "nike" and score >= 0.92


def test_best_match_refuse_plutot_que_de_se_tromper():
    # « Sport 2000 » et « Sport » se ressemblent : au-dessous du seuil, on
    # préfère la file de validation manuelle à un faux appariement.
    candidates = {"decathlon": "Decathlon", "intersport": "Intersport"}
    slug, _ = best_match("Sport 2000", candidates)
    assert slug is None
