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
from dataclasses import dataclass
from typing import Literal

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


# --- Décision d'appariement --------------------------------------------------
#
# Logique pure, sans base : `decide` reçoit un alias et les marchands connus,
# et rend ce qu'il faut faire. La commande `maxicash match` applique.

# Au-dessous du seuil d'appariement mais au-dessus de ce plancher, deux noms se
# ressemblent trop pour créer un nouveau marchand sans regarder : validation.
REVIEW_FLOOR = 0.75

# Deuxièmes niveaux de domaine qui ne désignent pas une entreprise
# (« boutique.co.uk »). Au MVP, marchés français : liste courte suffisante.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gouv"})


def domain_key(url_or_domain: str | None) -> str | None:
    """Domaine enregistrable : « https://boutique.orange.fr/x » → « orange.fr ».

    Deux plateformes pointent souvent vers des sous-domaines différents du même
    marchand (boutique.orange.fr, www.orange.fr) : c'est cette clé qu'on compare.
    """
    if not url_or_domain:
        return None
    host = domain_root(url_or_domain).split(":")[0].strip(".")
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


@dataclass(frozen=True)
class KnownMerchant:
    id: int
    slug: str
    name: str
    domain: str | None                 # merchant.canonical_domain
    provider_ids: frozenset[int]       # plateformes déjà rattachées


@dataclass(frozen=True)
class Decision:
    action: Literal["link", "create", "review"]
    merchant_id: int | None = None
    confidence: float | None = None
    reason: str = ""


def decide(
    provider_id: int,
    raw_name: str,
    raw_domain: str | None,
    merchants: list[KnownMerchant],
) -> Decision:
    """Rattacher, créer, ou demander une validation.

    1. Même domaine          → rattacher (certitude), sauf si cette plateforme
                                est déjà rattachée à ce marchand : deux fiches
                                d'une même plateforme pour un domaine, c'est à
                                regarder (« Fnac » et « Fnac Spectacles »).
    2. Nom très proche       → rattacher, sauf si les deux domaines sont connus
                                et différents.
    3. Nom assez proche      → validation manuelle.
    4. Rien                  → nouveau marchand.
    """
    key = domain_key(raw_domain)

    if key:
        for m in merchants:
            if m.domain == key:
                if provider_id in m.provider_ids:
                    return Decision("review", m.id, 1.0,
                                    f"domaine {key} déjà rattaché pour cette plateforme")
                return Decision("link", m.id, 1.0, f"domaine {key}")

    candidates = [m for m in merchants if provider_id not in m.provider_ids]
    best, score = None, 0.0
    for m in candidates:
        s = similarity(raw_name, m.name)
        if s > score:
            best, score = m, s

    if best and score >= FUZZY_THRESHOLD:
        if key and best.domain and best.domain != key:
            return Decision("review", best.id, round(score, 3),
                            f"nom proche de « {best.name} » mais domaines différents "
                            f"({key} / {best.domain})")
        return Decision("link", best.id, round(score, 3), f"nom proche de « {best.name} »")

    if best and score >= REVIEW_FLOOR:
        return Decision("review", best.id, round(score, 3),
                        f"nom ressemblant à « {best.name} »")

    return Decision("create", reason="aucun marchand correspondant")


def unique_slug(name: str, taken: set[str]) -> str:
    """Slug de page marchand (maxicash.fr/cashback/<slug>), sans collision."""
    base = slugify(normalize_name(name)) or slugify(name) or "marchand"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug
