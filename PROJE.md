# BIST Tahmin Karşılaştırma ve Öğrenen Model Projesi

Bu dosya projenin tek kaynak gerçeğidir (single source of truth). Bu projede çalışan her oturum, kod yazmadan önce bu dosyayı baştan sona okumalı. Bir karar değişirse önce bu dosya güncellenir, sonra kod.

Sürüm: v2 (2026-11-08). Değişiklikler en altta, "Değişiklik günlüğü" bölümünde.

---

## 0. Ana hedef ve başarı tanımı

**Ana hedef:** BIST 30 için 5 işlem günü yön olasılığı üreten modelin başarısını sistematik ve ölçülebilir biçimde artırmak. Başarı, basit baseline'ların üzerindeki iyileşmeyle ölçülür. Sahte iyileşmeyi (aşırı uyum, sızıntı, deneme şişmesi) elemek hedefin ayrılmaz parçasıdır.

**Başarının ölçütleri (öncelik sırasıyla):**

| Ölçüt | Kural |
|---|---|
| Ana | Brier skorunda "geçmiş yükseliş oranı" baseline'ına karşı iyileşme. Eşleştirilmiş (paired) blok bootstrap güven aralığı sıfırı dışlamalı. |
| İkinci | Günlük Spearman IC (tahmin sırası ile gerçekleşen getiri sırası) ortalaması ve Newey-West t-istatistiği. |
| Üçüncü | Maliyet sonrası, işlem zamanlaması gerçekçi simüle getiri; eşit ağırlıklı BIST 30 benchmark'ını ve champion'ı geçmeli. |
| Yardımcı | Kalibrasyon, log loss, accuracy (tek başına karar vermez). |

**Gerçekçi beklenti (2026-11-08 ölçümü, 30 hisse, 2019-07 → 2026-11):**
- 5 günlük yükseliş oranı %54,7; yıllara göre %48,8 ile %62,3 arasında oynuyor. Sabit tahminin Brier'ı 0,2478, yani rastgeleye (0,25) çok yakın. İyileşme payı dar.
- Hisseler arası ortalama günlük korelasyon 0,42. Günlerin %38,6'sında hisselerin >%80'i ya da <%20'si aynı yöne gidiyor. Bir günün 30 etiketi kabaca 2–3 bağımsız gözlem değerinde.
- Etiketin ardışık gün otokorelasyonu 0,60 (5 günlük pencereler çakışıyor).
- Sonuç: küçük ama kanıtlanmış iyileşmeler başarıdır. **Hiçbir modelin baseline'ı geçememesi de geçerli ve dürüstçe raporlanacak bir sonuçtur.**

**Kanıt hiyerarşisi:** Tarihsel backtest yalnızca aday elemek içindir. Bir iyileşmenin "gerçek" sayılması için (1) dokunulmaz sınama kümesinde ve (2) canlı ileriye dönük defterde doğrulanması gerekir.

---

## 1. Amaç

BIST 30 hisseleri için günlük olarak kısa vadeli yön tahmini (5 işlem günü ufku) üretmek; her tahmini gerçekleşen sonucuyla karşılaştırmak; bu karşılaştırmadan elde edilen hata sinyaliyle modeli periyodik olarak yeniden eğitmek ve yalnızca kanıtlanmış iyileşmeleri canlı tahmin sürecine terfi ettirmek.

Sistem bir **ölçüm ve öğrenme laboratuvarıdır**. Kesin fiyat tahmini yapmaz, alım satım emri göndermez, kişisel yatırım tavsiyesi vermez.

## 2. Kapsam dışı (bilerek yapılmayacaklar)

- Gerçek para ile canlı emir gönderme (broker entegrasyonu yok)
- Kesin fiyat tahmini (yalnızca yön olasılığı ve sıralama skoru üretilir)
- Kişiye özel yatırım tavsiyesi veya "al/sat" önerisi
- Derin öğrenme (ilk sürümde lojistik regresyon ve gradient boosting yeterli)
- BIST 30 dışı evrenler (ilk sürüm yalnızca BIST 30; genişleme Faz 5)
- Gerçek zamanlı (intraday) tahmin; tahmin günlük kapanıştan sonra üretilir
- Kısa pozisyon simülasyonu (BIST'te açığa satışın uygulanabilirliği doğrulanmadı; simülasyon yalnızca uzun pozisyondur)

## 3. Temel kararlar (değiştirmeden önce bu bölüme bakılmalı)

| Konu | Karar | Gerekçe |
|---|---|---|
| Dil | Python 3.11+ (sunucuda 3.12) | borsapy ve veri bilimi ekosistemi |
| Veri kaynağı | borsapy (ana), yfinance (yedek) | BIST için yfinance benzeri API. Sunucudan test edildi (Bölüm 4). |
| Depolama | **PostgreSQL 16**: `postgres-ortak` içinde ayrı `bist_tahmin` veritabanı (*varsayılan, kullanıcı onayı bekliyor, Bölüm 17*) | Append-only kuralı veritabanında zorlanır (rol yetkisi + tetikleyici); gece yedeği hazır; Power BI doğrudan bağlanır. |
| Yerel geliştirme/test | Geçici Postgres konteyneri (docker) | Tetikleyici ve yetki testleri SQLite'ta aynı sonucu vermez. |
| Çalıştırma | Docker Compose + host cron, sürekli çalışan servis yok | Sunucudaki diğer işlerin deseniyle aynı; boşta RAM kullanmaz. |
| Zaman dilimi | Uygulama ve cron **Europe/Istanbul** (sunucu Europe/Berlin) | Berlin DST'si İstanbul saatini kaydırır. |
| Evren | BIST 30, **tarihli üyelik tablosuyla** | Çeyreklik değişiklikler; hayatta kalma yanlılığını önler. |
| Tahmin ufku | 5 işlem günü (XU100 takvimine göre sayılır) | Gürültü ve maliyet dengesi |
| Çıktı | `prob_up` (0..1, [0,01; 0,99] aralığına kırpılır) ve `score` (günlük sıralama skoru) | Kesin fiyat yerine yön olasılığı ve göreli sıralama |
| Ana etiket | `label_up`: mutlak yön (kapanıştan kapanışa). Yardımcı: `label_rel_up`: XU100'e göre göreli yön | Mutlak yön ağırlıkla piyasa yönünü ölçer |
| İlk model | Düzenlileştirilmiş lojistik regresyon, hisseler arası havuzlanmış, özellikler günlük hisseler arası normalize | Yorumlanabilir, az veriyle çalışır |
| Eğitim şeması | Walk-forward (genişleyen pencere) **+ 5 işlem günü purge** | Etiket çakışmasından doğan sızıntıyı önler |
| Terfi kuralı | Champion/challenger, gölge modda canlı, eşleştirilmiş blok bootstrap | Model rastgele değişmesin |
| Sınama kümesi | Son ~6 ay dokunulmaz (`holdout_start`) | Deneme şişmesine karşı |
| LLM | Opsiyonel, yerel (Ollama) | Maliyet sıfır; yorum/özet için |
| Lisans | Yalnızca kişisel ve eğitim kullanımı | borsapy kullanım koşulları veriyi ticari kullanıma kapatıyor |

## 4. Veri kaynakları ve bilinen kısıtlar

**borsapy** (0.11.0 ile 2026-11-08'de Contabo sunucusundan test edildi)
- `bp.Ticker("THYAO").history(period="5y")` günlük OHLCV verir (`period="5y"` ≈ 1825 işlem günü, ~7 yıl). `bp.Index("XU100").history(...)` endeks verir.
- `bp.Index("XU030").component_symbols` güncel BIST 30 bileşenlerini verir. 2026-11-08'de 30 hisse; 4. çeyrek değişikliğiyle uyumlu (TRMET dahil, DSTKF yok). **KAP duyurusuyla çapraz kontrol edilir.**
- `history()` varsayılan çıktısı bölünme/bedelsiz düzeltmelidir (7 yılda −%35'ten büyük günlük düşüş yok), temettü düzeltmesi yoktur. Parametreler: `period, interval, start, end, actions, adjust=True, auto_adjust=False`.
- **Ham fiyat doğrulandı (Faz 1, 2026-11-08):** `adjust=False`, düzeltmeyi İş Yatırım `splits` tablosundaki bedelsiz oranlarıyla geri alarak ham fiyat verir. Örnekler: BIMAS 2026-05-14 (%100 bedelsiz) ham 813,00 → 414,00 (−%49,1), düzeltmeli 406,50 → 414,00 (+%1,85); FROTO 2025-05-07 (%900) ham −%90,1, düzeltmeli −%0,57. 30 hissenin tüm geçmişinde bölünme günlerinde ham seride sıçrama var, düzeltmelide yok. Bedelli sermaye artırımları (`RightsIssue`) düzeltmeye girmiyor.
- **Sessiz hata riski:** `splits` alınamazsa borsapy hata vermeden düzeltmeli seriyi "ham" diye döndürebilir. `BorsapySource` bu yüzden `splits`'i kendisi çeker, alamazsa `SourceError` verir ve ham/düzeltmeli oranını bedelsiz olaylarıyla karşılaştırır (`check_raw_vs_adjusted`).
- **Sütun sözleşmesi (`prices`):** `close` ham, `close_adj` düzeltmeli; `open/high/low/volume` ham serinin değerleri. `open_adj = open × close_adj / close` (migration 004, üretilmiş sütun): bedelsiz gün sınırında olduğundan oran gün içinde sabittir. `tradable_ret` (Faz 2) ham açılışla düzeltmeli kapanışı **karıştırmamalı**; `open_adj` kullanılır (örnek: BIMAS 2026-05-14 `open_adj` 411,50, bir önceki gün 393,25).
- Kaynakta `period="max"` en fazla 3650 bar (~14,5 yıl) verir.
- **Düzeltme geçmişi sonradan değiştirir:** düzeltmeli fiyattan türeyen özellikler yeni bir bölünmeden sonra farklı çıkar. Bu yüzden tahmin anındaki özellikler `features_json` olarak kaydedilir (Bölüm 6).
- Günlük barların zaman damgası `09:00+03:00`'dır (kapanış saati değil). Tarihe normalize edilir (`tz_localize(None).normalize()`).
- Kapanıştan önce çalışırsa bugünün **yarım barı** gelir. Bugünün barı, kapanış + gecikme penceresi geçmeden kabul edilmez.
- BIST fiyatları TradingView üzerinden gelir; **varsayılan olarak yaklaşık 15 dakika gecikmelidir**. Günlük kapanış verisi için kabul edilebilir, canlı karar için kullanılamaz.
- TCMB EVDS verileri (USD/TRY, TÜFE vb.) için ücretsiz anahtar gerekir: `bp.set_evds_key(...)`. Anahtar **asla koda gömülmez**; ortam değişkeni (`EVDS_KEY`).
- Bazı hisselerin geçmişi kısadır (örn. en geç başlayan hissede ilk geçerli tarih 2023-01-18). Minimum geçmiş kuralı uygulanır.
- Veriler üçüncü taraf sağlayıcılara aittir (İş Yatırım, TradingView, TCMB). Borsa İstanbul verinin dağıtımını lisansa bağlar; **ham fiyat serileri herkese açık yayımlanmaz**, yalnızca türetilmiş metrikler.

**İş Yatırım:** Contabo sunucusundan `www.isyatirim.com.tr` ana sayfası **zaman aşımına uğruyor** (2026-11-08), ama borsapy'nin `splits` verisi (ham/düzeltmeli fiyat için gerekli) aynı sunucudan çalışıyor (BIMAS testi). Bilanço/tarama uç noktaları (Faz 5) sunucudan **denenmedi**; denenene kadar kapsam dışı varsayılır.

**yfinance (yedek)**
- BIST için sembol sonuna `.IS` eklenir. Varsayılan `curl` User-Agent'ıyla sunucudan 429 döndü; tarayıcı User-Agent'ıyla veri geldi. Resmi API değildir, kesinti/hız sınırı olabilir. Yalnızca çapraz kontrol ve yedek olarak kullanılır.

**Veri kalite kontrolleri (her indirmede zorunlu)**
- Tarih sıralaması monoton artan, tekrar eden tarih yok.
- Kapanış fiyatı sıfır veya negatif olmamalı.
- Ardışık günler arasında aşırı sıçrama (±%35 üzeri) işaretlenmeli, otomatik silinmemeli (BIST günlük fiyat limiti nedeniyle gerçek sıçrama sınırlıdır; sıçrama çoğunlukla veri/düzeltme hatasıdır).
- **İşlem günü takvimi:** XU100'de bar olan günler esas alınır; statik resmi tatil listesiyle çapraz kontrol edilir (yarım günler dahil).
- Hacmi sıfır olan ya da işlem durdurulan gün/hisse işaretlenir.
- Eksik veri olan hisse o gün tahmin evreninden çıkarılır ve bu kayıt loglanır.
- (Opsiyonel) borsapy ile yfinance kapanışları arasında tolerans dışı fark loglanır.

## 5. Veri sızıntısı kuralları (en kritik bölüm)

Bir tahmin, yalnızca `made_at` gününün kapanışına kadar bilinen bilgilerle üretilebilir. Bunu ihlal eden her özellik reddedilir.

- Özellik üretimi `veri.index.date <= made_at` filtresinden geçmeden çalışmaz.
- Hareketli ortalama, RSI, volatilite gibi göstergeler yalnızca geçmiş pencereyle hesaplanır (`center=True` kullanılmaz).
- Normalizasyon (z-skor vb.) parametreleri tüm veri üzerinden değil, yalnızca eğitim penceresi üzerinden fit edilir. Günlük hisseler arası normalizasyon yalnızca o günün verisini kullanır.
- **Özellik seçimi ve hiperparametre ayarı fold içinde yapılır**; tüm veriye bakarak seçim yapılamaz.
- **Purge/embargo:** Walk-forward eğitim ve backtestte, test günü `t` için eğitim satırlarının etiket penceresi `t`'yi kesmemeli. Eğitim kümesinin son satırı en geç `t − horizon_days` işlem günüdür (5 işlem günü boşluk).
- Bilanço verileri, yayınlandıkları tarihten itibaren kullanılır, dönem sonundan itibaren değil.
- KAP duyuruları yalnızca yayın zamanından sonra kullanılabilir.
- Eğitim ve test ayrımı rastgele değil, zamana göre yapılır.
- Etiket (`label_up`), yalnızca `made_at + horizon` gününün kapanışı bilindikten sonra hesaplanır.
- **İşlem zamanlaması:** Tahmin kapanıştan sonra üretildiği için işlem en erken ertesi gün yapılabilir. Simüle getiri `made_at + 1` açılışından başlar (Bölüm 11). Ölçüm etiketi (kapanıştan kapanışa) ile işlem getirisi ayrı alanlardır.
- **Sınama kümesi kilidi:** `holdout_start` sonrası veri, backtest araçlarına varsayılan olarak verilmez. Erişim yalnızca terfi aşamasında açık bayrakla (`--use-holdout`) yapılır ve loglanır.

Bu kurallar için birim testleri yazılmalı (bkz. Bölüm 13).

## 6. Veri modeli

PostgreSQL 16, veritabanı `bist_tahmin`, şema `public`. Aşağıdaki DDL tasarım niyetini gösterir; asıl migration dosyaları `sql/` altında tutulur ve testlerle doğrulanır.

```sql
-- Günlük fiyatlar (yeniden üretilebilir kaynak veri)
CREATE TABLE prices (
    ticker     TEXT NOT NULL,
    date       DATE NOT NULL,
    open       DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close      DOUBLE PRECISION,        -- ham kapanış (adjust=False; Faz 1'de doğrulandı)
    close_adj  DOUBLE PRECISION,        -- düzeltmeli kapanış (varsayılan çıktı)
    -- open_adj: migration 004'te üretilmiş sütun = open * close_adj / close
    volume     DOUBLE PRECISION,
    source     TEXT NOT NULL,           -- 'borsapy' | 'yfinance'
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticker, date)
);

-- Bölünme/temettü kayıtları (düzeltme geçmişini izlenebilir kılar)
CREATE TABLE corporate_actions (
    ticker     TEXT NOT NULL,
    ex_date    DATE NOT NULL,
    kind       TEXT NOT NULL,           -- 'split' | 'dividend'
    value      DOUBLE PRECISION NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ticker, ex_date, kind)
);

-- Tarihli evren üyeliği (hayatta kalma yanlılığına karşı)
CREATE TABLE universe_membership (
    ticker      TEXT NOT NULL,
    index_name  TEXT NOT NULL DEFAULT 'BIST30',
    valid_from  DATE NOT NULL,
    valid_to    DATE,                   -- NULL = hâlâ üye
    PRIMARY KEY (ticker, index_name, valid_from)
);

-- İşlem günü takvimi (XU100 barlarından türetilir)
CREATE TABLE trading_calendar (
    date       DATE PRIMARY KEY,
    is_half_day BOOLEAN NOT NULL DEFAULT FALSE
);

-- Tahmin defteri: append-only, tahmin anındaki alanlar DEĞİŞTİRİLEMEZ
CREATE TABLE predictions (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    made_at         DATE NOT NULL,         -- tahminin yapıldığı gün (kapanış)
    horizon_days    INTEGER NOT NULL,      -- 5
    prob_up         DOUBLE PRECISION NOT NULL CHECK (prob_up BETWEEN 0 AND 1),
    score           DOUBLE PRECISION,      -- günlük sıralama skoru
    model_version   TEXT NOT NULL,         -- örn. "logreg_v1@2026-11-08" veya "baseline_base_rate"
    model_role      TEXT NOT NULL,         -- 'champion' | 'challenger' | 'baseline'
    feature_hash    TEXT NOT NULL,         -- features_json'un hash'i
    features_json   JSONB NOT NULL,        -- tahmin anındaki özellik vektörü (değişmez)
    created_at      TIMESTAMPTZ NOT NULL,  -- kaydın yazıldığı gerçek zaman
    -- Çözüm alanları (sonradan doldurulur, tek seferlik)
    resolved_at      DATE,
    realized_ret     DOUBLE PRECISION,     -- kapanıştan kapanışa getiri (made_at -> made_at+H)
    realized_rel_ret DOUBLE PRECISION,     -- aynı dönem, XU100'e göre fark
    tradable_ret     DOUBLE PRECISION,     -- made_at+1 açılış -> made_at+H kapanış
    label_up         INTEGER,              -- 1 ise yükseliş, 0 ise değil
    label_rel_up     INTEGER,              -- 1 ise XU100'ü geçti
    UNIQUE (ticker, made_at, horizon_days, model_version)
);

-- Model kayıtları
CREATE TABLE models (
    version         TEXT PRIMARY KEY,
    trained_at      TIMESTAMPTZ NOT NULL,
    train_end       DATE NOT NULL,         -- eğitimde kullanılan son gün (purge sonrası)
    params_json     JSONB NOT NULL,
    role            TEXT NOT NULL          -- 'champion' | 'challenger' | 'retired'
);

-- Deneme sayacı (p-hacking takibi)
CREATE TABLE experiments (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    family          TEXT NOT NULL,         -- çoklu deneme düzeltmesi için aile (son champion'dan beri)
    description     TEXT NOT NULL,
    params_json     JSONB NOT NULL,
    result_json     JSONB
);

-- İş günlüğü ve çift çalışma kilidi
CREATE TABLE runs (
    id          BIGSERIAL PRIMARY KEY,
    job         TEXT NOT NULL,             -- 'fetch' | 'resolve' | 'predict' | 'report' | ...
    run_date    DATE NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL,             -- 'running' | 'ok' | 'failed' | 'skipped'
    detail      JSONB,
    UNIQUE (job, run_date, started_at)
);
```

**Roller ve yetkiler**
- `bist_owner`: şema sahibi, yalnızca migration için kullanılır.
- `bist_app`: uygulama rolü. `predictions` üzerinde `INSERT`, `SELECT` ve **yalnızca çözüm sütunlarında** `UPDATE` (`resolved_at, realized_ret, realized_rel_ret, tradable_ret, label_up, label_rel_up`). `DELETE` ve `TRUNCATE` yoktur.
- Sunucudaki "tüm projeler aynı uygulama kullanıcısını kullanır" kuralının **bilinçli istisnası**: aynı kullanıcı append-only garantisini anlamsız kılar.
- `predictions` üzerinde `BEFORE UPDATE` tetikleyicisi: değişmez alanlardan biri değişirse ya da `resolved_at` zaten doluysa hata verir. `BEFORE DELETE` ve `BEFORE TRUNCATE` tetikleyicileri her zaman hata verir.

**Kurallar**
- `predictions` tablosunda tahmin alanları (`prob_up`, `score`, `feature_hash`, `features_json`, `made_at`, `model_version`) yazıldıktan sonra **UPDATE edilmez**. Yalnızca çözüm alanları bir kez doldurulur.
- Her model eğitimi ve her deneme `experiments` tablosuna yazılır.
- Yazma işlemleri idempotenttir: `INSERT ... ON CONFLICT DO NOTHING` ve iş başına advisory lock.

## 7. Proje yapısı

```
bist-tahmin/
├── PROJE.md                  # bu dosya
├── README.md                 # kısa kurulum ve kullanım
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml        # üretim (ortak-db ağına bağlı)
├── docker-compose.test.yml   # yerel test Postgres'i
├── .env.example              # EVDS_KEY=, DATABASE_URL=  (gerçek değerler commit edilmez)
├── config/
│   └── default.yaml          # evren, ufuk, maliyet, eşikler
├── data/                     # .gitignore'da (yedek çıktıları vb.)
├── logs/                     # .gitignore'da
├── src/bist_tahmin/
│   ├── __init__.py
│   ├── config.py             # yaml okuma, doğrulama
│   ├── db.py                 # roller, veritabanı, migration (init); advisory lock (Faz 2)
│   ├── sql/                  # migration dosyaları: 001_schema, 002_guard, 003_grants, 004_open_adj, 005_ledger_hardening (pakete dahil)
│   ├── calendar.py           # işlem günü takvimi, ufuk sayımı
│   ├── data/
│   │   ├── source.py         # veri kaynağı soyutlaması (borsapy/yfinance)
│   │   ├── fetch.py          # indirme
│   │   ├── universe.py       # XU030 bileşenleri, tarihli üyelik
│   │   └── quality.py        # veri kalite kontrolleri
│   ├── features/
│   │   └── build.py          # sızıntısız özellik üretimi
│   ├── baselines/
│   │   └── rules.py          # hep-yukarı, dün-tekrar, rastgele, MA, geçmiş-oran
│   ├── models/
│   │   ├── train.py          # walk-forward eğitim (purge'lu)
│   │   └── registry.py       # champion/challenger yönetimi
│   ├── backtest/
│   │   └── lab.py            # tarihsel walk-forward deney laboratuvarı
│   ├── predict/
│   │   └── daily.py          # günlük tahmin üretimi ve kayıt
│   ├── resolve/
│   │   └── resolver.py       # vadesi gelen tahminleri çözme
│   ├── evaluate/
│   │   ├── metrics.py        # brier, log loss, kalibrasyon, IC
│   │   ├── bootstrap.py      # tarihe göre blok bootstrap, eşleştirilmiş fark
│   │   ├── portfolio.py      # kohort simülasyonu, benchmark
│   │   ├── costs.py          # komisyon ve spread duyarlılık tablosu
│   │   └── report.py         # haftalık rapor
│   └── cli.py                # komut satırı girişi
└── tests/
    ├── conftest.py           # gerçek Postgres: BIST_TEST_ADMIN_URL ya da gömülü pgserver
    ├── test_config.py        # (Faz 0)
    ├── test_init.py          # (Faz 0)
    ├── test_append_only.py   # (Faz 0)
    ├── test_leakage.py
    ├── test_purge.py
    ├── test_resolver.py
    ├── test_metrics.py
    ├── test_bootstrap.py
    ├── test_portfolio.py
    └── test_quality.py
```

## 8. Yapılandırma (config/default.yaml)

```yaml
universe:
  name: BIST30
  source: borsapy_XU030     # bileşenler borsapy'den okunur, KAP ile çapraz kontrol edilir
  tickers: []               # çalışma anında doldurulur; tarihli üyelik DB'de tutulur

timezone: Europe/Istanbul
schedule:
  daily_run_time: "19:00"   # kapanış seansı + 15 dk veri gecikmesi sonrası; yeniden deneme var

horizon_days: 5
label_threshold: 0.0        # yön etiketi: getiri > threshold ise 1
prob_clip: [0.01, 0.99]

costs:
  commission_bps: null      # GERÇEK ORAN DOĞRULANACAK; null iken rapor "maliyetsiz/tahmini" yazar
  spread_bps: null
  sensitivity_grid_bps: [0, 10, 20, 40]   # rapor her zaman bu ızgarayı gösterir

training:
  min_train_days: 500
  retrain_every_days: 20
  purge_days: 5             # = horizon_days

holdout:
  holdout_start: "2026-05-01"   # dokunulmaz sınama kümesi başlangıcı (sabit; değiştirilirse gerekçe yazılır)

terfi:
  min_resolved_dates: 60          # farklı made_at günü sayısı (tahmin sayısı değil)
  min_improvement_brier: 0.005    # champion'a göre, eşleştirilmiş fark
  bootstrap: {kind: block_by_date, resamples: 2000, block_days: 10, ci: 0.95}
  multiple_testing: bonferroni    # alpha = 0.05 / (aynı ailedeki deneme sayısı)
  require_holdout_pass: true
  require_live_shadow_days: 60    # challenger canlı defterde en az bu kadar gün gölge çalışmış olmalı
  min_improvement_net_return: 0.0 # maliyet sonrası, benchmark'a göre fazla getiri champion'dan büyük

drift:
  rolling_window: 60
  brier_over_base_rate_alert: 0.01  # modelin Brier'ı - geçmiş-oran baseline'ının Brier'ı bu değeri aşarsa alarm

circuit_breaker:
  max_relative_drawdown: 0.10      # simüle strateji / eşit ağırlıklı BIST30 oranının zirveden düşüşü
```

Bu değerler başlangıç varsayımlarıdır. Değiştirilirse gerekçe `experiments` tablosuna yazılır.

## 9. Özellikler (ilk sürüm)

Yalnızca fiyat tabanlı, sızıntısız göstergeler. **Tüm özellikler hem ham hem günlük hisseler arası normalize (z-skor/rank) biçimde üretilir.**

- Getiriler: 1, 5, 10, 20 gün
- Hareketli ortalama oranları: fiyat / MA(5), MA(20), MA(50) − 1
- Volatilite: 10 ve 20 günlük getiri standart sapması
- RSI(14)
- Hacim oranı: hacim / 20 günlük ortalama hacim
- **Göreli güç:** hissenin 5/20 günlük getirisi eksi XU100'ün aynısı
- **Piyasa bağlamı:** XU100'ün aynı göstergeleri; **piyasa genişliği** (BIST 30'da MA(20) üstündeki hisse oranı)
- Takvim: haftanın günü, resmi tatil öncesi/sonrası bayrağı

Sonraki sürümde (Faz 3.5 deneyleri, ayrı karar): USD/TRY ve faiz değişimleri (TCMB EVDS). Bilanço oranları ve KAP duyuru sayıları İş Yatırım erişimi çözülmeden planlanmaz.

Her özellik üretimi `feature_hash` üretir. Aynı tahmin tekrar üretilirse hash değişmemelidir; değişirse bu bir hata sinyalidir. Hash, `features_json`'dan hesaplanır, geçmiş fiyatların sonradan düzeltilmesinden etkilenmez.

## 10. Baseline'lar (ML'den önce uygulanır)

Her baseline aynı tahmin defterine (`model_role='baseline'`) yazılır ve aynı şekilde çözülür. Model bu baseline'ları maliyet sonrası geçemezse yayına alınmaz. Tüm olasılıklar `prob_clip` aralığına kırpılır.

1. **Hep yükseliş:** `prob_up = 0.99` (kırpılmış; log loss sonsuz olmasın)
2. **Dün tekrar:** dünkü getiri pozitifse `prob_up = 0.6`, değilse `0.4`
3. **Rastgele (sabit tohum):** `prob_up = 0.5` ya da tohumlu rastgele değer
4. **MA kuralı:** fiyat MA(20) üstündeyse `prob_up = 0.6`, altındaysa `0.4`
5. **Geçmiş yükseliş oranı (asıl çıta):** `prob_up` = o güne kadar **çözülmüş** (etiketi bilinen) örneklerde son N günlük yükseliş oranı. Etiketi henüz bilinmeyen satırlar kullanılmaz. Brier karşılaştırmasının referansı budur.

Baseline sonuçları haftalık raporda ML modeliyle yan yana gösterilir.

## 11. Değerlendirme

Metrikler (her rapor döneminde):

- **Brier skoru:** olasılık kalitesi (rastgele = 0.25; sabit tahmin ≈ 0.248). Ana ölçüt, geçmiş-oran baseline'ına göre fark.
- **Günlük Spearman IC** (tahmin sırası ile gerçekleşen getiri sırası) ve ortalaması, Newey-West t-istatistiği.
- **Log loss**, **accuracy** (yalnızca referans).
- **Kalibrasyon tablosu:** tahmin olasılık kovaları ile gerçekleşen oran. Baz oran kayması ayrıca izlenir.
- **Örneklem sayısı:** her metrik yanında hem tahmin sayısı hem **farklı gün sayısı** yazılır.
- **Güven aralığı:** tarihe göre **blok bootstrap** (aynı günün tüm hisseleri birlikte, günler `block_days` uzunluğunda bloklar halinde yeniden örneklenir; en az 1000, varsayılan 2000). Modeller arası farklar **eşleştirilmiş** (aynı gün, aynı hisse) hesaplanır.

**Maliyet sonrası simülasyon (kohort portföyü):**
- Her gün `score` sıralamasına göre en üst dilim (varsayılan en üst %20) uzun alınır; eşit ağırlıklı.
- Giriş `made_at + 1` açılışı, çıkış `made_at + H` kapanışı (`tradable_ret`).
- Her günlük kohort sermayenin `1/H`'sidir; çakışan pozisyonlar çift sayılmaz.
- Karşılaştırma ölçütü: aynı zamanlamayla **eşit ağırlıklı BIST 30**. Rapor, modelin benchmark'a göre fazla getirisini gösterir.
- Maliyet `costs.sensitivity_grid_bps` ızgarasının tamamı için raporlanır; oranlar doğrulanana kadar "tahmini" etiketi taşır.
- `prob_up > 0.5` kuralı kullanılmaz (baz oran %55 olduğundan neredeyse her zaman "al" der).
- Yalnızca uzun pozisyon.

**Karar kuralı:** Bir challenger ancak şu koşulların **hepsini** sağlarsa champion olur:
1. Farklı gün sayısı `terfi.min_resolved_dates` üstünde
2. Brier'da `min_improvement_brier` kadar iyileşme; eşleştirilmiş blok bootstrap güven aralığı sıfırı dışlıyor (alfa, aynı ailedeki deneme sayısına göre Bonferroni ile sıkılaştırılmış)
3. Dokunulmaz sınama kümesinde de aynı yönde iyileşme
4. Canlı defterde en az `require_live_shadow_days` gün gölge çalışmış ve orada da iyileşme
5. Maliyet sonrası benchmark'a göre fazla getiri champion'dan yüksek

## 12. Öğrenme döngüsü

Günlük (İstanbul saatiyle `schedule.daily_run_time`, bir kez; her adım `runs` tablosuna yazılır, iş başına advisory lock):
1. Veriyi indir, bugünün barının tam olduğunu doğrula (yoksa yeniden dene), kalite kontrolünden geçir
2. Evren üyeliğini ve işlem takvimini güncelle
3. Vadesi gelmiş tahminleri çöz (`resolver`)
4. Bugünün tahminlerini (champion, gölge challenger'lar) üret ve yaz (`predict daily`)
5. Baseline'ları aynı gün için kaydet
6. Drift kontrolü yap

Haftalık:
1. Rapor üret (`report weekly`)
2. Yeniden eğitim tetiklenmişse challenger eğit (`models train`, purge'lu)
3. Terfi kuralını uygula (`models promote-check`)

Devre kesici: Simüle edilmiş stratejinin eşit ağırlıklı BIST 30'a göre göreli düşüşü `circuit_breaker.max_relative_drawdown` eşiğini aşarsa ya da drift alarmı sürerse tahmin üretimi durur ve kullanıcıya rapor olarak bildirilir. Otomatik olarak yeniden başlamaz. (Mutlak düşüş kullanılmaz: piyasa düşerken model bozulmamış olabilir.)

## 13. Testler (zorunlu)

- `test_leakage.py`: Özellik üretimine `made_at` sonrası veri verildiğinde çıktı değişmemeli ya da hata vermeli. Bir tahminin özellikleri yalnızca geçmiş satırlardan türetildiği doğrulanır. Bilinçli olarak geleceğe bakan bir özellik (ör. ileri getiri) kullanan sahte model, sızıntı dedektörü tarafından yakalanır.
- `test_purge.py`: Walk-forward bölmede eğitim satırlarının etiket penceresi test gününü kesmez; eğitim son günü ≤ `t − purge_days`.
- `test_append_only.py` (gerçek Postgres'te): `bist_app` rolüyle `prob_up`/`feature_hash`/`features_json` güncellenemez, satır silinemez, TRUNCATE çalışmaz, çözüm alanları yalnızca bir kez yazılabilir.
- `test_resolver.py`: Vade gelmeden çözüm yapılamaz; çözüm bir kez yazılır; `realized_ret` ve `tradable_ret` bilinen örnekte doğru; ufuk, XU100 takvimine göre işlem günü sayar (tatil atlanır).
- `test_metrics.py`: Brier skoru bilinen değerlerle eşleşir (sabit 0.5 → 0.25); IC bilinen örnekte doğru.
- `test_bootstrap.py`: Blok bootstrap aynı günün hisselerini bölmez; tohumla tekrarlanabilir; bağımlı sentetik veride iid bootstrap'ten daha geniş aralık verir.
- `test_portfolio.py`: Kohort getirisi çift saymaz; giriş `made_at + 1` açılıştan; bilinen küçük örnekte benchmark farkı doğru.
- `test_quality.py`: Tekrar tarihleri, sıfır fiyat, tatil ve yarım bar kontrolleri doğru çalışır.

Test çalıştırma: `pytest -q` (yerelde geçici Postgres konteyneri ile). Bu testler geçmeden hiçbir faz tamamlanmış sayılmaz.

## 14. CLI

```
bist init                      # db oluştur, şema/rol/tetikleyici yükle
bist fetch --since 2024-01-01 [--tickers A,B]  # fiyat indir ve kalite kontrolü (DATABASE_URL = bist_app)
bist universe sync [--as-of D] # XU030 bileşenlerini al, tarihli üyeliği güncelle
bist predict daily [--dry-run] # takvimin son günü için baseline tahminlerini yaz (geçmişe yazmak yasak; --allow-backfill yalnız geliştirme)
bist resolve                   # vadesi gelenleri çöz (vadesi geçmiş ama çözülemeyenleri sayar)
bist run-daily                 # günlük akış: evren -> indir+kalite -> çöz -> tahmin (cron; çıkış 0/1/3/4)
bist baseline report           # baseline performansı
bist backtest run              # tarihsel walk-forward deney (sınama kümesi kilitli)
bist models train              # challenger eğit
bist models promote-check      # terfi kuralını değerlendir
bist report weekly             # haftalık özet
```

Tüm komutlar `--dry-run` desteklemeli (yazmadan ne olacağını gösterir). `backtest` ve `promote-check` dışındaki komutlar `--use-holdout` bayrağını kabul etmez.

## 15. Fazlar ve kabul kriterleri

Her faz, kabul kriterleri sağlanmadan kapatılmaz.

**Faz 0: İskelet** — *TAMAMLANDI (2026-11-08)*
- Proje yapısı, pyproject, Dockerfile, config, `.env.example`
- Yerel test Postgres'i, şema, roller, tetikleyiciler
- Kabul: `bist init` hatasız çalışır; `pytest` yeşil (özellikle `test_append_only`)
- Doğrulama: 34 test yeşil (gömülü Postgres 16). Tetikleyiciler ve sütun yetkileri kasıtlı olarak bozulduğunda ilgili testler kırıldı (5 ve 7 başarısız), geri yüklenince yeşil. `bist init` CLI ile iki kez çalıştırıldı (ikincisi değişiklik yapmadı), `--dry-run` doğrulandı.
- **Doğrulanmadı:** Docker imajı derlenmedi ve gerçek `postgres:16` konteyneriyle test koşulmadı (yerel WSL'de Docker yok); Contabo sunucusunda henüz kurulum yapılmadı.

**Faz 1: Veri** — *TAMAMLANDI (yerelde ve Contabo'da, 2026-11-08)*
- borsapy ile BIST 30 indirme, kalite kontrolü, `prices`, `corporate_actions`, `universe_membership`, `trading_calendar`
- `adjust=False`'un ham fiyat verdiği doğrulanır
- Kabul: 30 hissenin tamamı için en az son 1 yıllık (tercihen tüm mevcut geçmiş) veri; kalite raporu; eksik hisseler loglanmış
- Doğrulama (gömülü Postgres + gerçek borsapy verisi, `bist universe sync` ve `bist fetch --since 2025-11-08`): 30 hisse × 250 işlem günü, kalite raporu çıktı (tek bayrak: BIMAS 2026-05-14 `jump_close`, ham kapanışın bedelsiz günü sıçraması, beklenen), ikinci çalıştırma 0 satır değiştirdi (idempotent), `open_adj` %100 dolu. 191 test yeşil, 3 ağ testi varsayılan olarak atlanır (`BIST_NETWORK_TESTS=1` ile çalışır). Alt ajan raporlarındaki iddialar (ham fiyat, evren, takvim) ana oturumda veriyle yeniden doğrulandı.
- **Contabo doğrulaması (2026-11-08):** `postgres-ortak` içinde `bist_tahmin` DB, `bist_owner`/`bist_app` rolleri ve 4 migration kuruldu (`bist init`, ikinci çalıştırma değişiklik yapmadı). Konteynerden (`/opt/bist-tahmin`, `docker compose run --rm bist ...`, `ortak-db` ağı) `bist_app` ile bağlanıldı: DELETE, TRUNCATE, tahmin alanı UPDATE'i ve DROP engelli. `universe sync` ve `fetch --since 2025-11-08` sunucuda ~2 dakikada çalıştı; sonuç yerelle aynı (30 hisse × 250 gün, 250 takvim günü, 729 kurumsal işlem, `open_adj` %100, tek bayrak BIMAS). Yedek kapsamı `pg_dumpall` çıktısıyla doğrulandı (bist_tahmin DB, roller, `COPY public.prices`); ilk dosya dökümü 03:50'de beklenir. Diğer 11 veritabanına dokunulmadı. Cron henüz **kurulmadı** (günlük iş Faz 2'de).
- Uygulama kararları:
  - Sıra: önce XU100 (takvim kaynağı), sonra hisseler; kalite kontrolü gerçek takvimi kullanır. Kapanış penceresinden (18:30 İstanbul) önceki bugünün yarım barı `prices`a da `trading_calendar`a da yazılmaz. Hata ("error") veren hisse yazılmaz ve `DISLANAN` olarak raporlanır; "flag"ler yazılır ve `BAYRAK` olarak raporlanır; hiçbir bar silinmez/düzeltilmez.
  - Evren üyeliği yarı açık aralıktır `[valid_from, valid_to)`; ilk senkronizasyonda 30 hissenin hepsi `valid_from = as_of` alır. **Geçmiş çeyreklik üyelikler ilk senkronizasyonda uydurulmadı.** 2026-11-08'de depoya eklendi (aşağıdaki "Geçmiş üyelik" maddesi); canlı sunucu DB'sine henüz YÜKLENMEDİ.
  - Takvim: XU100 bar günleri. `holidays` (TR) ile çapraz kontrol: 5 yılda veride olup tatil denen gün 0; barsız hafta içi tatil olmayan 5 gün (2023-02-08…14, deprem kapanışıyla uyumlu, resmi kaynaktan doğrulanmadı). Arife yarım günleri `holidays`'te yoktur; hacimden türetilen 17 aday (`half_day_candidates`) borsa takvimiyle **doğrulanmadı**, `is_half_day` doldurulmadı.
  - `bist fetch` tam 1 yıl için ~5 dakika sürer (istekler arası bekleme); günlük çalıştırmada yalnızca son birkaç hafta çekilir.
  - Bilinen küçük eksik: kalite kontrolü, bilinen bir bölünme gününe denk gelen ham `jump_close` bayrağını "beklenen" diye ayırmıyor (gürültü, engelleyici değil).

**Faz 2: Tahmin defteri ve çözücü (ML yok)** — *TAMAMLANDI (yerelde ve Contabo'da, 2026-11-08); ilk canlı çalışma 2026-11-09 19:00 İstanbul*
- Özellik üretimi, baseline'lar, `predict daily`, `resolve`
- Sızıntı, purge ve append-only testleri geçiyor
- Kabul: Günlük cron/manuel çalıştırmada tahminler yazılıyor, vadesi gelenler çözülüyor, tekrar çalıştırma çift kayıt üretmiyor (idempotent)
- Doğrulama: 424 test yeşil (gömülü Postgres). **Gerçek veriyle** (30 hisse, `fetch --since 2025-01-01`, 3 gün için geliştirme amaçlı geçmişe yazım): 450 tahmin yazıldı ve **450'si çözüldü, 0 atlandı**. BIMAS 2026-05-08 → 05-15 (14 Mayıs %100 bedelsiz dahil): ham kapanış 790→405,25, çözücü **+%2,59** yazdı (sahte −%49 değil); |getiri| > %35 olan satır yok.
- **Contabo doğrulaması (2026-11-08):** migration 005 canlı `bist_tahmin`'e uygulandı. Geçici `bist_deneme` veritabanında (sonra silindi) sunucudaki konteynerden tam `run-daily` gerçek ağla çalıştı: evren 30, indirme hatasız, 150 tahmin yazıldı (`made_at=2026-11-06`), çıkış 0, ikinci çalıştırma 0 yazdı (idempotent), `runs` iki satır `ok`, `recorded_at − created_at ≈ 0,1 sn`. Canlı defter bilerek **boş** bırakıldı: sunucudaki üyelik `valid_from = 2026-11-08` olduğundan 6 Kasım için evren boş (geçmiş üyelik uydurulmaz), `run-daily` bunu net bir mesajla reddediyor. **İlk canlı tahmin: Pazartesi 2026-11-09 kapanışı.**
- **Zamanlama (`/etc/cron.d/bist-tahmin`, depoda `deploy/`):** `CRON_TZ` bu sunucuda (Ubuntu 24.04, cron 3.0pl1) **çalışmıyor** (denendi, tetiklenmedi). Sunucu Europe/Berlin'de (yaz/kış saati değişir), İstanbul sabit UTC+3. Çözüm: satırlar Berlin saatiyle iki olası saate konur, `deploy/cron_run.sh` `ONLY_IST_HOUR` ile gerçek İstanbul saatini kontrol eder. Hafta içi 19:00 İstanbul `run-daily`, 19:45 yeniden deneme (`RETRY=1`, yalnız bugün başarısızsa), Cumartesi 10:00 haftalık rapor (`/opt/bist-tahmin/reports/`). `flock` + `timeout 45m`; log `/opt/bist-tahmin/logs/`. Root crontab'ına dokunulmadı. Sarmalayıcının saat koruması, `RETRY` atlaması ve konteyner çağrısı sunucuda sınandı.
- Modüller: `features/build.py` (34 özellik, `FEATURE_VERSION=f1`, kanonik JSON + sha256, `assert_no_lookahead` dedektörü), `baselines/rules.py` (5 baseline, `baseline_<ad>_v1`), `predict/ledger.py` (idempotent yazıcı), `predict/daily.py`, `resolve/resolver.py`, `jobs.py` (advisory lock + `runs`), `pipeline.py`.
- Bağımsız kod incelemesi (ayrı bir ajan, salt okunur) 9 bulgu getirdi; hepsi ele alındı veya kayda geçti:
  - **Kritik:** bedelsiz hem kaynakta hem `corporate_actions`'ta yoksa iki yol da aynı sahte düşüşü gösterip yanlış getiriyi **kalıcı** yazıyordu → çözücü artık made_at–vade arasında günlük `|close_adj getirisi| > 0,15` (`MAX_DAILY_MOVE`; BIST limiti ~%10) görürse `unexplained_jump` ile **yazmıyor** (şüphede yazma; veri düzelince aynı satır çözülür).
  - **Gecikmiş yazım:** yeni tahmin, `made_at`'i izleyen seansın 10:00'ından (İstanbul; hafta sonu/tatil atlanır) sonra yazılamaz (`LateWrite`). Zaten yazılmış satırların tekrarı bu kurala takılmaz. Pipeline bu durumda çıkış 3 verir.
  - **DB sertleştirme (migration 005):** `recorded_at` tetikleyiciyle DB saatine kilitli (uygulama sahteleyemez; geç yazım `recorded_at − made_at` ile denetlenebilir); ileri tarihli `made_at` reddedilir; çözümde 6 alanın hepsi zorunlu; vade gelmeden (`resolved_at < made_at + horizon_days` takvim günü) çözüm reddedilir.
  - **Sessiz kapsam kaybı:** kapsam < %80, atlanan baseline, `adjustment_mismatch`/`unexplained_jump`/`stale_ticker`, vadesi ≥3 işlem günü geçmiş ama çözülmemiş tahmin (`count_overdue`) ve XU100 takvim deliği şüphesi artık çıkış kodunu 1 yapar ve `runs.detail`'e yazılır. `stale_ticker`: hissenin barları durmuşsa sonsuza dek "not_due" denmez.
  - **İş takibi:** her adım `runs.detail`'e hemen yazılır (iş sonradan patlasa bile görünür); bayat veri `skipped`; kilit alınamazsa `skipped` satırı + çıkış 4; `connect_timeout=15`.
  - **Evren senkronu** günlük akışa girdi (kaynak <25 bileşen verirse uygulanmaz, uyarı).
  - Bilinen sınır: `XU100` takvim deliği (kaynak bir günü eksik verirse ufuk 1 gün kayar) yalnızca şüpheyle (≥3 hissede "XU100'de olmayan günde bar") işaretlenir, otomatik düzeltilmez. Çözücü eşiği 0,15 gerçek BIST verisinde taranmadı; işlem durdurma sonrası meşru büyük hareketler satırı `unexplained_jump` olarak bekletebilir (bilinçli).

**Faz 3: Değerlendirme ve baseline raporu** — *kod tamam, faz AÇIK: gerçek canlı veriyle ilk rapor bekliyor*
- Metrikler, IC, blok bootstrap, kohort simülasyonu, maliyet ızgarası, kalibrasyon, haftalık rapor
- Kabul: Baseline sonuçları rapor olarak üretiliyor; metrikler ve bootstrap test edilmiş
- Hazır (veriden bağımsız saf çekirdek, `evaluate/metrics.py` ve `evaluate/bootstrap.py`, testli): Brier, log loss (kırpılmış), kalibrasyon, günlük Spearman IC ve Newey-West t (varsayılan gecikme `horizon_days − 1`, Bartlett ağırlıkları), tarihe göre dairesel blok bootstrap, eşleştirilmiş fark CI, iid karşılaştırması, Bonferroni, etkin örneklem büyüklüğü. Sentetik günlük-korelasyonlu veride blok CI, iid CI'dan ~3,5 kat geniş çıktı.
- Eklendi (2026-11-08): `evaluate/portfolio.py` (kohort portföyü: günlük skor sırasına göre ilk %20, eşit skorlar dahil → "hep yükseliş" = benchmark; maliyet = 2×bps; benchmark maliyetsiz; yalnız uzun; `equity_curve`, `max_drawdown`, `relative_drawdown`), `evaluate/costs.py` (maliyet duyarlılığı), `evaluate/report.py` + `report_portfolio.py` (model başına Brier, baz-orana karşı eşleştirilmiş blok bootstrap farkı, IC, kalibrasyon, **örnek ve farklı gün sayısı**, etkin örneklem uyarısı, maliyet etiketi "DOĞRULANMADI: tahmini/maliyetsiz", yatırım tavsiyesi değildir notu), CLI `bist baseline report` ve `bist report weekly [--out]`.
- Sınırlar: `equity_curve` günlük mark-to-market içermez (kohort getirisi made_at adımına yazılır, gerçekte vadede gerçekleşir); drawdown **alt sınır** sayılmalıdır. Kabul için henüz gerçek canlı defter yok (birikim ~3 ay). Newey-West t, harici bir kütüphaneyle (statsmodels) çapraz doğrulanmadı.

**Faz 3.5: Backtest laboratuvarı** — *laboratuvar kodu TAMAMLANDI ve kabul kriterleri karşılandı (2026-11-08, yerelde); canlı sunucuya üyelik/fiyat yükleme bekliyor*
- **Geçmiş üyelik (2026-11-08):** `reference/bist30/` altında 3 dönem dosyası (kaynak URL'leri ve notlarla) ve üretilen `membership_history.csv` (58 aralık, yarı açık `[valid_from, valid_to)`, 2019-07-01 → bugün). Kaynak: Borsa İstanbul resmi "BIST Pay Endeksleri Dönemsel Değişiklikleri" duyuruları (yalnızca eklenen/çıkarılan/yedek verir). **Tam 30'luk listeler türetilmiştir**: 22 Ocak 2021 tarihli arşiv listesi çapa alınıp resmi değişiklikler ileri/geri uygulanmıştır. Doğrulama (programatik): 31 dönem × tam 30 hisse, boşluk/çakışma yok, ardışık fark = beyan edilen eklenen/çıkarılan (0 tutarsızlık), zincir bugünkü borsapy XU030 listesine **birebir** ulaşıyor; ayrıca Mayıs 2024 haber listesi 2024-Q2 ile uyuşuyor. Güven: 24 aralık high, 34 aralık medium (2019-Q3–2020-Q4 resmi değişikliklerden geriye zincirleme; doğrudan tam liste yok). Kod eşlemesi: KOZAL→TRALT, KOZAA→TRMET (borsapy geçmişi yeni kodla verir).
- **Bilinen sınırlar:** (1) Çeyrek ortası istisnai değişiklikler aranıp bulunamadı ("yok" denmiyor). (2) Fiyat geçmişi: bugün üye olmayan 19 hissenin 17'si tam geçmişle indirilebiliyor (2019-06 → 2026-11); `SODA` ve `TRKCM` (Ekim 2020'de Şişecam'a devrolup borsadan çıktı) TradingView'de geçersiz sembol, bu yüzden ilgili dönemlerde (SODA 2019-07→2020-10, TRKCM 2020-04→2020-10) değerlendirme evreni 28–29 hisse olur (hayatta kalma yanlılığının küçük, belgelenmiş kalıntısı). `DSTKF` 2025-02, `KONTR` 2020-10'dan itibaren bar verir.
- **Araçlar:** `tools/build_membership_history.py` (JSON→CSV), `bist universe load-history [--dry-run]` (mevcut açık satırların `valid_from`'unu yalnızca daha erkene çeker, çelişkide durur, idempotent). Testler: her gün tam 30 üye, çakışma yok, bilinen resmi değişiklikler.
- **Laboratuvar (kod):** `backtest/splits.py` (genişleyen pencere + purge), `guard.py` (sınama kümesi kilidi), `registry.py` (`experiments`; deney çalıştırılmadan ÖNCE kaydedilir = deneme sayılır), `dataset.py` (tarihli üyelikle özellik+etiket, preflight sızıntı kontrolü), `lab.py` (lojistik regresyon, scaler yalnız eğitime fit, baseline'ların veri setinden yeniden üretimi `rules.py` ile birebir testli, eşleştirilmiş blok bootstrap + Bonferroni), `runner.py`, CLI `bist backtest build-dataset|run`. `weekday` (0–4 sayısal) doğrusal modelde yanlış belirtilmiş olduğundan varsayılan gruplardan çıkarıldı.
- **Kabul kriterleri:** (1) baseline'lar ve lojistik regresyon tarihsel olarak çalışıyor ✓; (2) kasıtlı sızıntılı özellik fonksiyonu preflight dedektörü tarafından yakalanıyor ✓ (`tests/test_dataset.py`), eğitimde etiket-türevi sütun reddi ✓, purge ihlali `AssertionError` ✓; (3) sınama kümesi varsayılan kilitli ✓ (gerçek veride 4020 satır düşürüldü; `--use-holdout` yalnız terfi için, loglanır). 538 test yeşil.
- **Gerçek veri seti (yerel):** 53.546 satır, 1.798 gün (2019-09 → 2026-11), 47 hisse, günlük evren 28–30; etiket kapsamı %99,7; günlük |getiri| > %15 olan yalnızca 1 satır (TTKOM 2022-04-28, −%17,3, incelenmedi). `DSTKF`'nin 2026-07/09'da tekrarlayan −%40,9'luk 5 günlük getirisi veri hatası DEĞİL: art arda günlük −%10 limit-aşağı (0,9⁵), doğrulandı.
- **İlk üç deney (önceden belirlendi, aile `faz35-ilk-v1`, holdout KİLİTLİ, 1.164 farklı gün, 2021-09 → 2026-04, 59 fold):**

  | Deney | Etiket / C | Brier farkı (model − geçmiş-oran) | Aralık (düzeltilmiş) | Sıfırı dışlıyor | IC ort. (NW t) | Net fazla getiri (kohort başına, 10 bps tek yön) |
  |---|---|---|---|---|---|---|
  | E1 | mutlak, C=1 | +0,00006 | [−0,0030; +0,0030] (%95) | hayır | 0,037 (3,19) | −0,00146 |
  | E2 | XU100'e göre, C=1 | +0,00013 | [−0,0006; +0,0008] (%97,5) | hayır | 0,033 (2,79) | −0,00181 |
  | E3 | mutlak, C=0,05 | +0,00002 | [−0,0037; +0,0037] (%98,3) | hayır | 0,037 (3,19) | −0,00153 |

  **Yorum:** (a) Hiçbir model ana ölçütte (Brier) geçmiş-oran baseline'ını geçemedi (0,2484 vs 0,2483); terfi kuralı hiçbirini kabul etmez. (b) Hisseler arası sıralamada küçük ama istatistiksel olarak anlamlı bir sinyal görünüyor (IC ≈ 0,03–0,04, Newey-West t ≈ 3; baseline'larda ≈ 0). (c) Bu sinyal ekonomik olarak kullanılamaz: maliyetsiz brüt fazla getiri kohort başına +%0,05; tek yön 5 bps maliyet (gidiş-dönüş %0,10) bunu siler. (d) Kalibrasyon: model üst kovalarda aşırı iddialı (0,63–0,73 tahmin, 0,58–0,59 gerçekleşen) ve alt kovalarda aşırı çekingen; olası neden piyasa-düzeyi özelliklerin (mkt_*, breadth) piyasa yönünü tahmin etmeye çalışması. Bu bir HİPOTEZDİR, henüz test edilmedi.
  - **Uyarılar:** IC t-istatistiği deneme sayısına göre düzeltilmiş DEĞİL (yalnızca Brier aralığı Bonferroni'lidir). Bu üç deney yerel geliştirme DB'sinin `experiments` tablosundadır, canlı sunucu kaydı değildir; resmi terfi denemeleri kanonik kayıtta tutulacaktır. Coğrafi/rejim kararlılığı (yıllara göre IC) henüz bakılmadı.
  - **Sonraki aile için ön kayıt önerisi (`faz35-v2`):** (1) yalnız `stock_z` + göreli etiket; (2) E1 + olasılık kalibrasyonu (izotonik/Platt); (3) 750 günlük yuvarlanan pencere. Üç deneyden sonra durulur; sonuçlar yorumlanmadan yeni deney eklenmez.
- Purge'lu tarihsel walk-forward, sınama kümesi kilidi, `experiments` kaydı ve çoklu deneme düzeltmesi
- Deneyler: etiket (mutlak/göreli), özellik grupları, düzenlileştirme, eğitim penceresi
- Kabul: Baseline'lar ve lojistik regresyon tarihsel olarak çalışıyor; kasıtlı sızıntılı özellik dedektör tarafından yakalanıyor; sınama kümesi varsayılan olarak kilitli

**Faz 4: ML modeli ve öğrenme döngüsü**
- Challenger'ın canlı gölge modda yazması, champion/challenger, terfi kuralı, drift izleme, devre kesici
- Kabul: Challenger eğitimi ve terfi kontrolü uçtan uca çalışıyor; terfi kuralı bilinçli olarak kötü bir challenger ile test edilmiş ve reddetmiş

**Faz 5 (opsiyonel): Genişleme**
- BIST 100 evreni, USD/TRY ve faiz özellikleri, bilanço/KAP (İş Yatırım erişimi çözülürse), yerel LLM ile haftalık yorum
- Bu faz, Faz 4'ün en az 60 farklı günlük çözülmüş tahminle sonuç vermesinden sonra değerlendirilir

## 16. Çalışma kuralları (her oturum için)

- Önce bu dosyayı oku. Sonra `git status` ile mevcut durumu kontrol et.
- Her değişiklik bir faza ve bir kabul kriterine bağlanır.
- Sızıntı kuralları (Bölüm 5) ihlal edilirse değişiklik kabul edilmez, test yazılır.
- Yeni bir deneme (parametre, özellik, model) yapıldığında `experiments` tablosuna kayıt eklenir. Sınama kümesi yalnızca terfi aşamasında kullanılır.
- Gerçek anahtar, şifre veya kişisel veri commit edilmez.
- `predictions` tablosunda geçmiş kayıtları değiştiren hiçbir kod yazılmaz. Hata düzeltmesi gerekiyorsa yeni bir kayıt ve açıklama eklenir.
- Komisyon ve spread oranları doğrulanmadan `costs` değerleri varsayılan dışında kullanılmaz. Doğrulanmamış oranlar raporda "tahmini" olarak işaretlenir.
- Sunucuda (Contabo) değişiklik yapmadan önce `/opt/postgres-ortak/session.md` gibi ilgili hizmetin notlarına kayıt düşülür; paylaşımlı hizmetlere (`postgres-ortak`, `mysql-ortak`, Ollama, nginx) yıkıcı işlem yapılmaz.
- Tahmin çıktıları kullanıcıya yatırım tavsiyesi olarak sunulmaz. Her rapora şu not eklenir: "Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır."

## 17. Açık sorular ve onay bekleyen varsayılanlar

| # | Konu | Durum |
|---|---|---|
| 1 | Komisyon ve spread oranları (aracı kuruma göre) | **Açık.** Doğrulanana kadar `null`, raporda "tahmini" ve duyarlılık ızgarası. |
| 2 | Günlük çalışma saati | **Karar:** 19:00 İstanbul (kapanış seansı + 15 dk gecikme + güvenlik payı). |
| 3 | BIST 30 bileşen doğrulaması | **Karar:** `bp.Index("XU030").component_symbols`, her çeyrek KAP duyurusuyla çapraz kontrol. |
| 4 | Ticari kullanım | **Varsayılan: hayır.** Kullanıcı teyidi bekliyor. Evet ise borsapy/veri lisansı ayrıca değerlendirilir. |
| 5 | Rapor yeri | **Varsayılan: yalnızca yerel/özel** (ham fiyat serisi yayımlanmaz). Power BI veya şifreli HTML sonradan seçilebilir. Kullanıcı teyidi bekliyor. |
| 6 | Depolama | **Varsayılan: `postgres-ortak` + ayrı `bist_owner`/`bist_app` rolleri.** Alternatifler: SQLite, projeye özel Postgres konteyneri. Kullanıcı teyidi bekliyor. |
| 7 | İş Yatırım erişimi (bilanço) | **Açık.** Sunucudan bağlanılamıyor; Faz 5 önkoşulu. |

Bu sorular cevaplanana kadar ilgili değerler varsayılan olarak bırakılır ve raporlarda belirtilir.

## 18. Bilinen riskler

- **Küçük etkin örneklem:** Hisseler arası korelasyon 0,42 ve çakışan pencereler nedeniyle ilk anlamlı sonuç için en az ~60 farklı gün (yaklaşık 3 ay) gerekir. Erken sonuçlar gürültüdür.
- **Baz oran kayması:** Yükseliş oranı yıldan yıla %49–62 arasında oynadı; "geçmiş-oran" baseline'ı ve kalibrasyon izlemesi bunun için vardır.
- **Piyasa rejimi değişimi:** Faiz kararları, kur şokları ve seçim dönemleri modeli hızla bozabilir. Drift izleme yakalamaya çalışır ama garanti vermez.
- **Veri sağlayıcı değişikliği:** borsapy veya TradingView erişimi değişebilir. Veri kaynağı soyutlama katmanı arkasında tutulur. TradingView WebSocket erişimi resmi bir ürün değildir.
- **Deneme şişmesi:** Backtest laboratuvarı çok deneme üretir. Sınama kümesi, çoklu deneme düzeltmesi ve canlı gölge çalıştırma bunun içindir.
- **Paylaşımlı altyapı:** `postgres-ortak` 12 veritabanına hizmet eder. Bu projenin yükü çok küçük olsa da `statement_timeout` ve bağlantı sınırı konur.
- **Disk:** Sunucu diskinin %88'i dolu (2026-11-08). Proje veri hacmi küçüktür; yine de disk durumu izlenir.
- **Lisans:** borsapy kullanım koşulları veriyi ticari amaçla kullanmayı yasaklar; Borsa İstanbul veri dağıtımını lisansa bağlar. Bu proje kişisel ve eğitim amaçlıdır.
- **Kullanıcı yanılgısı:** Sistemin "tahmin ettiği" izlenimi yaratılabilir. Raporlar her zaman belirsizliği, örneklem sayısını ve farklı gün sayısını gösterir.

## 19. Dağıtım (Contabo sunucusu)

Durum tespiti 2026-11-08'de SSH ile yapıldı (yalnızca okuma). Dağıtımdan önce yeniden doğrulanır.

- **Sunucu:** Ubuntu 24.04, 6 çekirdek, 11 GB RAM (~5,4 GB boş), Python 3.12, Docker 29, Compose 2.40, saat dilimi Europe/Berlin. Ana makinede Ollama (`127.0.0.1:11434`, `qwen3:4b`, `bge-m3`); konteynerden erişim için ayrıca ağ ayarı gerekir.
- **Veritabanı:** `postgres-ortak` (PG 16.14, `mem_limit` 800 MB, 100 bağlantı), `ortak-db` Docker ağı, konteyner içi adres `postgres-ortak:5432`. Yeni `bist_tahmin` veritabanı ve iki rol açılır; rol parolaları sunucuda `.env` (600) içinde tutulur, repoya girmez.
- **Yedek:** `postgres-ortak` için gece `pg_dumpall` (14 gün) ve 04:30 restic mevcut; yeni veritabanı otomatik kapsanır. Dağıtımda ilk yedeğin yeni veritabanını içerdiği doğrulanır.
- **Konum:** `/opt/bist-tahmin/` (Compose, `.env` 600, `deploy/` cron betikleri, `logs/`, `reports/`; kod `git archive` ile aktarıldı, sunucuda git deposu yok). Sürekli çalışan servis yok; cron `docker compose run --rm` ile tetikler. İmaj `bist-tahmin:latest` (1,13 GB, sunucuda `docker build`). **Güncelleme:** `git archive HEAD | ssh contabo 'tar -x -C /opt/bist-tahmin'` sonra `docker build -t bist-tahmin:latest .`; `.env` ezilmez.
- **Kurulum durumu (2026-11-08):** `bist_tahmin` DB ve roller kuruldu, ilk veri yüklendi (Bölüm 15, Faz 1). Yönetici (`postgres`) parolası yalnızca `bist init` sırasında, geçici SSH tüneliyle (`127.0.0.1:5434`) kullanıldı; konteynerin `.env`'inde yoktur.
- **Cron:** `CRON_TZ=Europe/Istanbul` ile günlük 19:00 (Ubuntu 24.04 cron'unda `CRON_TZ` desteği kurulumda doğrulanır; yoksa betik içinde İstanbul saati kontrol edilir) ve haftalık rapor. Çıktılar `logs/` ve `runs` tablosuna.
- **Kaynak sınırları:** Konteynere CPU/RAM sınırı konur (`--cpus`, `mem_limit`), `nice` ile çalışır. Sunucu yükü ~3,5 / 6 çekirdek.
- **Dikkat:** Sunucudaki diğer hizmetlere (nginx, diğer veritabanları, diğer konteynerler) dokunulmaz. Erişim sorunları (SSH zaman aşımı 2026-11-08) dağıtım öncesi kontrol edilir; art arda bağlantı denenmez.
- **İş Yatırım** bu sunucudan açılmaz (Bölüm 4).

## 20. Başlangıç komutları (Faz 0, yerel geliştirme)

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest                                     # Docker yoksa gömülü Postgres (pgserver)

# Docker'lı makinede gerçek Postgres 16 ile:
docker compose -f docker-compose.test.yml up -d
BIST_TEST_ADMIN_URL=postgresql://postgres:test@127.0.0.1:54329/postgres pytest

# Kurulum (hedef sunucuda): .env'i doldur, önce --dry-run
cp .env.example .env
set -a; source .env; set +a
bist init --dry-run && bist init
```

(Bağımlılıklar `pyproject.toml` içinde tanımlanır. SQLAlchemy kullanılmıyor; doğrudan `psycopg` 3.)

---

## Değişiklik günlüğü

- **v2.9 (2026-11-10):** Faz 4 yerelde BİRLEŞMEYE HAZIR DEĞİL, ama düzeltmeler tamam (commit'ler `e99ae35`, `e91b9cb`, `6d35357`, test düzeltmesi bu sürümle). Bağımsız incelemeler ve düzeltmeler: (1) holdout dışarıda (train), holdout kanıtı spec ile eşleşmeli; (2) terfi kosul 2 ve 5 champion'a karşı (varsa) VE baz orana karşı (her zaman); champion yoksa yalnız baz oran; (3) champion yazma yalnız promote ile (SET LOCAL kapısı, sınırı belgeli); (4) migration 007: tek champion indeksi, rol trigger'ı, system_state korumaları; (5) drift alarmı model üretimini durdurur (baseline sürer), bayat veride izleme atlanır (kayıtlı); (6) `bist models list|train|promote-check|promote|retire`; promote `--confirm` ister. Kalan (kararsız): haftalık yeniden eğitim otomatik DEĞİL (manuel CLI); promote_check ile promote aynı kilit altında; SET LOCAL kapısı kasıtlı SQL bypass'ını durdurmaz (güçlü garanti SECURITY DEFINER gerektirir: spec dışı). Sunucuya Faz 4 henüz uygulanmadı.
  **Test altyapısı notu:** Tam takım, `/tmp` küçük bir tmpfs iken (kalan ~2 GB) gömülü Postgres'in disk dolmasından "recovery mode" hatası verdi (14 hata; kod değil). Geçici dizin büyük diske alınınca 718 geçti, 0 hata. README'ye TMPDIR notu eklendi.
- **v2.8 (2026-11-10):** (1) İlk canlı çalıştırma (2026-11-09) başarılı: 150 tahmin, çıkış 0; süre 24 dk. Neden: hisse başına Is Yatırım `splits` çağrısı 3 kez yapılıyordu; önbellekle THYAO için 3 → 1 (gerçek ağ), `deploy-splits-fix` dalında, sunucuya henüz dağıtılmadı. (2) Faz 4 yarım işi: model eğitimi (salt-okunur dizi hatası), eşzamanlılık testi (bağlantı sınırı), terfi beklentisi (`assert or True` kaldırıldı) ve sağlık kontrolü (bayat/atlanan akış UYARI) düzeltildi; 672 test yeşil. Yine de Faz 4 `main`'e birleştirilmedi: terfi kararının canlı kanıtı ve sunucu entegrasyonu yok. (3) Önceden kayıtlı `faz35-v2` (E4–E6): hiçbiri Brier'da baz oranı geçmedi, aralıklar sıfırı dışlamadı. Piyasa-düzeyi özellik hipotezi desteklenmedi (E2 ↔ E4 karşılaştırması). Kalibrasyon (izotonik) Brier'ı kötüleştirdi (0.2484 → 0.2503).
- **v2.7 (2026-11-08):** Faz 3.5 laboratuvarı tamamlandı (üç paralel ajan + ana oturumda birleştirme); gerçek veri setiyle ilk üç deney koşuldu: model Brier'da baz oranı geçemedi, küçük ama anlamlı bir IC var, maliyet sonrası kullanılamaz. Sınama kümesi dokunulmadı.
- **v2.6 (2026-11-08):** Geçmiş BIST 30 üyeliği (2019-07 → 2026-11) üç ajanla araştırıldı, programatik olarak doğrulandı (çapadan çapaya zincir) ve depoya eklendi; yükleyici + testler. Eski üyelerin fiyat erişimi ölçüldü (17/19). Sunucu DB'sine henüz yüklenmedi.
- **v2.5 (2026-11-08):** Faz 2 Contabo'da kuruldu: migration 005, imaj, tam akış denemesi (geçici DB, silindi), cron (`CRON_TZ` desteklenmediği için Berlin saati + İstanbul saat koruması). Canlı defter boş; ilk canlı tahmin 2026-11-09. Üyelik başlangıcı net hata mesajı verir.
- **v2.4 (2026-11-08):** Faz 2 yerelde tamamlandı (özellikler, baseline'lar, defter yazıcısı, çözücü, pipeline), Faz 3 kodu eklendi (portföy, maliyet, rapor). Bağımsız kod incelemesi bulgularıyla migration 005, çözücüde `unexplained_jump`/`stale_ticker`, yazım penceresi (`LateWrite`), çıkış kodları ve `runs` kaydı sertleştirildi. Çalışma yöntemi: işler dosya sahipliği ayrık alt ajanlara bölündü; sonuçlar ana oturumda yeniden doğrulandı.
- **v2.3 (2026-11-08):** Contabo kurulumu tamamlandı (DB, roller, imaj, ilk veri, yedek kapsamı). Faz 1 sunucuda da kapandı. Çıkarım: borsapy sunucudan ham/düzeltmeli fiyat ve `splits` ile çalışıyor.
- **v2.2 (2026-11-08):** Faz 1 yerelde tamamlandı (üç paralel alt ajanla; sonuçlar ana oturumda doğrulandı). Ham fiyat doğrulandı, `open_adj` (migration 004) eklendi, kalite kapısı ve yarım bar elemesi `fetch`e bağlandı. Düzeltme: İş Yatırım "sunucudan açılmıyor" ifadesi daraltıldı (yalnızca ana sayfa; `splits` çalışıyor, bilanço uç noktaları denenmedi). Faz 3 saf istatistik çekirdeği ön çalışma olarak eklendi (faz açık).
- **v2.1 (2026-11-08):** Faz 0 tamamlandı. Migration'lar `src/bist_tahmin/sql/` altına (pakete dahil), `bist_owner` NOLOGIN olarak tasarlandı (yalnızca `init` sırasında `SET ROLE` ile kullanılır, ayrı parola yok). Yerel test gömülü `pgserver` ile (Docker gerekmez). `db_name` doğrulaması ve `CONNECT` yetkisi yalnızca `bist_app`e verildi (paylaşımlı sunucudaki diğer kullanıcılar bağlanamaz).
- **v2 (2026-11-08):**
  - Ana hedef ve başarı tanımı eklendi (Bölüm 0).
  - Depolama SQLite'tan PostgreSQL'e (`postgres-ortak`, ayrı roller, tetikleyiciler) önerildi, onay bekliyor.
  - Eğitime purge/embargo, fold içi özellik seçimi, işlem zamanlaması, sınama kümesi kilidi eklendi.
  - Etkin örneklem düzeltmesi: tarihe göre blok bootstrap, farklı gün sayısı eşiği, eşleştirilmiş karşılaştırma, çoklu deneme düzeltmesi.
  - Mutlak yön yanında göreli etiket ve sıralama skoru; IC ölçütü.
  - `prob_up > 0.5` simülasyon kuralı yerine sıralama tabanlı kohort portföyü, benchmark ve maliyet ızgarası.
  - Devre kesici göreli düşüşe bağlandı; drift eşiği geçmiş-oran baseline'ına göreli oldu.
  - Geçmiş-oran baseline'ı eklendi; `prob_up` kırpıldı.
  - `universe_membership`, `corporate_actions`, `trading_calendar`, `runs` tabloları ve `features_json` eklendi.
  - Faz 3.5 (backtest laboratuvarı) eklendi; saat Europe/Istanbul 19:00 oldu; dağıtım bölümü eklendi.
- **v1:** İlk sürüm.
