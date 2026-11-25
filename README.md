# BIST Tahmin Laboratuvarı

BIST 30 için 5 işlem günü yön olasılığı üreten, tahminleri gerçekleşenle karşılaştıran ve modeli yalnızca kanıtlanmış iyileşmeyle terfi ettiren bir **ölçüm ve öğrenme laboratuvarı**.

> Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır.

Tasarımın tek kaynağı [`PROJE.md`](PROJE.md). Mimari için [`architect.md`](architect.md), çalışma kuralları için [`CLAUDE.md`](CLAUDE.md), güncel işler için [`task.md`](task.md), fikirler için [`backlog.md`](backlog.md), oturum günlüğü için [`session.md`](session.md).

## Durum (2026-11-25)

- **Canlı:** Contabo sunucusunda günlük akış (19:00 İstanbul) çalışıyor. 24 Kasım'dan beri `bist-tahmin:latest` güncel koddan (`b549f0d`, `b3b284670391`); önceki holdout imajı `prev-20261124`; 19, 20 ve 23 Kasım koşuları çıkış 0 ve 150'şer tahmin yazdı (hafta sonu koşu yok). 21 Kasım haftalık rapor üretildi. 11 ve 12 Kasım kontrollerinin sonucu kayıtlı değil (bkz. task.md).
- **Sağlık:** Sağlık cron'u 20:30'da çalışıyor (19 Kasım'dan beri); `bist health` SAGLIKLI, devre kesici kapalı.
- **Modeller:** Altyapı hazır (kayıt, eğitim, terfi kontrolü, champion koruması), ama **champion yok**. Terfi bilerek manuel ve kanıt gerektiriyor.
- **Araştırma:** Üç ön-kayıtlı deney ve `faz35-v2` (E4–E6) koşuldu; hiçbir model baz oranı Brier'da geçmedi. Hisseler arası küçük bir sıralama sinyali var; maliyetten sonra kullanılamıyor.
- **Açık:** 25 Kasım 19:00 koşusunun ve 20:30 sağlık kontrolünün sonucu; sunucu diski %92 (bkz. task.md). 24 Kasım akşam koşusu çıkış 0 ve 150 tahmin, 20:30 sağlık SAGLIKLI (2026-11-25 kontrolü). `horizon_unknown` atlamaları açıklandı: vadesi henüz dolmamış tahminler, beklenen davranış.

## Kurulum (yerel)

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

Tam takım, çok sayıda geçici veritabanı oluşturduğu için geçici dizini büyük bir diske alın:

```bash
TMPDIR=$HOME/.cache/bist-pytest-tmp .venv/bin/python -m pytest -p no:cacheprovider -o addopts=""
```

Ağ testleri (gerçek borsapy) varsayılan olarak atlanır; `BIST_NETWORK_TESTS=1` ile çalışır.

Docker'lı makinede gerçek Postgres 16 ile: `docker compose -f docker-compose.test.yml up -d`, sonra `BIST_TEST_ADMIN_URL=postgresql://postgres:test@127.0.0.1:54329/postgres pytest`.

## Veritabanı kurulumu

`bist init`, `DATABASE_ADMIN_URL` (yönetici) ve `BIST_APP_PASSWORD` (yalnızca `bist_app` rolü ilk kez oluşturulurken) ortam değişkenlerini kullanır. İdempotenttir; önce `--dry-run` ile ne yapacağını gösterir.

```bash
cp .env.example .env     # değerleri doldur; .env commit edilmez
set -a; source .env; set +a
bist init --dry-run
bist init
```

Roller: `bist_owner` (NOLOGIN, şema sahibi), `bist_app` (uygulama; tahmin alanlarını güncelleyemez, silemez). Ayrıntı: PROJE.md Bölüm 6 ve 19.

## CLI

| Komut | İşlev |
|---|---|
| `bist init [--dry-run]` | Roller, veritabanı ve migration'lar (idempotent) |
| `bist universe sync` / `load-history` | Güncel evren / tarihli geçmiş üyelik |
| `bist fetch --since YYYY-MM-DD [--tickers A,B]` | Fiyat ve kurumsal işlemler, kalite kapısı |
| `bist predict daily` | Baseline tahminleri (yalnızca takvimin son günü) |
| `bist resolve` | Vadesi gelen tahminleri çöz |
| `bist run-daily` | Günlük akış (cron): evren → indir → çöz → tahmin → izleme |
| `bist health` | Sağlık kontrolü (0 sağlıklı, 1 uyarı, 2 kritik) |
| `bist breaker status` / `reset --reason` | Devre kesici |
| `bist report weekly` / `baseline report` | Markdown rapor (veritabanına yazmaz) |
| `bist backtest build-dataset` / `run` | Tarihsel deney (sınama kümesi kilitli) |
| `bist models list / train / promote-check / promote / retire` | Model yaşam döngüsü (`promote` `--confirm` ister) |

Yazan komutların çoğu `--dry-run` destekler. Veritabanı bağlantısı `DATABASE_URL` (bist_app), yönetici işleri için `DATABASE_ADMIN_URL` ortam değişkenlerinden okunur; `.env.example` şablondur, gerçek `.env` commit edilmez.

## Sunucu dağıtımı

Konteyner, cron ve veritabanı kurulumu için [`PROJE.md` Bölüm 19](PROJE.md) ve [`architect.md`](architect.md). Dağıtım: `git archive <dal> | ssh contabo 'tar -x -C /opt/bist-tahmin'`, ardından sunucuda `docker build -t bist-tahmin:latest .`. Sunucuya yazan her adım önce kuru çalıştırmayla doğrulanır.

## Dallar

`main` dalı yok. Çalışma dalı `faz-4-yasam-dongusu`. Önceki fazların dalları (`faz-0-1-iskelet-veri`, `faz-2-defter`, `faz-3-5-lab`) faz-4'e birleşti ve uzaktan silindi. Sunucu `git archive <sha>` ile dağıtır; dal kullanmaz. `deploy-splits-fix` uzaktan silindi (2026-11-25); tek commit'i (`ff60a45`, splits önbelleği) faz-4'te `cab326c` olarak var, yerel `deploy-splits-fix` dalı arşiv olarak duruyor.
