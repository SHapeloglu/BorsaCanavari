-- 003: bist_app yetkileri (en az yetki). bist_owner altinda calisir; bist_app rolu onceden vardir.

-- Bu veritabanina yalnizca bist_app (ve sahip/superuser) baglanabilsin; paylasimli sunucudaki
-- diger kullanicilar baglanamasin.
DO $$
BEGIN
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO bist_app', current_database());
END
$$;

-- Referans/kaynak veri: ekle ve guncelle (yeniden indirme upsert); silme yok.
GRANT SELECT, INSERT, UPDATE ON prices, corporate_actions, universe_membership, trading_calendar TO bist_app;

-- Tahmin defteri: ekle, oku, yalnizca cozum sutunlarini guncelle. DELETE/TRUNCATE yok.
GRANT SELECT, INSERT ON predictions TO bist_app;
GRANT UPDATE (resolved_at, realized_ret, realized_rel_ret, tradable_ret, label_up, label_rel_up)
    ON predictions TO bist_app;

-- Modeller: ekle, oku, yalnizca rol degisebilir.
GRANT SELECT, INSERT ON models TO bist_app;
GRANT UPDATE (role) ON models TO bist_app;

-- Deneme kayitlari: ekle, oku, sonuc alani sonradan yazilabilir.
GRANT SELECT, INSERT ON experiments TO bist_app;
GRANT UPDATE (result_json) ON experiments TO bist_app;

-- Is gunlugu
GRANT SELECT, INSERT, UPDATE ON runs TO bist_app;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO bist_app;
