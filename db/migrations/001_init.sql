-- 001_init.sql — socle du schéma.
-- Principe structurant : aucun UPDATE destructif sur un taux.
-- Chaque relevé crée une ligne dans offer_snapshot. L'état courant est une vue.

CREATE TABLE IF NOT EXISTS provider (
    id                  SERIAL PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    base_url            TEXT NOT NULL,
    referral_url        TEXT,
    signup_bonus_eur    NUMERIC(10,2),
    min_payout_eur      NUMERIC(10,2),
    avg_validation_days INTEGER,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS merchant (
    id               SERIAL PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    canonical_domain TEXT UNIQUE,
    category         TEXT,
    priority         TEXT CHECK (priority IN ('P1','P2','P3')),
    search_volume    INTEGER,
    is_published     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Le nom d'un marchand varie d'une plateforme à l'autre. Un alias par couple
-- (marchand, plateforme) : c'est la clé de jointure à chaque passe suivante.
CREATE TABLE IF NOT EXISTS merchant_alias (
    id          SERIAL PRIMARY KEY,
    merchant_id INTEGER REFERENCES merchant(id) ON DELETE CASCADE,
    provider_id INTEGER NOT NULL REFERENCES provider(id) ON DELETE CASCADE,
    raw_slug    TEXT NOT NULL,
    raw_name    TEXT NOT NULL,
    raw_url     TEXT NOT NULL,
    confidence  NUMERIC(4,3),
    UNIQUE (provider_id, raw_slug)
);

CREATE TABLE IF NOT EXISTS scrape_run (
    id           SERIAL PRIMARY KEY,
    provider_id  INTEGER NOT NULL REFERENCES provider(id),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running','ok','partial','failed')),
    urls_fetched INTEGER NOT NULL DEFAULT 0,
    offers_found INTEGER NOT NULL DEFAULT 0,
    errors_count INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);

-- unit : 'percent' = pourcentage du panier · 'fixed_eur' = montant fixe
-- kind  : ce que le taux récompense. Seul 'purchase' entre dans la comparaison.
CREATE TABLE IF NOT EXISTS offer_snapshot (
    id                      BIGSERIAL PRIMARY KEY,
    merchant_alias_id       INTEGER NOT NULL REFERENCES merchant_alias(id) ON DELETE CASCADE,
    provider_id             INTEGER NOT NULL REFERENCES provider(id),
    scrape_run_id           INTEGER NOT NULL REFERENCES scrape_run(id),

    value                   NUMERIC(10,3),          -- taux courant affiché
    value_base              NUMERIC(10,3),          -- taux barré, si campagne boostée
    unit                    TEXT NOT NULL CHECK (unit IN ('percent','fixed_eur')),
    kind                    TEXT NOT NULL DEFAULT 'purchase'
                            CHECK (kind IN ('purchase','signup_bonus','giftcard','category','unknown')),

    category_label          TEXT,
    conditions_text         TEXT,
    is_upto                 BOOLEAN NOT NULL DEFAULT FALSE,
    is_new_customer_only    BOOLEAN NOT NULL DEFAULT FALSE,
    is_sale_excluded        BOOLEAN,
    is_marketplace_excluded BOOLEAN,

    -- Colonne de tri. Renseignée uniquement pour kind='purchase' non catégoriel.
    -- Voir §4 de la spéc : mélanger les types produit une comparaison fausse.
    effective_value         NUMERIC(10,3),

    raw_text                TEXT NOT NULL,          -- ce qui a été lu, verbatim
    collected_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_snapshot_alias_time
    ON offer_snapshot (merchant_alias_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_run
    ON offer_snapshot (scrape_run_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_effective
    ON offer_snapshot (effective_value DESC NULLS LAST)
    WHERE kind = 'purchase';

-- File de validation manuelle : marchand non apparié, taux aberrant, DOM cassé.
CREATE TABLE IF NOT EXISTS review_queue (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    JSONB NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','ignored')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
