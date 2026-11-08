-- 002: tahmin defteri koruma tetikleyicileri. Rol yetkilerinden bagimsiz ikinci savunma hattidir
-- (tablo sahibi dahil tum roller icin gecerlidir).

CREATE FUNCTION predictions_guard() RETURNS trigger
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
        RETURN NEW;
    END IF;

    -- UPDATE: tahmin alanlari degismez
    IF ROW(NEW.id, NEW.ticker, NEW.made_at, NEW.horizon_days, NEW.prob_up, NEW.score,
           NEW.model_version, NEW.model_role, NEW.feature_hash, NEW.features_json, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.ticker, OLD.made_at, OLD.horizon_days, OLD.prob_up, OLD.score,
           OLD.model_version, OLD.model_role, OLD.feature_hash, OLD.features_json, OLD.created_at)
    THEN
        RAISE EXCEPTION 'predictions: prediction fields are immutable'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF OLD.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION 'predictions: already resolved; resolution is write-once'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.resolved_at IS NULL OR NEW.realized_ret IS NULL OR NEW.label_up IS NULL THEN
        RAISE EXCEPTION 'predictions: resolution requires resolved_at, realized_ret and label_up'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.resolved_at <= NEW.made_at THEN
        RAISE EXCEPTION 'predictions: resolved_at must be after made_at'
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER predictions_guard
    BEFORE INSERT OR UPDATE OR DELETE ON predictions
    FOR EACH ROW EXECUTE FUNCTION predictions_guard();

CREATE FUNCTION predictions_no_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'predictions is append-only: TRUNCATE is not allowed'
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER predictions_no_truncate
    BEFORE TRUNCATE ON predictions
    FOR EACH STATEMENT EXECUTE FUNCTION predictions_no_truncate();
