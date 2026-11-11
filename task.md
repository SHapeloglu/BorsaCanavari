# task.md — BIST Tahmin Laboratuvarı: görevler

Durum: 2026-11-11. Açık kararlar `PROJE.md` Bölüm 17'dedir; fikirler `backlog.md`'dedir.

## 🔜 Sıradaki

- [ ] **Holdout kanıt düzeltmesini sunucuya dağıt** (`7b29738`; sunucudaki imaj bu düzeltmeden önceki). Adımlar: arşiv → yeni imaj (önceki imaj `prev-*` olarak etiketlenir) → `run-daily --dry-run` → `breaker status`. Model olmadığı için günlük akışı etkilemiyor. **Onay bekliyor.**
- [ ] **Sağlık cron'unu sunucuya ekle**: `deploy/cron_health.sh` ve `deploy/bist-tahmin.cron` içindeki 20:30 satırı, `/etc/cron.d/bist-tahmin`'e. Sunucuda henüz yok. **Onay bekliyor.**
- [ ] **11 Kasım 19:00 çalıştırmasını doğrula**: 150 yeni tahmin (`made_at=2026-11-11`), çıkış 0, `runs` son `daily` satırı `ok`, devre kesici kapalı. Tek seferlik kontrol yalnızca oturum açıkken çalışır; yoksa elle yapılır.
- [ ] **12 Kasım sabahı yedek kontrolü**: 03:50 dökümünde `bist_tahmin` ve roller var mı (`pg_dumpall` çıktısında `CREATE DATABASE bist_tahmin`).

## 🚧 Devam Eden

- _(yok)_ — Faz 4 yerelde tamam; canlı terfi yok, bilerek.

## ✅ Tamamlanan

- [x] 2026-11-11 — Migration 006 ve 007 sunucuda uygulandı (idempotent, şema kontrolü temiz).
- [x] 2026-11-11 — Geçmiş BIST 30 üyeliği yüklendi (58 aralık, açık üye 30, evren 9–11 Kasım ve geçmiş tarihlerde değişmedi).
- [x] 2026-11-11 — 17 eski üyenin fiyat geçmişi 2019-06'dan çekildi (47 hisse, takvim 2019–2026, hata yok).
- [x] 2026-11-11 — Holdout kanıt kontrolü düzeltildi: pencere, erişim bayrağı, eğitim son tarihi (`7b29738`, yerel; 726 test yeşil).
- [x] 2026-11-10 — 10 Kasım 19:00 çalıştırması: 150 tahmin, çıkış 0 (toplam defter 300 satır).
- [x] 2026-11-10 — Splits önbelleği: hisse başına çağrı 3 → 1 (`cab326c`); canlıya dağıtıldı (imaj içinde).
- [x] 2026-11-10 — Faz 4 bağımsız incelemesi ve düzeltmeleri: holdout bütünlüğü, champion'a göre terfi, champion koruması (007), drift durdurma, `bist models` CLI.
- [x] 2026-11-10 — Önceden kayıtlı `faz35-v2` deneyleri (E4–E6) koşuldu; hiçbiri baz oranı geçmedi.
- [x] 2026-11-09 — İlk canlı çalıştırma: 150 tahmin, çıkış 0 (ilk çalıştırma 24 dk sürdü → splits düzeltmesi).
- [x] 2026-11-08 — Cron kuruldu (19:00/19:45 günlük, cumartesi 10:00 rapor); `CRON_TZ` bu sunucuda çalışmadığı için İstanbul saat koruması.
- [x] 2026-11-08 — Contabo kurulumu: `bist_tahmin` DB, roller, migration 001–005, imaj, ilk veri.
- [x] 2026-11-08 — Faz 3.5 laboratuvarı ve ilk üç deney (E1–E3): Brier baz oranı geçemedi.
- [x] 2026-11-08 — Faz 0–3: iskelet, veri, kalite, takvim, tahmin defteri, çözücü, pipeline, metrikler, portföy, rapor.
