# MAXICASH — collecteur

Socle minimal du MVP (lot L0 réduit) et premier adaptateur de plateforme
(lot L1, Widilo). Le périmètre, les arbitrages et le découpage en lots sont
dans **Spécification MVP — Comparateur de cashback + extension**.

## Démarrer

```bash
cp .env.example .env          # puis renseignez MAXICASH_USER_AGENT (obligatoire)
make up                       # Postgres local via Docker
make install                  # installe le collecteur en mode éditable
make migrate                  # applique db/migrations/*.sql
make test                     # suite de tests, sans réseau
```

`MAXICASH_USER_AGENT` doit contenir une URL de contact joignable. Le collecteur
refuse de démarrer sans, et c'est voulu : un crawler anonyme est ce qui
déclenche un blocage.

## Commandes

| Commande | Effet |
|---|---|
| `maxicash migrate` | applique les migrations, idempotent |
| `maxicash discover widilo` | énumère les marchands via le sitemap |
| `maxicash snapshot widilo fnac` | fige une page réelle en fixture de test |
| `maxicash run widilo --limit 20` | collecte et écrit en base |
| `maxicash status` | dernières passes, fraîcheur, erreurs |

## La première chose à faire

Les expressions XPath de `adapters/widilo.py` ont été écrites d'après la
structure observée du site, pas d'après son balisage réel — je n'ai pas eu
accès au HTML brut. Elles sont regroupées dans un seul dictionnaire `XPATHS`.

```bash
maxicash snapshot widilo fnac      # fige la vraie page
make test                     # le test sur fixture réelle échoue
# ajustez XPATHS, relancez jusqu'au vert
```

Ce test est volontairement le garde-fou du projet : tant qu'il est rouge,
aucune collecte ne doit tourner. Une casse de DOM devient un test rouge
plutôt que des données silencieusement fausses.

## Ce que la collecte respecte

- `robots.txt` lu et appliqué avant chaque requête. Injoignable = tout interdit.
- Une requête toutes les 3 s par hôte, jamais de parallélisme sur un domaine.
- Cache disque : en développement, une page déjà lue n'est pas redemandée.
- Aucun lien de tracking n'est suivi. Chez Widilo, `robots.txt` interdit
  `/out/`, `/redirect/` et `/l/` — la résolution de redirection pour obtenir le
  domaine canonique est donc exclue sur cette plateforme. L'appariement se fait
  par slug et par nom, avec validation manuelle au-dessous du seuil.

## Structure

```
db/migrations/     schéma SQL, appliqué dans l'ordre
collector/
  src/maxicash/
    config.py      environnement
    types.py       contrat ProviderAdapter, RawMerchant, RawOffer
    http.py        client poli : robots, cadence, cache
    normalize.py   parsing des taux → effective_value   ← le cœur métier
    matching.py    appariement des marchands
    db.py          accès Postgres, snapshots jamais écrasés
    cli.py         commandes
    adapters/      une plateforme = un fichier
  tests/           fixtures + tests, aucun accès réseau
```

## Ce qui reste du lot L0

Volontairement écarté pour livrer un adaptateur qui tourne d'abord :

- back-office de validation de `review_queue` (pour l'instant : requêtes SQL) ;
- alerting Slack sur variation anormale (le calcul est fait, l'envoi non) ;
- déploiement du planificateur (GitHub Actions en cron, lot L7).
