-- 003_write_on_change.sql
--
-- Un taux de cashback bouge rarement. Écrire un snapshot à chaque passe même
-- quand rien n'a changé fait croître offer_snapshot d'environ 360 Mo par mois
-- pour 5 plateformes × 400 marchands × 4 passes/jour — le plafond gratuit est
-- atteint en six semaines, et l'historique n'y gagne rien.
--
-- On n'insère donc que lorsque la valeur change. Le principe reste entier :
-- aucun UPDATE sur un taux, offer_snapshot demeure en ajout seul. Ce qui est
-- mis à jour, c'est la preuve d'avoir vérifié — qui n'est pas une donnée
-- d'historique.

ALTER TABLE merchant_alias
    ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_alias_last_checked
    ON merchant_alias (last_checked_at);

-- offers_found reste le nombre d'offres LUES : c'est lui qui sert à détecter
-- une casse de DOM. offers_written compte les insertions réelles.
ALTER TABLE scrape_run
    ADD COLUMN IF NOT EXISTS offers_written INTEGER NOT NULL DEFAULT 0;

-- La vue courante doit maintenant distinguer deux dates, et le site doit
-- afficher les deux :
--   collected_at    — ce taux est en vigueur depuis cette date
--   last_checked_at — nous l'avons vérifié pour la dernière fois à cette date
-- Confondre les deux ferait passer un taux stable et frais pour un taux périmé.
-- merchant_coverage dépend d'offer_current : il faut la retirer d'abord, puis
-- la reconstruire. Sans cela, le DROP échoue — pas de CASCADE ici, qui
-- supprimerait en silence des objets qu'on ne voit pas.
DROP VIEW IF EXISTS merchant_coverage;
DROP MATERIALIZED VIEW IF EXISTS offer_current;

CREATE MATERIALIZED VIEW offer_current AS
SELECT DISTINCT ON (s.merchant_alias_id)
       s.id AS snapshot_id,
       a.merchant_id,
       s.provider_id,
       s.merchant_alias_id,
       s.value,
       s.value_base,
       s.unit,
       s.effective_value,
       s.is_upto,
       s.conditions_text,
       s.collected_at,
       a.last_checked_at
FROM   offer_snapshot s
JOIN   merchant_alias a ON a.id = s.merchant_alias_id
WHERE  s.kind = 'purchase' AND s.category_label IS NULL
ORDER  BY s.merchant_alias_id, s.collected_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_current_alias
    ON offer_current (merchant_alias_id);
CREATE INDEX IF NOT EXISTS idx_offer_current_merchant
    ON offer_current (merchant_id);

-- Reconstruite à l'identique, plus la fraîcheur : `oldest_checked_at` est ce
-- qui permet de vérifier le critère de recette « aucun taux de plus de 48 h ».
CREATE OR REPLACE VIEW merchant_coverage AS
SELECT m.id           AS merchant_id,
       m.slug,
       m.name,
       COUNT(DISTINCT c.provider_id)  AS providers_count,
       MAX(c.effective_value)         AS best_effective_value,
       MIN(c.collected_at)            AS oldest_collected_at,
       MIN(c.last_checked_at)         AS oldest_checked_at
FROM   merchant m
LEFT   JOIN offer_current c ON c.merchant_id = m.id
GROUP  BY m.id, m.slug, m.name;
