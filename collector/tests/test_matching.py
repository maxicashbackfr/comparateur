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


# --- décision d'appariement ---------------------------------------------------

from maxicash.matching import KnownMerchant, decide, domain_key, unique_slug  # noqa: E402

WIDILO, IGRAAL = 1, 2


def _m(id, name, domain, *providers):
    return KnownMerchant(id, slugify(name), name, domain, frozenset(providers))


def test_domain_key_garde_le_domaine_enregistrable():
    assert domain_key("https://boutique.orange.fr/") == "orange.fr"
    assert domain_key("http://www.newbalance.fr/fr/home") == "newbalance.fr"
    assert domain_key("https://www.amazon.co.uk/") == "amazon.co.uk"
    assert domain_key(None) is None and domain_key("") is None


def test_meme_domaine_rattache_avec_certitude():
    known = [_m(1, "Orange", "orange.fr", WIDILO)]
    d = decide(IGRAAL, "Orange Boutique", "https://www.orange.fr", known)
    assert (d.action, d.merchant_id, d.confidence) == ("link", 1, 1.0)


def test_meme_domaine_meme_plateforme_part_en_validation():
    known = [_m(1, "Fnac", "fnac.com", WIDILO)]
    assert decide(WIDILO, "Fnac Occasion", "https://www.fnac.com/occasion", known).action == "review"


def test_nom_identique_sans_domaine_rattache():
    known = [_m(1, "New Balance", None, WIDILO)]
    d = decide(IGRAAL, "New Balance FR", None, known)
    assert (d.action, d.merchant_id) == ("link", 1)


def test_nom_identique_mais_domaines_differents_part_en_validation():
    known = [_m(1, "Orange", "orange.fr", WIDILO)]
    assert decide(IGRAAL, "Orange", "https://www.orange-bank.fr", known).action == "review"


def test_nom_ressemblant_part_en_validation():
    # 0,90 : trop proche pour créer à l'aveugle, trop loin pour rattacher.
    known = [_m(1, "Sport 3000", None, WIDILO)]
    assert decide(IGRAAL, "Sport 2000", None, known).action == "review"


def test_rien_de_proche_cree_un_marchand():
    known = [_m(1, "Fnac", "fnac.com", WIDILO)]
    assert decide(IGRAAL, "Decathlon", "https://www.decathlon.fr", known).action == "create"


def test_une_plateforme_ne_se_rattache_pas_deux_fois_par_le_nom():
    known = [_m(1, "Fnac", None, WIDILO)]
    assert decide(WIDILO, "Fnac", None, known).action == "create"


def test_unique_slug_evite_les_collisions():
    assert unique_slug("New Balance FR", set()) == "new-balance"
    assert unique_slug("Fnac", {"fnac", "fnac-2"}) == "fnac-3"
