# Migrations

Un fichier SQL par migration, préfixé d'un numéro. `maxicash migrate` les applique
dans l'ordre et mémorise les appliquées dans `schema_migrations`.

Règle : on n'édite jamais une migration déjà appliquée en production, on en
ajoute une nouvelle. Le schéma est écrit en `CREATE ... IF NOT EXISTS` pour
que la CI puisse le rejouer.

## Le point à ne pas modifier sans y réfléchir

`offer_snapshot` ne subit jamais d'`UPDATE`. Chaque relevé est une ligne.
`offer_current` est une vue matérialisée dérivée, pas une table.

C'est ce qui rend l'historique possible — et l'historique est l'actif que le
projet construit. Il ne se reconstitue pas après coup.
