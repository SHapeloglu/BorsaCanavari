-- SuperBI icin salt okunur rol (bist_tahmin veritabani, public semasi).
-- Durum: TASLAK. Sunucuya uygulanmadi. Once 1. BOLUM (salt okuma), onaydan sonra 2. BOLUM.
--
-- Kapsam (bilerek dar):
--   Erisilebilen: predictions, models, experiments, runs, system_state
--   Erisilemeyen: prices, corporate_actions, universe_membership, trading_calendar (ham veri; lisans kosulu, PROJE Bolum 4)
--
-- Parola bu dosyaya YAZILMAZ. Rol olusturulduktan sonra parola psql icinde `\password bist_readonly` ile verilir.

-- ============================================================================
-- 1. BOLUM: KURU KONTROL (yalnizca okur; hicbir sey degistirmez)
-- ============================================================================
-- Rol var mi?
SELECT rolname, rolcanlogin, rolconnlimit FROM pg_roles WHERE rolname = 'bist_readonly';
-- Hedef tablolar var mi ve ne kadar satir?
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'public'
   AND table_name IN ('predictions','models','experiments','runs','system_state','prices','corporate_actions',
                      'universe_membership','trading_calendar')
 ORDER BY table_name;
-- Bu veritabaninda mevcut yetkiler (PUBLIC'e acik bir sey olmamali)
SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants
 WHERE table_schema = 'public' AND grantee IN ('PUBLIC','bist_readonly') ORDER BY table_name;

-- ============================================================================
-- 2. BOLUM: UYGULAMA (yalniz kuru kontrol onaylandiktan ve kullanici onay verdikten sonra)
-- ============================================================================
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bist_readonly') THEN
    CREATE ROLE bist_readonly LOGIN NOINHERIT CONNECTION LIMIT 3;
  END IF;
END
$$;

-- Oturum seviyesinde salt okunur ve zaman sinirli: yazma denemesi ve uzun sorgular sunucuda reddedilir.
ALTER ROLE bist_readonly SET default_transaction_read_only = on;
ALTER ROLE bist_readonly SET statement_timeout = '30s';

GRANT CONNECT ON DATABASE bist_tahmin TO bist_readonly;
GRANT USAGE ON SCHEMA public TO bist_readonly;

-- Yalnizca turetilmis tablolar. Ham fiyat tablolarina hic yetki verilmez.
GRANT SELECT ON predictions, models, experiments, runs, system_state TO bist_readonly;

-- Guvence: ham veriye yanlislikla yetki verilmis olmasin
REVOKE ALL ON prices, corporate_actions, universe_membership, trading_calendar FROM bist_readonly;

COMMIT;

-- Dogrulama (uygulamadan sonra): bu rolle baglanip
--   SELECT count(*) FROM predictions;          -- calismali
--   SELECT count(*) FROM prices;               -- 'permission denied' bekleniyor
--   INSERT INTO runs ... ;                     -- 'read-only transaction' bekleniyor
