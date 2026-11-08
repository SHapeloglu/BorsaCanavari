-- 004: duzeltmeli acilis. `prices.close` ham, `close_adj` duzeltmeli; acilis ham gelir. Cozucu (tradable_ret =
-- made_at+1 acilis -> made_at+H kapanis) araya bedelsiz/bolunme dusunce ham acilis ile duzeltmeli kapanisi
-- karistirmamali. Bolunme duzeltmesi gun siniridir, gun icinde oran sabittir; bu yuzden
-- open_adj = open * close_adj / close ayni gunun barinda tutarlidir. Tek tanim burada.
ALTER TABLE prices
    ADD COLUMN open_adj DOUBLE PRECISION
    GENERATED ALWAYS AS (open * close_adj / NULLIF(close, 0)) STORED;
