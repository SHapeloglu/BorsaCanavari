-- 007: champion korumasi ve devre kesici durum korumasi. bist_owner altinda, tek islemde calisir.
-- 001-006 degistirilmedi; bu dosya onlarin uzerine eklenir.
--
-- (1) En fazla bir champion: kismi unique index (DB seviyesi garanti).
-- (2) Champion ATAMASI yalnizca registry.promote() ile: tetikleyici, bist_app dahil herkes icin, champion yazimini
--     yalnizca oturum ayari app.allow_promote = 'on' iken kabul eder. promote() bu ayari yalnizca kendi
--     transaction'i icinde (SET LOCAL) acar ve champion gecisinden hemen sonra 'off' yapar.
--     SINIR: bist_app kendi oturumunda bu ayari yazabilir; bu nedenle koruma kazara ve denetimsiz (promote disi)
--     champion yazimini engeller, kotu niyetli bir bist_app SQL'ini tek basina engellemez. Tam sinir icin
--     promote'in SECURITY DEFINER fonksiyonla yapilmasi gerekir (bkz. registry.py).
--     Gecisler: INSERT/UPDATE ile champion (yalniz ayar acikken); challenger->retired, champion->retired;
--     champion->challenger ve retired'dan her cikis yasak (retired terminal).
-- (3) system_state 'circuit_breaker' anahtari: satir silinemez/yeniden adlandirilamaz/TRUNCATE edilemez; 'history'
--     yalnizca eklenir (onceki elemanlar degismez/silinmez); trip (false->true) gerekce ister ve history'ye dokunmaz;
--     acik devre kesici (true->true) degistirilemez; her reset (true->false ya da kapaliyken) history'ye tam bir
--     kayit ekler ve reset_reason bos olamaz. Diger anahtarlar etkilenmez.

-- (1) ---------------------------------------------------------------------------------------------------------------
CREATE UNIQUE INDEX models_one_champion ON models (role) WHERE role = 'champion';

-- (2) ---------------------------------------------------------------------------------------------------------------
CREATE FUNCTION models_role_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.role = 'champion'
           AND coalesce(current_setting('app.allow_promote', true), '') <> 'on' THEN
            RAISE EXCEPTION 'models: champion yalniz promote ile atanabilir (%)', NEW.version
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: ayni rol (baska bir kolon zaten yetkisiz) serbest.
    IF NEW.role IS NOT DISTINCT FROM OLD.role THEN
        RETURN NEW;
    END IF;

    IF OLD.role = 'retired' THEN
        RAISE EXCEPTION 'models: retired terminal, geri donus yok (%)', NEW.version
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Buradan OLD.role = 'challenger' ya da 'champion'.
    IF NEW.role = 'champion' THEN
        IF OLD.role <> 'challenger' THEN
            RAISE EXCEPTION 'models: gecis yasak: % -> champion (%)', OLD.role, NEW.version
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF coalesce(current_setting('app.allow_promote', true), '') <> 'on' THEN
            RAISE EXCEPTION 'models: champion yalniz promote ile atanabilir (%)', NEW.version
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.role = 'retired' THEN
        RETURN NEW;
    END IF;

    -- challenger -> challenger degil (yukarida elendi); champion -> challenger ve diger her gecis yasak.
    RAISE EXCEPTION 'models: gecis yasak: % -> % (%)', OLD.role, NEW.role, NEW.version
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER models_role_guard
    BEFORE INSERT OR UPDATE ON models
    FOR EACH ROW EXECUTE FUNCTION models_role_guard();

-- (3) ---------------------------------------------------------------------------------------------------------------
CREATE FUNCTION system_state_breaker_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    old_h jsonb;
    new_h jsonb;
    old_n int;
    new_n int;
    old_t boolean;
    new_t boolean;
    i int;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.key = 'circuit_breaker' THEN
            RAISE EXCEPTION 'system_state: circuit_breaker silinemez'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.key = 'circuit_breaker'
           AND coalesce(NEW.value->'tripped', 'false'::jsonb) = 'true'::jsonb
           AND btrim(coalesce(NEW.value->>'reason', '')) = '' THEN
            RAISE EXCEPTION 'circuit_breaker: acik devre kesici gerekce (reason) ister'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE. Anahtar PK'dir; yeniden adlandirma bir silme/uretme yoludur, bu yuzden circuit_breaker'a girmez/cikmaz.
    IF OLD.key = 'circuit_breaker' AND NEW.key <> OLD.key THEN
        RAISE EXCEPTION 'system_state: circuit_breaker anahtari yeniden adlandirilamaz'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.key <> 'circuit_breaker' AND NEW.key = 'circuit_breaker' THEN
        RAISE EXCEPTION 'system_state: baska bir anahtar circuit_breaker adina tasinamaz'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.key <> 'circuit_breaker' THEN
        RETURN NEW;  -- diger anahtarlar serbest
    END IF;

    old_t := coalesce(OLD.value->'tripped', 'false'::jsonb) = 'true'::jsonb;
    new_t := coalesce(NEW.value->'tripped', 'false'::jsonb) = 'true'::jsonb;
    old_h := coalesce(OLD.value->'history', '[]'::jsonb);
    new_h := coalesce(NEW.value->'history', '[]'::jsonb);
    IF jsonb_typeof(old_h) <> 'array' OR jsonb_typeof(new_h) <> 'array' THEN
        RAISE EXCEPTION 'circuit_breaker: history bir dizi olmali'
            USING ERRCODE = 'restrict_violation';
    END IF;
    old_n := jsonb_array_length(old_h);
    new_n := jsonb_array_length(new_h);

    -- history yalnizca eklenir: onceki her eleman aynen korunur.
    IF new_n < old_n THEN
        RAISE EXCEPTION 'circuit_breaker: history silinemez (% -> %)', old_n, new_n
            USING ERRCODE = 'restrict_violation';
    END IF;
    FOR i IN 0 .. old_n - 1 LOOP
        IF old_h->i IS DISTINCT FROM new_h->i THEN
            RAISE EXCEPTION 'circuit_breaker: history degistirilemez (indeks %)', i
                USING ERRCODE = 'restrict_violation';
        END IF;
    END LOOP;

    IF new_t THEN
        IF old_t THEN
            -- Acik devre kesici: since/reason/model_version/relative_drawdown ve history degismez ('since' korunur).
            IF ROW(OLD.value->'since', OLD.value->'reason', OLD.value->'model_version',
                   OLD.value->'relative_drawdown') IS DISTINCT FROM
               ROW(NEW.value->'since', NEW.value->'reason', NEW.value->'model_version',
                   NEW.value->'relative_drawdown')
               OR new_n <> old_n THEN
                RAISE EXCEPTION 'circuit_breaker: acik devre kesici degistirilemez'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSE
            -- Trip: gerekce zorunlu; history degismez.
            IF btrim(coalesce(NEW.value->>'reason', '')) = '' THEN
                RAISE EXCEPTION 'circuit_breaker: trip icin gerekce (reason) zorunlu'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF new_n <> old_n THEN
                RAISE EXCEPTION 'circuit_breaker: trip history degistiremez'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
    ELSE
        -- Reset (acik ya da kapali iken): history'ye tam bir kayit eklenir ve gerekce dolu olmali.
        IF new_n <> old_n + 1 THEN
            RAISE EXCEPTION 'circuit_breaker: reset history alanina tam bir kayit eklemeli (% -> %)', old_n, new_n
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF btrim(coalesce(NEW.value->>'reset_reason', '')) = ''
           OR btrim(coalesce(new_h->(new_n - 1)->>'reset_reason', '')) = '' THEN
            RAISE EXCEPTION 'circuit_breaker: reset gerekcesi (reset_reason) bos olamaz'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER system_state_breaker_guard
    BEFORE INSERT OR UPDATE OR DELETE ON system_state
    FOR EACH ROW EXECUTE FUNCTION system_state_breaker_guard();

CREATE FUNCTION system_state_no_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'system_state: TRUNCATE devre kesici durumunu siler'
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER system_state_no_truncate
    BEFORE TRUNCATE ON system_state
    FOR EACH STATEMENT EXECUTE FUNCTION system_state_no_truncate();
