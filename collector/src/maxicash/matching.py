"""Appariement des marchands entre plateformes.

Le domaine canonique serait la meilleure clé — mais chez Widilo les chemins de
redirection (/out/, /redirect/, /l/) sont interdits par robots.txt. Résoudre un
lien de tracking pour obtenir le domaine final n'est donc pas une option : ni
techniquement autorisé, ni souhaitable (chaque résolution génère un clic dans
les statistiques du réseau d'affiliation).

On apparie donc par slug normalisé, puis par nom, et tout ce qui reste ambigu
part en validation manuelle. Un marchand non apparié n'est jamais publié.
"""
from __future__ import annotations

import difflib
import re

from .normalize import strip_accents

FUZZY_THRESHOLD = 0.92

# Jetons de bruit : ce qui varie d'une plateforme à l'autre sans changer
# l'identité du marchand. Comparés jeton par jeton — « en ligne » doit donc
# figurer comme deux entrées, pas comme une chaîne.
_NOISE = frozenset({
    "fr", "france", "com", "www", "shop", "store", "boutique",
    "officiel", "official", "en", "ligne", "online", "site",
})


def slugify(text: str) -> str:
    text = strip_accents(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_name(name: str) -> str:
    """Retire le bruit qui varie d'une plateforme à l'autre : « Nike FR »,
    « Nike.com » et « Nike » doivent se réduire au même jeton."""
    tokens = [t for t in slugify(name).split("-") if t and t not in _NOISE]
    return " ".join(tokens)


def domain_root(domain: str) -> str:
    """« www.zalando.fr » → « zalando.fr ». Pas de liste de suffixes publics ici :
    au MVP les domaines viennent du back-office, pas d'une résolution automatique."""
    domain = domain.strip().lower().removeprefix("http://").removeprefix("https://")
    domain = domain.split("/")[0]
    return domain.removeprefix("www.")


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


def best_match(raw_name: str, candidates: dict[str, str]) -> tuple[str | None, float]:
    """`candidates` : slug marchand → nom. Rend (slug, score) ou (None, score)
    si rien n'atteint le seuil. Le seuil est volontairement haut : un faux
    appariement publie un taux sous le mauvais marchand, ce qui est pire qu'un
    marchand manquant."""
    best_slug, best_score = None, 0.0
    for slug, name in candidates.items():
        score = similarity(raw_name, name)
        if score > best_score:
            best_slug, best_score = slug, score
    if best_score >= FUZZY_THRESHOLD:
        return best_slug, best_score
    return None, best_score
