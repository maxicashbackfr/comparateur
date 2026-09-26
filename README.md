# MAXICASH — collecteur

Socle minimal du MVP (lot L0 réduit) et premier adaptateur de plateforme
(lot L1, Widilo). Le périmètre, les arbitrages et le découpage en lots sont
dans **Spécification MVP — Comparateur de cashback + extension**.

## Base de données : Neon

Le projet tourne sur **Neon** (Postgres managé, mise en veille automatique).
Deux endpoints pour le même projet, et la distinction compte :

| Endpoint | Hostname | Usage |
|---|---|---|
| Direct | `ep-XXXX.<region>.aws.neon.tech` | collecteur et migrations |
| Poolé | `ep-XXXX-pooler.<region>.aws.neon.tech` | le site Next.js (lot L4) |

Le collecteur utilise l'endpoint **direct** : il ouvre une connexion, la garde
le temps de la passe, et `REFRESH MATERIALIZED VIEW` a besoin d'une session
complète. Le pooling par transaction est fait pour l'inverse — beaucoup de
connexions courtes, comme celles d'un rendu serverless.

`sslmode=require` et `channel_binding=require` sont exigés par Neon.

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

Le domaine est **maxicash.fr**. Publiez-y une page `/bot` avant la première
passe en production : elle explique qui nous sommes, ce que nous collectons et
comment nous joindre pour demander un retrait. C'est ce qui transforme un
blocage automatique en courriel, et c'est l'adresse que porte le User-Agent.

Arborescence SEO arrêtée (§12 de la spéc) :

```
maxicash.fr/cashback/{marchand}      page marchand — 90 % du trafic visé
maxicash.fr/plateformes/{plateforme} fiche plateforme
maxicash.fr/meilleurs-taux           classements hebdomadaires
```

Ces chemins ne changent plus une fois indexés : une redirection conserve le
trafic, jamais toute l'autorité acquise.

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

## Deux décisions coûteuses à défaire

Le reste du code est remplaçable. Ces deux règles ne le sont pas — les enfreindre
ne casse rien tout de suite, et coûte cher plus tard.

**1. `DATABASE_URL` est le seul point de couplage à l'hébergeur.**

Pas d'ORM, pas d'extension exotique, pas de SDK propriétaire, des migrations en
SQL brut appliquées par notre propre exécuteur. Changer d'hébergeur doit rester
un changement de variable d'environnement.

Ce qui romprait la règle, par ordre de tentation : le driver serverless de Neon
(HTTP au lieu de TCP) — si adopté côté site, à isoler derrière une seule
fonction d'accès ; les branches Neon dans la CI — confort de développement,
jamais une dépendance du produit ; de la logique métier en PL/pgSQL — elle
appartient à Python, où elle se teste et se déplace.

Conséquence : partir de Neon laisse les deux portes ouvertes. S'appuyer un jour
sur l'authentification d'une plateforme en ferme une, parce que les identités
et les politiques de sécurité, elles, ne se transfèrent pas.

**2. `offer_snapshot` est en ajout seul.**

Aucun `UPDATE` sur un taux, jamais. L'état courant est une vue dérivée. C'est
l'historique qui fait la valeur du produit, et il ne se reconstitue pas après
coup.

Depuis la migration 003, un relevé identique au précédent n'est pas réécrit —
ce n'est pas une entorse : ce qui est mis à jour est `last_checked_at`, la
preuve d'avoir vérifié, qui n'est pas une donnée d'historique. Sans cela la
base grossit d'environ 360 Mo par mois sans rien apprendre ; avec, de l'ordre
de 20 Mo.

Le site doit afficher **les deux dates** : « ce taux est en vigueur depuis le X »
(`collected_at`) et « vérifié le Y » (`last_checked_at`). Les confondre ferait
passer un taux stable et frais pour un taux périmé.

## Ce qui reste du lot L0

Volontairement écarté pour livrer un adaptateur qui tourne d'abord :

- back-office de validation de `review_queue` (pour l'instant : requêtes SQL) ;
- alerting Slack sur variation anormale (le calcul est fait, l'envoi non) ;
- déploiement du planificateur (GitHub Actions en cron, lot L7).
