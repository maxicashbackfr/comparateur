-- 002_views.sql — état courant dérivé de l'historique, jamais stocké en dur.

CREATE MATERIALIZED VIEW IF NOT EXISTS offer_current AS
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
       s.collected_at
FROM   offer_snapshot s
JOIN   merchant_alias a ON a.id = s.merchant_alias_id
WHERE  s.kind = 'purchase' AND s.category_label IS NULL
ORDER  BY s.merchant_alias_id, s.collected_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_offer_current_alias
    ON offer_current (merchant_alias_id);
CREATE INDEX IF NOT EXISTS idx_offer_current_merchant
    ON offer_current (merchant_id);

-- Couverture : combien de plateformes couvrent chaque marchand.
-- C'est la colonne H du classeur, produite automatiquement.
CREATE OR REPLACE VIEW merchant_coverage AS
SELECT m.id           AS merchant_id,
       m.slug,
       m.name,
       COUNT(DISTINCT c.provider_id)              AS providers_count,
       MAX(c.effective_value)                     AS best_effective_value,
       MIN(c.collected_at)                        AS oldest_collected_at
FROM   merchant m
LEFT   JOIN offer_current c ON c.merchant_id = m.id
GROUP  BY m.id, m.slug, m.name;
