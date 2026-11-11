# architect.md — BIST Tahmin Laboratuvarı: mimari

Karar gerekçeleri ve sayısal kabul kriterleri `PROJE.md`'dedir. Bu dosya **nasıl bağlandığını** anlatır. Durum: 2026-11-11.

## Bileşenler

```
 borsapy (TradingView fiyat, İş Yatırım splits)
        │
        ▼
 data/source.py ──► data/fetch.py ──► data/quality.py (kalite, yarım bar)
   (ham/düzeltmeli     │                calendar.py (XU100 işlem günleri, tatil)
    fiyat, splits      ▼
    önbelleği)     data/store.py ──► Postgres `bist_tahmin` (prices, corporate_actions,
                        │             universe_membership, trading_calendar, predictions, models,
                        │             experiments, runs, system_state)
                        ▼
 features/build.py (34 özellik, f1, sızıntısız, kanonik JSON + sha256)
        │
        ├──► baselines/rules.py (5 baseline) ──┐
        └──► models/ (logreg, train, promote,   │
              registry; yalnızca champion/      ▼
              challenger, promote = tek yol) predict/daily.py ──► predictions (append-only)
                                                 │
 resolve/resolver.py ◄── vade gelince ───────────┘  (tek seferlik çözüm, adjustment koruması)
        │
        ▼
 monitor/ (drift, devre kesici; system_state) ──► pipeline.py (run-daily akışı)
        │
        ▼
 evaluate/ (metrics, bootstrap, portfolio, costs, report) ──► `bist report weekly`
 backtest/ (dataset, splits, guard, registry, lab, runner) ──► `experiments`
```

## Veri akışı (günlük, İstanbul 19:00)
1. **Evren:** `universe sync` ile kaynaktan (XU030) güncel 30 bileşen; 25'ten az ise uygulanmaz.
2. **İndirme:** XU100 önce (takvim kaynağı), sonra hisseler. Hisse başına splits bir kez çekilir (önbellek). Kalite kapısı: `error` olan hisse yazılmaz, `flag` yazılır.
3. **Bayatlık:** son bar beklenen işlem gününe eşit değilse tahmin yazılmaz (çıkış 3).
4. **Çözüm:** vadesi gelen tahminler `ticker/XU100` barlarıyla çözülür; bölünme tutarsızlığı ya da >%15 sıçrama varsa yazılmaz.
5. **Tahmin:** baseline'lar (ve champion/challenger varsa modeller) yazılır; yazım penceresi ertesi seans açılışına kadardır (`LateWrite`).
6. **İzleme:** drift alarmı devre kesiciyi açar (model üretimini durdurur, baseline sürer); bayat veride izleme atlanır.
7. **Kayıt:** her adım `runs.detail`'e yazılır; kilit `pg_advisory_lock` ile.

## Sunucu topolojisi (Contabo)
- Konteyner `bist-tahmin:latest` (`/opt/bist-tahmin/docker-compose.yml`), ağ `ortak-db` (external), `mem_limit 1g`.
- Veritabanı `bist_tahmin` ortak Postgres'te (`postgres-ortak`, `:5432`): roller `bist_owner` (NOLOGIN, migration), `bist_app` (LOGIN, en az yetki, `statement_timeout 60s`, bağlantı sınırı 5).
- Konteyner `.env` (600): yalnızca `DATABASE_URL` (bist_app). Yönetici bağlantısı konteynere verilmez.
- Cron: `/etc/cron.d/bist-tahmin` (Berlin saatiyle iki olası saat; `ONLY_IST_HOUR` koruması). Sarmalayıcılar `deploy/cron_run.sh`, `deploy/cron_weekly.sh`, `deploy/cron_health.sh` (**sağlık cron'u henüz sunucuda yok**).
- Yedek: `postgres-ortak` gecelik `pg_dumpall` (03:50, 04:30) bist_tahmin'i kapsar; geri yükleme testi yapılmadı.
- Dağıtım: `git archive <dal> | ssh contabo 'tar -x -C /opt/bist-tahmin'`, sonra `docker build -t bist-tahmin:latest .`; önceki imaj `bist-tahmin:prev-*` etiketiyle saklanır.

## Güvenlik katmanları (veritabanında, uygulama kodunda değil)
- Tahmin defteri: `predictions` için `bist_app` yalnızca INSERT/SELECT ve çözüm sütunlarında UPDATE; DELETE/TRUNCATE yok; tetikleyici (002, 005) tahmin alanlarını ve çift çözümü engeller; `recorded_at` DB saatine kilitli.
- Champion: tek champion kısıtı (007), rol geçişi tetikleyicisi; champion yalnızca `promote` ile (SET LOCAL kapısı).
- Devre kesici durumu (007): geçmiş değiştirilemez, gerekçesiz reset yok.
- Holdout: eğitim `holdout_start` sonrası satırları dışarıda tutar; terfi kanıtı erişim bayrağı, pencere eşleşmesi ve eğitim son tarihi ile doğrulanır (fail-closed).
- **Sınır:** SET LOCAL kapısı kasıtlı SQL bypass'ını durdurmaz; güçlü garanti `SECURITY DEFINER` gerektirir (bkz. backlog).

## Test altyapısı
- Gömülü Postgres (`pgserver`) veya `BIST_TEST_ADMIN_URL` ile gerçek Postgres 16. Test başına izole veritabanı (`run_init`).
- Tam takım yaklaşık 2–10 dakika; geçici dizin büyük diskte olmalı (bkz. CLAUDE.md).

## Kararların nerede olduğu
| Konu | Yer |
|---|---|
| Hedef, ölçütler, sayısal kabul | PROJE.md Bölüm 0, 11, 15 |
| Veri sözleşmesi, sızıntı kuralları | PROJE.md Bölüm 4, 5 |
| Terfi kuralı (5 koşul) | PROJE.md Bölüm 11; `models/promote.py` |
| Sunucu dağıtımı kayıtları | PROJE.md Bölüm 19 ve v3.0 günlüğü; `session.md` |
| Açık kararlar | PROJE.md Bölüm 17; `backlog.md` |
