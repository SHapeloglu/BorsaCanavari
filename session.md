# session.md — BIST Tahmin Laboratuvarı: oturum günlüğü

Yeni kayıtlar en üste. Her kayıt: yapılanlar, kararlar ve nedeni, açık sorunlar, sıradaki adım. Sunucu değişiklikleri ayrıca `/opt/postgres-ortak/session.md` (ortak servis) ve `task.md`'de de izlenir.

---

## 2026-11-24 — sunucu imajı güncellemesi
**Yapılanlar:**
- Kullanıcı onayıyla ("olur") `b549f0d` (runtime kodu `5665b38` ile aynı) ayrı dizinde derlendi: `/opt/bist-tahmin-head-b549f0d` → `bist-tahmin:head-b549f0d` (`b3b284670391`). Derleme arka planda, `setsid nohup` ile; 11 kaynak dosya değişikliğinin işaret kontrolü (`load_resolved`) imajın içinde doğrulandı.
- Kuru çalıştırma (`run-daily --dry-run`, yeni imaj): çıkış 0, yazma yok; 23 Kasım için 150 zaten vardı. `health` SAGLIKLI, `breaker status` kapalı.
- Etiketler: önceki `latest` → `prev-20261124` (holdout `d6036647`); `head-b549f0d` → `latest`. Geri dönüş: `holdout-7b29738` → `latest`.
- Sunucu diski %94 (14 GB boş) — 19 Kasım'daki %85'ten yükseldi. Docker'da geri kazanılabilir ~35 GB görünüyor; silinmedi, başka projelerin imajları olabilir.
- 19:00 çalıştırması bu imajla yapılacak; sonucu 24 Kasım akşamı kontrol edilmeli.

## 2026-11-24
**Yapılanlar:**
- Salt okunur kontrol (09:55 İstanbul): 19, 20 ve 23 Kasım 19:00 koşuları çıkış 0, her biri 150 tahmin; 19 Kasım 19:45 yeniden deneme gerekmedi; 21–22 Kasım hafta sonu.
- Sağlık cron'u 20:30'da çalışıyor (23 Kasım 20:30 kaydı); `health` SAGLIKLI; son başarılı akış 23 Kasım 19:00.
- Haftalık rapor 21 Kasım 10:00: çıkış 0, `reports/rapor-2026-11-21.md`.
- ALERT.txt yok; devre kesici kapalı.
- Tüm proje md dosyaları güncellendi: README, architect, task, backlog, PROJE (v3.4), ORCHESTRATION (sürüm 4), session.

**Kararlar / neden:**
- `resolve` adımındaki `horizon_unknown` atlamaları (günde ~600) kayda alındı ama neden incelenmedi; kod ve veri incelemesi gerekiyor, bu yüzden backlog'a ve task'a açık madde olarak eklendi.
- ssh yasağı kullanıcı tarafından kaldırıldı; proje izin dosyasındaki ssh satırı dosyada duruyor, dokunulmadı.

**Ek kontroller (24 Kasım, aynı gün):**
- 11 Kasım 19:00 (log: 17:00 CET başladı, 17:16 bitti): çıkış 0, 150 tahmin yazıldı. `runs` tablosu sorgulanmadı.
- 12 Kasım yedeği `postgres-ortak-20261112-0350.sql.gz`: gzip bütünlüğü OK; dökümde `bist_tahmin` veritabanı ve `bist_owner`/`bist_app` rolleri birer kez var.
- `horizon_unknown`: kod incelendi. `add_trading_days` vade takvimin son işlem gününü aşarsa `None` döndürüyor; yani 5 işlem günlük vadesi henüz dolmamış tahminler. Son 4 tahmin günü × 150 = 600 ile sayı uyumlu. Beklenen davranış; log mesajı "vade bilinemiyor" yanıltıcı.

**Açık sorunlar:** Sunucu imajında `5665b38` inceleme düzeltmeleri yok (imaj `7b29738`'den). Dağıtım kararı bekliyor.

**Sıradaki adım:** 5665b38 dağıtımı için kuru çalıştırma planı onayı.

---

## 2026-11-19
**Yapılanlar:**
- Onaylı birleştirmeler (yerel, push yok): `kapi-v3` (kapsam kapisi, K1 betigi, ORCHESTRATION v3), `proje-duzelt` (PROJE v3.3 tur 2), `md-senkron` (architect, README, backlog, task), `verify-script` (scripts/), `kontrol-katmani` (PreToolUse kapsam kancasi). Sonuç faz-4 HEAD `488932b`.
- `task.md` ile `md-senkron` çakışması: ana dalın holdout/cron/K1 satırları korundu, 11 ve 12 Kasım satırları "süresi geçti" olarak alındı.
- `kontrol-katmani`'nin `settings.json` çakışması kontrol-katmani sürümüyle çözüldü; deny listesi ana dalla aynı kaldı.
- Birleştirilmiş ana ağaçta tam takım: 736 passed, 3 skipped, çıkış 0. Kanca testleri: 68/68 geçti.
- Temizlik: 5 worktree ve dal silindi (`agent/hizli-test` dahil; zaten HEAD'in atasıydı).

**Kararlar / neden:**
- Birleştirmeler yerelde tutuldu; push, sunucu salt okunur kontrollerinden (`bist health`, `breaker status`, son `runs` satırı) sonra yapılacak (`CLAUDE.md` birleştirme kuralı).
- Kanca `git merge`/`push`/`pull`'u her modda engelliyor; birleştirmeler bundan sonra kullanıcı tarafından yapılır.

**Sunucu (kullanıcı onayıyla; ssh yasağı yalnızca bu işler için geçici kaldırılıp geri yüklendi):**
- Salt okunur kontroller (17:29 İstanbul): `bist health` SAGLIKLI (son günlük akış 18 Kasım 19:00 başarılı, veri güncel, takılı iş yok); `bist breaker status` kapalı.
- Push: `origin/faz-4-yasam-dongusu` `9fc29a0..f62cce0` (23 commit), `git push` yasağı geçici kaldırılıp geri yüklendi.
- Sağlık cron'u: `/etc/cron.d/bist-tahmin` repo sürümüyle değiştirildi (`cmp` temiz). Önceki dosya `/root/bist-tahmin.cron.bak-20261119`. 20:30 `cron_health.sh` satırı artık etkin.
- Holdout imajı: `7b29738` ağacından (`git archive`, çalışma ağacı dahil değil) `/opt/bist-tahmin-holdout-7b29738` altında derlendi → `bist-tahmin:holdout-7b29738` (`d6036647f670`). Önceki `latest` (`ce76edae4cad`, 11 Kasım) `bist-tahmin:prev-20261119` olarak etiketlendi; `latest` artık `d6036647f670`. Derleme 15:37 Berlin'de (günlük çalıştırmadan önce) bitti.
- `run-daily --dry-run` yeni imajla: çıkış 0, yazma yok; 18 Kasım için 150 kayıt zaten vardı, aktif model yok.
- Sonrası `health` SAGLIKLI, `breaker status` kapalı (15:41 Berlin).
- Build logu `/root/build-holdout-7b29738.log`, dry-run logu `/root/dryrun-holdout-7b29738.log`.

**Açık sorunlar:** 11 ve 12 Kasım kontrolleri süresi geçmiş; sonuçları kayıtlı değil. 19 Kasım 19:00 çalıştırmasının (ilk günlük koşu yeni imajla) sonucu henüz yok; bakılmalı.

**Sıradaki adım:** 19 Kasım 19:00 ve 19:45 koşusunun `runs` sonucu ve 20:30 sağlık kontrolü; sonra bu kaydı kapatma.

---

## 2026-11-18

**Yapılanlar:**
- Kullanıcı dört öneriyi onayladı: holdout dağıtımı, sağlık cron'u, commit'ler, K1 birleştirme ve kapsam kancasının üçüncü turu.
- Kapsam kancası üçüncü tur (`agent/kontrol-katmani`, commit `91f3390`): değişkenli/komut ikameli yıkıcı komutlar, xargs ile yıkıcı hedef, find -delete/-exec, eval/source/bare kabuk, satır içi python -c/perl -e, ANSI-C kaçışı engellendi. Bash ile yazma sözleşme modunda yasak; lider modunda kod ve kanca yollarına yazma engel. Şablondan `echo` kaldırıldı. Testler: `scripts/tests/test_scope_guard.py` (50) + `test_scope_guard_bash.py` (18) = 68 OK.
- Ana ağaç: 12 inceleme düzeltmesi, `tests/test_review_fixes.py`, `deploy/` iki salt okunur betik, `.claude/` ayarları commit'lendi. Tam takım: 736 passed, 3 skipped, çıkış 0 (946 s), commit öncesi.
- Sunucu: ssh erişimi bu oturumda izin sisteminde reddedildi. Sunucuya **hiçbir değişiklik yapılmadı**. Holdout dağıtımı ve sağlık cron'u onaylı ama bekliyor (`task.md`).

**Kararlar / neden:**
- Kanca için sınır değil uyarı katmanı: yorumlayıcının dosya çalıştırması (/tmp'e yazıp python3 ile çalıştırma), sözleşme modunda `python3 -m unittest` ile test kodunun yan etkisi ve Read aracı kapsam dışı. Bunlar `TEMPLATE.json` ve hook başlığında belgelendi.
- Holdout dağıtımı 7b29738 ağacından derlenir; çalışma ağacındaki 12 düzeltme sunucuya gitmez (onay kapsamı dışı).

**Açık sorunlar:**
- Sunucu imajı holdout düzeltmesinden önce.
- Sağlık cron'u sunucuda yok.
- K1 birleştirme (`agent/verify-script`, `agent/kontrol-katmani`) kullanıcı tarafından yapılacak; lider `git merge` izin listesinde yasak.
- 11 Kasım 19:00 çalıştırması ve 12 Kasım yedek kontrolü hâlâ doğrulanmadı.

**Sıradaki adım:** Sunucu erişimi açılırsa `task.md` ilk iki madde; ardından K1 birleştirme ve kanca pilotu.

---

## 2026-11-13
**Yapılanlar:** Salt-okunur inceleme bulgularının uygulanması (commit atılmadı, çalışma ağacında):
- Vade gelmiş tahminde `missing_bar`, `invalid_price`, `no_prices` artık uyarı (`pipeline.py`).
- Takvim (XU100) indirilemezse hisseler indirilmez/yazılmaz (`data/fetch.py`).
- `breaker reset` gunluk akisin kilidini (`lock_job='daily'`) bekler ve `runs`a `breaker-reset` olarak yazilir (`cli.py`, `jobs.py`).
- Kurumsal islemler fiyattan once cekilir; hata olursa fiyat yazilmaz (`data/fetch.py`).
- Defter: ayni anahtarli ama icerigi farkli atlanan satirlar sayilir ve uyari verilir (`predict/ledger.py`, `predict/daily.py`).
- Drift okuma ufuga gore suzulur (`monitor/drift.py`).
- `--use-holdout` icin `--confirm-holdout` zorunlu (`cli.py`).
- Egitim kaydina `dropped_nonfinite_rows`; ozellik surumu eksikse reddedilir (`models/train.py`).
- `run_health` kullanilmayan `cfg` parametresi kaldirildi; `fetch` beklenmeyen hatada temiz cikar.
- Yeni testler: `tests/test_review_fixes.py` (9 test). Tam takim: 735 passed, 3 skipped, cikis 0.

**Kararlar / neden:**
- Cron yeniden denemesi (exit 1'de 19:45) bilerek degistirilmedi: exit 1 "eksik veri" anlamina geliyor ve gec gelen veri icin yeniden deneme isin amaci. Bedeli bir idempotent ekstra calisma.
- Maliyet varsayilani (10 bps) ve devre kesici/terfi maliyet kurali degistirilmedi: politika karari gerektiriyor.
- `app.allow_promote` GUC sinirini SECURITY DEFINER'a tasima degistirilmedi: veritabani migration'i ve sunucu onayi (K2) gerektirir.
- `source.py` kismi bolunme uyusmazligini hataya cevirme degistirilmedi: borsapy veri farkliliklarinda yanlis pozitif riski var.
- Takvim tatil bilgisi (idari izin) ve baz oranlarin ardisik-bar varsayimi degistirilmedi: davranis degisikligi, ayri karar.

**Acik sorunlar:** Sunucu imaji bu degisikliklerden onceki surumde; dagitim K2 onayi bekliyor.

**Sıradaki adım:** Degisikliklerin commit'i (kullanici onayi) ve sonra dagitim karari.

## 2026-11-11

**Yapılanlar:**
- Sunucuya migration 006 (`system_state`) ve 007 (champion koruması) uygulandı; önce `--dry-run` (yalnızca 006 ve 007 bekliyordu), sonra gerçek, sonra idempotans kontrolü.
- Yeni imaj (`bist-tahmin:latest`) derlendi; önceki imaj `prev-20261110` etiketiyle saklandı.
- Kuru `run-daily` (yeni imaj): çıkış 0; `breaker status` kapalı; `models list` boş.
- Geçmiş BIST 30 üyeliği yüklendi (`universe load-history`: 28 ekleme, 30 açık satır valid_from geri çekildi). Evren 9 Kasım, 10 Kasım, 1 Aralık ve 2020'de 30 hisse; yarınki günlük fetch kümesi değişmedi.
- 17 eski üyenin fiyat geçmişi 2019-06'dan çekildi (`fetch --since 2019-06-01`): 47 hisse, takvim 2019-06-03 → 2026-11-10 (1.859 işlem günü), hata ve dışlama yok.
- Holdout kanıt kontrolü düzeltildi (`7b29738`, yalnız yerel): terfi kanıtı kullanım erişimi, pencere eşleşmesi ve eğitim son tarihi ile doğrulanır. Mekanik test (holdout'a dokunmadan) bu açığı yakaladı: holdout_start=2030 ile üretilmiş bir sonuç kabul edilebiliyordu.
- 10 Kasım 19:00 çalıştırması doğrulandı: 150 tahmin, çıkış 0; defter 300 satır.
- Bu dokümanlar (CLAUDE, architect, task, backlog, session, README) oluşturuldu/güncellendi.

**Kararlar / neden:**
- Holdout'u mekanik test için gerçek veriyle kullanmadık: kopya yapılandırmada `holdout_start` geleceğe alınıp holdout satırları veri setinden düşürüldü. Gerekçe: gerçek sınama kümesini bir kez kullanmak terfi kanıtını tüketir.
- Faz 4 kodu dağıtıldı ama terfi yok: champion yok, cron yalnızca `run-daily`, `models` komutları manuel. Model katmanı üretimi etkilemiyor.
- Holdout düzeltmesi (`7b29738`) sunucuya henüz uygulanmadı; terfi öncesi zorunlu. Onay bekliyor.
- Sağlık cron'u sunucuya eklenmedi; depoda hazır. Onay bekliyor.

**Açık sorunlar:**
- Sunucu imajı holdout düzeltmesinden önce (kod sürümü: `8307250` ağacı).
- Sağlık cron'u sunucuda yok.
- 11 Kasım 19:00 çalıştırmasının sonucu bu kayıt yazılırken henüz doğrulanmadı.
- Geri yükleme testi `postgres-ortak` için hâlâ yapılmadı.

**Sıradaki adım:**
- 11 Kasım 19:00 çalıştırmasını doğrula (150 yeni tahmin).
- Onay gelirse: holdout düzeltmesini ve sağlık cron'unu dağıt.

---

## 2026-11-10

**Yapılanlar:**
- Splits önbelleği (`cab326c`): hisse başına Is Yatırım splits çağrısı 3 → 1 (THYAO, gerçek ağ). Canlı ilk çalıştırmadaki 24 dakikanın kaynağı buydu.
- Faz 4 bağımsız incelemesi iki tur; kritik ve önemli bulgular düzeltildi: holdout dışarıda tutma, terfi referansı (champion'a göre + baz oran), canlı gölge iyileşme, champion yazma kapısı, migration 007 (tek champion, rol trigger'ı, system_state korumaları), drift durdurma, `bist models` CLI, bayat veride izleme kaydı.
- Önceden kayıtlı `faz35-v2` (E4–E6): hiçbiri Brier'da baz oranı geçmedi; piyasa-özellik hipotezi desteklenmedi.
- Tam takım 14 hatayla bitti; neden tmpfs disk doluluğu (gömülü Postgres kurtarma modu). Geçici dizin büyük diske alınınca 718 geçti, 0 hata.
- Önceki Faz 4 commit'i çıkış kodunu kontrol etmeden atıldı; hata ortamsal olduğu için kod doğru kaldı (sonradan doğrulandı).

**Kararlar / neden:**
- Terfi kuralı champion varsa champion'a göre + her zaman baz orana göre (PROJE Bölüm 11 ve Bölüm 0 ile uyumlu). Önceki "yalnız baz oran" kararı geri alındı.
- Faz 4 birleştirilmedi; `main` dalı yok, çalışma dalı `faz-4-yasam-dongusu`.
- Dağıtım dalı `deploy-splits-fix` ayrı tutuldu (Faz 4 olmadan yalnız splits düzeltmesi).

**Açık sorunlar:**
- Holdout kanıt penceresi kontrolü o an yalnızca yerelde (bkz. 11 Kasım).

**Sıradaki adım:**
- Splits düzeltmesini ve Faz 4'ü sunucuya dağıtmak (11 Kasım'da yapıldı).

---

## 2026-11-09

**Yapılanlar:**
- İlk canlı çalıştırma (19:00): 150 tahmin (30 hisse × 5 baseline, `made_at=2026-11-09`), çıkış 0. Süre 24 dakika (çok büyük; splits çağrıları nedeni).
- 19:45 yeniden deneme doğru şekilde atlandı (`bugun zaten basarili`).
- Zamanlayıcı kontrolleri (16:51, 19:12) makine yeniden başlatıldığı için çalışmadı; kontroller elle yapıldı.

**Kararlar / neden:**
- Sunucuya geçmiş üyelik ve eski üye fiyatları, ilk canlı çalıştırma temiz geçtikten sonra (sıra tutuldu).

**Açık sorunlar:**
- Splits düzeltmesi yok (24 dakika).

---

## 2026-11-08

**Yapılanlar:**
- Faz 0 (iskelet, şema, roller, `bist init`), Faz 1 (veri, kalite, XU100 takvimi, yarım bar elemesi), Faz 2 (özellikler 34, baseline 5, idempotent defter, çözücü, pipeline), Faz 3 (metrik, blok bootstrap, portföy, maliyet ızgarası, rapor), Faz 3.5 (purge'lu walk-forward, sınama kümesi kilidi, deney kaydı, veri seti, lab).
- Contabo kurulumu: `bist_tahmin` DB, `bist_owner` ve `bist_app` rolleri, migration 001–005, imaj, ilk veri (30 hisse × 250 gün).
- Cron kuruldu. `CRON_TZ` bu sunucuda (cron 3.0pl1) çalışmadı; satırlar Berlin saatiyle iki olası saate konup `ONLY_IST_HOUR` ile İstanbul saati kontrol edildi.
- Ham fiyat doğrulandı: `adjust=False` bedelsizi geri alıyor (BIMAS, FROTO örnekleri).
- Üç ön-kayıtlı deney (E1–E3): Brier baz oranı geçemedi; IC ≈ 0,037 ama maliyetten sonra kullanılamıyor.

**Kararlar / neden:**
- PostgreSQL (`postgres-ortak` içinde ayrı DB) — append-only garantisi veritabanında; SQLite değil.
- Evren tarihli üyelik (hayatta kalma yanlılığı); geçmiş üyelik sonradan eklendi.
- Canlı defter sıfırdan başlar; geçmiş tahminler uydurulmaz.

**Açık sorunlar:**
- `CRON_TZ` desteklenmiyor: çözüm geçici, dokümante.

---

## Kayıt şablonu

```markdown
## YYYY-AA-GG
**Yapılanlar:** ...
**Kararlar / neden:** ...
**Açık sorunlar:** ...
**Sıradaki adım:** ...
```
