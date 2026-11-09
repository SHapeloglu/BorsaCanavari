-- 006: sistem durumu (anahtar -> JSON). Devre kesici durumu gibi kucuk, kalici izleme durumlari icin.
-- bist_owner altinda calisir. Idempotenttir (IF NOT EXISTS; GRANT tekrarlanabilir).
-- bist_app: okuyabilir, ekleyebilir, guncelleyebilir; SILEMEZ (durum gecmisi value icindeki 'history' listesindedir,
-- bir kaydin kaybolmasi devre kesicinin sessizce kapanmasi demek olurdu).

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON system_state TO bist_app;
