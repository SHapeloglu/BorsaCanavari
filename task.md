# task.md — BIST Tahmin Laboratuvarı: görevler

Durum: 2026-11-24. Açık kararlar `PROJE.md` Bölüm 17'dedir; fikirler `backlog.md`'dedir.

## 🔜 Sıradaki

- [x] **Holdout kanıt düzeltmesini sunucuya dağıt** (2026-11-19, uygulandı): `7b29738` ağacından `bist-tahmin:holdout-7b29738` (`d6036647f670`) derlendi; önceki `latest` `prev-20261119` (`ce76edae4cad`) olarak etiketlendi; `latest` = `d6036647f670`. `run-daily --dry-run` çıkış 0, yazma yok; `health` SAGLIKLI, devre kesici kapalı. Model olmadığı için günlük akış etkilenmez. Ayrıntı: `session.md` 2026-11-19.
- [x] **Sağlık cron'unu sunucuya ekle** (2026-11-19, uygulandı): `/etc/cron.d/bist-tahmin` repo sürümüyle birebir (`cmp` temiz, 20:30 `cron_health.sh` satırı dahil). Önceki dosya `/root/bist-tahmin.cron.bak-20261119`. Ayrıntı: `session.md` 2026-11-19.
- [x] **K1 birleştirme** (2026-11-19; faz-4'e birleşti, aynı gün push edildi: `2532893`): `agent/kapi-v3` (`e17456a`), `agent/proje-duzelt` (`e2b4b0c`), `agent/md-senkron` (`bcf42eb`), `agent/verify-script` (`d1140d5`), `agent/kontrol-katmani` (`488932b`) faz-4'e birleşti; `settings.json` çakışması kontrol-katmani sürümüyle çözüldü (deny listesi aynı, hooks eklendi). Dallar ve worktree'ler silindi. Bundan sonra `git merge` kanca tarafından engelleniyor; birleştirmeleri kullanıcı yapar.
- [x] **11 Kasım 19:00 çalıştırmasını doğrula** (2026-11-24, log'dan): 150 tahmin, çıkış 0. `runs` tablosu sorgulanmadı. Ayrıntı: `session.md` 2026-11-24.
- [x] **12 Kasım sabahı yedek kontrolü** (2026-11-24): `postgres-ortak-20261112-0350.sql.gz` gzip OK; `bist_tahmin` ve roller dökümde.
- [x] **`resolve` atlamaları (`horizon_unknown`)** (2026-11-24): açıklandı; vadesi henüz dolmamış tahminler, beklenen davranış. Log mesajı iyileştirilebilir.
- [x] **Sunucu imajını güncel koda al** (2026-11-24, uygulandı): `b549f0d` (runtime kodu `5665b38` ile aynı) `bist-tahmin:head-b549f0d` olarak derlendi; `latest` = `b3b284670391`; önceki `latest` `prev-20261124` (holdout `d6036647`). Kuru çalıştırma çıkış 0, `health` ve `breaker` temiz. Geri dönüş: `holdout-7b29738` etiketini `latest` yap. Eski plan metni: Kapsam: 11 kaynak dosya (`cli`, `fetch`, `jobs`, `promote`, `train`, `breaker`, `drift`, `health`, `pipeline`, `predict/daily`, `predict/ledger`); migration yok, şema değişmiyor. `predict/daily.py` ve `ledger.py` günlük yazma yolunda; kuru çalıştırma zorunlu. Plan: `git archive` ile ayrı dizin → `bist-tahmin:head-<sha>` derleme (arka planda, `< /dev/null`) → kod marker kontrolü → `run-daily --dry-run` → `health` → 18:30 İstanbul öncesinde `latest` etiketi (önceki: `prev-20261124`). Geri dönüş: `holdout-7b29738` etiketini `latest` yap.

- [ ] **Kapsam kancası birleştirme kararı** (`agent/kanca-kapsam-v2`, faz-4 üstünde): Bash uyarıya çevrildi, sınır izin listesinde. Birleştirme ve push kullanıcı kararında. Eski `agent/kanca-dorduncu-tur` (`d3da116`) yerelde, ayrıca bir karar bekliyor.
- [ ] **Git nesne deposu temizliği**: `.git/objects` içinde boş nesneler var (`git fsck` hataları; ana geçmiş sağlam). Yedek alınıp silme kararı kullanıcıda; silmeden yapılmaz.

## 🚧 Devam Eden

- _(yok)_ — Faz 4 yerelde tamam ve push edildi; canlı terfi yok, bilerek. 13 Kasım kod değişiklikleri `5665b38` ile commit edildi.

## ✅ Tamamlanan

- [x] 2026-11-24 — Kapsam kancası: Bash bulguları uyarıya çevrildi, Bash sınırı `.claude/settings.json` izin listesine taşındı (`agent/kanca-kapsam-v2`, faz-4 üstünde; commit: bkz. dal). Kanca testleri 75/75; tam takım bu dalda koşuldu.
- [x] 2026-11-24 — Günlük akış kontrolü (salt okunur): 19, 20 ve 23 Kasım koşuları çıkış 0, her biri 150 tahmin; `health` SAGLIKLI; devre kesici kapalı; haftalık rapor 21 Kasım üretildi; ALERT yok.
- [x] 2026-11-19 — faz-4 origin'e push: `9fc29a0..2532893`.

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
