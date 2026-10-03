-- 005_offer_current_all.sql
--
-- 1. Fin d'offre. Depuis la migration 003, un relevé identique n'est pas
--    réécrit ; il fallait donc un moyen de dire qu'une offre a DISPARU de la
--    page (bon d'achat retiré, catégorie supprimée, cashback arrêté). Le
--    collecteur écrit alors une ligne de clôture : même kind et même
--    category_label, value NULL. Toujours un INSERT, l'historique garde la
--    date de fin. Les vues ci-dessous ignorent les offres clôturées.
--
-- 2. offer_current_all : l'offre en cours de chaque alias pour CHAQUE type
--    (achat, catégorie, bon d'achat). Sert à la page marchand, bloc « Autres
--    façons d'économiser ». offer_current reste la seule base du classement.

DROP VIEW IF EXISTS merchant_coverage;
DROP MATERIALIZED VIEW IF EXISTS offer_current;

CREATE MATERIALIZED VIEW offer_current AS
SELECT * FROM (
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
    ORDER  BY s.merchant_alias_id, s.collected_at DESC, s.id DESC
) latest
WHERE latest.value IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_current_alias
    ON offer_current (merchant_alias_id);
CREATE INDEX IF NOT EXISTS idx_offer_current_merchant
    ON offer_current (merchant_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS offer_current_all AS
SELECT * FROM (
    SELECT DISTINCT ON (s.merchant_alias_id, s.kind, COALESCE(s.category_label, ''))
           s.id AS snapshot_id,
           a.merchant_id,
           s.provider_id,
           s.merchant_alias_id,
           s.kind,
           s.category_label,
           s.value,
           s.value_base,
           s.unit,
           s.effective_value,
           s.is_upto,
           s.is_new_customer_only,
           s.conditions_text,
           s.collected_at,
           a.last_checked_at
    FROM   offer_snapshot s
    JOIN   merchant_alias a ON a.id = s.merchant_alias_id
    ORDER  BY s.merchant_alias_id, s.kind, COALESCE(s.category_label, ''),
              s.collected_at DESC, s.id DESC
) latest
WHERE latest.value IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_current_all_snapshot
    ON offer_current_all (snapshot_id);
CREATE INDEX IF NOT EXISTS idx_offer_current_all_merchant
    ON offer_current_all (merchant_id);

-- Reconstruite à l'identique de la 003, plus le nombre d'offres affichables.
CREATE OR REPLACE VIEW merchant_coverage AS
SELECT m.id                           AS merchant_id,
       m.slug,
       m.name,
       COUNT(DISTINCT c.provider_id)  AS providers_count,
       MAX(c.effective_value)         AS best_effective_value,
       MIN(c.collected_at)            AS oldest_collected_at,
       MIN(c.last_checked_at)         AS oldest_checked_at,
       (SELECT COUNT(*) FROM offer_current_all o WHERE o.merchant_id = m.id)
                                      AS offers_displayable
FROM   merchant m
LEFT   JOIN offer_current c ON c.merchant_id = m.id
GROUP  BY m.id, m.slug, m.name;
