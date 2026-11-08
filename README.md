# BIST Tahmin Laboratuvarı

BIST 30 için 5 işlem günü yön olasılığı üreten, tahminleri gerçekleşenle karşılaştıran ve modeli yalnızca kanıtlanmış iyileşmeyle terfi ettiren bir **ölçüm ve öğrenme laboratuvarı**.

> Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır.

Tasarımın tek kaynağı [`PROJE.md`](PROJE.md). Kod yazmadan önce onu okuyun.

## Durum

Faz 0 (iskelet): veritabanı kurulumu (`bist init`), şema, roller, append-only tetikleyicileri, yapılandırma, testler.

## Yerel kurulum ve test

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest            # Docker yoksa pgserver ile gömülü Postgres kullanılır
```

Docker'lı makinede gerçek Postgres 16 ile:

```bash
docker compose -f docker-compose.test.yml up -d
BIST_TEST_ADMIN_URL=postgresql://postgres:test@127.0.0.1:54329/postgres pytest
```

## Veritabanı kurulumu

`bist init`, `DATABASE_ADMIN_URL` (yönetici) ve `BIST_APP_PASSWORD` (yalnızca `bist_app` rolü ilk kez oluşturulurken) ortam değişkenlerini kullanır. İdempotenttir; önce `--dry-run` ile ne yapacağını gösterir.

```bash
cp .env.example .env     # değerleri doldur; .env commit edilmez
set -a; source .env; set +a
bist init --dry-run
bist init
```

Roller: `bist_owner` (NOLOGIN, şema sahibi), `bist_app` (uygulama; tahmin alanlarını güncelleyemez, silemez). Ayrıntı: PROJE.md Bölüm 6 ve 19.
