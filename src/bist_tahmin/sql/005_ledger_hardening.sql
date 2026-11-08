-- 005: tahmin defteri sertlestirme. bist_owner altinda, tek islemde calisir.
-- Bosluklar: (a) uygulama made_at/created_at'i istedigi gibi yazabiliyordu, (b) cozum alanlari kismi yazilabiliyordu
-- (tek seferlik oldugu icin sonradan tamamlanamaz), (c) vade gelmeden cozum yazilabiliyordu.

-- 1) recorded_at: DB saati; uygulama gonderse bile tetikleyici ustune yazar.
ALTER TABLE predictions ADD COLUMN recorded_at TIMESTAMPTZ;

-- Eski satirlar icin created_at ile doldur. Guard tetikleyicisi UPDATE'i engelledigi icin gecici devre disi
-- (bu islem commit olmadan baska oturum bu durumu goremez; ALTER TABLE ACCESS EXCLUSIVE kilit tutar).
ALTER TABLE predictions DISABLE TRIGGER predictions_guard;
UPDATE predictions SET recorded_at = created_at WHERE recorded_at IS NULL;
ALTER TABLE predictions ENABLE TRIGGER predictions_guard;

ALTER TABLE predictions ALTER COLUMN recorded_at SET NOT NULL;
ALTER TABLE predictions ALTER COLUMN recorded_at SET DEFAULT clock_timestamp();

-- 2-3) Guard: recorded_at DB saati, ileri tarihli tahmin yok, tam ve vadesi gelmis cozum.
CREATE OR REPLACE FUNCTION predictions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'predictions is append-only: DELETE is not allowed'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.resolved_at IS NOT NULL OR NEW.realized_ret IS NOT NULL
           OR NEW.realized_rel_ret IS NOT NULL OR NEW.tradable_ret IS NOT NULL
           OR NEW.label_up IS NOT NULL OR NEW.label_rel_up IS NOT NULL THEN
            RAISE EXCEPTION 'predictions: resolution fields can only be set by UPDATE after the horizon'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- Guvenilir kayit zamani: uygulamanin gonderdigi deger yok sayilir.
        NEW.recorded_at := clock_timestamp();
        IF NEW.made_at > (NEW.recorded_at AT TIME ZONE 'Europe/Istanbul')::date THEN
            RAISE EXCEPTION 'predictions: made_at (%) cannot be after the recording date', NEW.made_at
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: tahmin alanlari ve recorded_at degismez
    IF ROW(NEW.id, NEW.ticker, NEW.made_at, NEW.horizon_days, NEW.prob_up, NEW.score,
           NEW.model_version, NEW.model_role, NEW.feature_hash, NEW.features_json, NEW.created_at,
           NEW.recorded_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.ticker, OLD.made_at, OLD.horizon_days, OLD.prob_up, OLD.score,
           OLD.model_version, OLD.model_role, OLD.feature_hash, OLD.features_json, OLD.created_at,
           OLD.recorded_at)
    THEN
        RAISE EXCEPTION 'predictions: prediction fields are immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION 'predictions: already resolved; resolution is write-once'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.resolved_at IS NULL OR NEW.realized_ret IS NULL OR NEW.realized_rel_ret IS NULL
       OR NEW.tradable_ret IS NULL OR NEW.label_up IS NULL OR NEW.label_rel_up IS NULL THEN
        RAISE EXCEPTION 'predictions: resolution requires all of resolved_at, realized_ret, realized_rel_ret, tradable_ret, label_up, label_rel_up'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.resolved_at <= NEW.made_at THEN
        RAISE EXCEPTION 'predictions: resolved_at must be after made_at'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- H islem gunu en az H takvim gunudur: vade gelmeden cozumu engelleyen kaba ama guvenli alt sinir.
    IF NEW.resolved_at < NEW.made_at + NEW.horizon_days THEN
        RAISE EXCEPTION 'predictions: resolved_at (%) is before made_at + horizon_days (% + %)',
            NEW.resolved_at, NEW.made_at, NEW.horizon_days
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$;
