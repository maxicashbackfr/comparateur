-- 004_alias_domain.sql
--
-- Le domaine du site marchand, tel que la plateforme l'indique sur sa propre
-- page (chez Widilo : champ « url » de la page marchand). C'est la meilleure
-- clé d'appariement entre plateformes, et elle s'obtient sans suivre aucun
-- lien de tracking.

ALTER TABLE merchant_alias
    ADD COLUMN IF NOT EXISTS raw_domain TEXT;

CREATE INDEX IF NOT EXISTS idx_alias_unmatched
    ON merchant_alias (provider_id)
    WHERE merchant_id IS NULL;
