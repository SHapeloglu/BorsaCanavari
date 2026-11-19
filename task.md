# task.md — BIST Tahmin Laboratuvarı: görevler

Durum: 2026-11-14. Açık kararlar `PROJE.md` Bölüm 17'dedir; fikirler `backlog.md`'dedir.

## 🔜 Sıradaki

- [ ] **Holdout kanıt düzeltmesini sunucuya dağıt** (`7b29738`; sunucudaki imaj bu düzeltmeden önceki). **Onaylandı (2026-11-18), sunucuda henüz yapılmadı:** bu oturumda ssh erişimi izin sisteminde reddedildi. Adımlar: imajı `7b29738` ağacından derle (çalışma ağacındaki yerel değişiklikler dahil edilmez) → önceki imaj `prev-20261118` olarak etiketlenir → `run-daily --dry-run` → `breaker status`. Model olmadığı için günlük akışı etkilemiyor. Sunucu değişikliği `session.md`'ye yazılır.
- [ ] **Sağlık cron'unu sunucuya ekle** (**onaylandı, uygulanmadı**): önce salt okunur kontrol `docker compose run --rm --no-deps -T bist health`; sonra `sudo cp deploy/bist-tahmin.cron /etc/cron.d/bist-tahmin` ve içeriği doğrula. 20:30 satırı `cron_health.sh` ile çalışır. Cron pencereleri (19:00, 19:45, cumartesi 10:00) dışında yapılır.
- [x] **K1 birleştirme** (2026-11-19, yerel, push edilmedi): `agent/kapi-v3` (`e17456a`), `agent/proje-duzelt` (`e2b4b0c`), `agent/md-senkron` (`bcf42eb`), `agent/verify-script` (`d1140d5`), `agent/kontrol-katmani` (`488932b`) faz-4'e birleşti; `settings.json` çakışması kontrol-katmani sürümüyle çözüldü (deny listesi aynı, hooks eklendi). Dallar ve worktree'ler silindi. Bundan sonra `git merge` kanca tarafından engelleniyor; birleştirmeleri kullanıcı yapar.
- [ ] **11 Kasım 19:00 çalıştırmasını doğrula**: 150 yeni tahmin (`made_at=2026-11-11`), çıkış 0, `runs` son `daily` satırı `ok`, devre kesici kapalı. **Süresi geçti; sonucu kayıtlı değil.** Kullanıcı ya da sunucu teyidi gerekir.
- [ ] **12 Kasım sabahı yedek kontrolü**: 03:50 dökümünde `bist_tahmin` ve roller var mı (`pg_dumpall` çıktısında `CREATE DATABASE bist_tahmin`). **Süresi geçti; sonucu kayıtlı değil.**

## 🚧 Devam Eden

- _(yok)_ — Faz 4 yerelde tamam; canlı terfi yok, bilerek. 13 Kasım tarihli kod değişiklikleri commit edilmemiş durumda; kayıt ana ağaçtaki commit edilmemiş session.md'de (satır 93). Commit kararı kullanıcıda.

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
