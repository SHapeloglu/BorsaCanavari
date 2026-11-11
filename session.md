# session.md — BIST Tahmin Laboratuvarı: oturum günlüğü

Yeni kayıtlar en üste. Her kayıt: yapılanlar, kararlar ve nedeni, açık sorunlar, sıradaki adım. Sunucu değişiklikleri ayrıca `/opt/postgres-ortak/session.md` (ortak servis) ve `task.md`'de de izlenir.

---

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
