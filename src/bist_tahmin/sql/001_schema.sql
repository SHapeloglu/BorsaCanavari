-- 001: temel sema (PROJE.md Bolum 6). bist_owner rolu altinda calisir.

CREATE TABLE prices (
    ticker     TEXT NOT NULL,
    date       DATE NOT NULL,
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,           -- ham kapanis (adjust=False; Faz 1'de dogrulanacak)
    close_adj  DOUBLE PRECISION,           -- duzeltmeli kapanis (varsayilan cikti)
    volume     DOUBLE PRECISION,
    source     TEXT NOT NULL,              -- 'borsapy' | 'yfinance'
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE corporate_actions (
    ticker     TEXT NOT NULL,
    ex_date    DATE NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('split', 'dividend')),
    value      DOUBLE PRECISION NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticker, ex_date, kind)
);

CREATE TABLE universe_membership (
    ticker      TEXT NOT NULL,
    index_name  TEXT NOT NULL DEFAULT 'BIST30',
    valid_from  DATE NOT NULL,
    valid_to    DATE,                      -- NULL = hala uye
    PRIMARY KEY (ticker, index_name, valid_from),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE trading_calendar (
    date        DATE PRIMARY KEY,
    is_half_day BOOLEAN NOT NULL DEFAULT FALSE
);

-- Tahmin defteri: append-only. Tahmin alanlari degistirilemez (002_guard.sql tetikleyicisi + 003 yetkileri).
CREATE TABLE predictions (
    id               BIGSERIAL PRIMARY KEY,
    ticker           TEXT NOT NULL,
    made_at          DATE NOT NULL,
    horizon_days     INTEGER NOT NULL CHECK (horizon_days > 0),
    prob_up          DOUBLE PRECISION NOT NULL CHECK (prob_up BETWEEN 0 AND 1),
    score            DOUBLE PRECISION,
    model_version    TEXT NOT NULL,
    model_role       TEXT NOT NULL CHECK (model_role IN ('champion', 'challenger', 'baseline')),
    feature_hash     TEXT NOT NULL,
    features_json    JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL,
    -- Cozum alanlari (sonradan, tek seferlik)
    resolved_at      DATE,
    realized_ret     DOUBLE PRECISION,
    realized_rel_ret DOUBLE PRECISION,
    tradable_ret     DOUBLE PRECISION,
    label_up         INTEGER CHECK (label_up IN (0, 1)),
    label_rel_up     INTEGER CHECK (label_rel_up IN (0, 1)),
    UNIQUE (ticker, made_at, horizon_days, model_version),
    CHECK ((resolved_at IS NULL) = (label_up IS NULL))
);
CREATE INDEX predictions_unresolved_idx ON predictions (made_at) WHERE resolved_at IS NULL;
CREATE INDEX predictions_model_idx ON predictions (model_version, made_at);

CREATE TABLE models (
    version     TEXT PRIMARY KEY,
    trained_at  TIMESTAMPTZ NOT NULL,
    train_end   DATE NOT NULL,
    params_json JSONB NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('champion', 'challenger', 'retired'))
);

CREATE TABLE experiments (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL,
    family      TEXT NOT NULL,
    description TEXT NOT NULL,
    params_json JSONB NOT NULL,
    result_json JSONB
);

CREATE TABLE runs (
    id          BIGSERIAL PRIMARY KEY,
    job         TEXT NOT NULL,
    run_date    DATE NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL CHECK (status IN ('running', 'ok', 'failed', 'skipped')),
    detail      JSONB,
    UNIQUE (job, run_date, started_at)
);
