# backlog.md — BIST Tahmin Laboratuvarı: fikir havuzu ve açık işler

Bu liste karar değildir. Bir madde işe dönüşürse `task.md`'ye taşınır; bir karar gerekiyorsa `PROJE.md` Bölüm 17'ye eklenir.

## Güvenlik ve dayanıklılık (öncelikli)
- **Terfi kapısını güçlendir:** SET LOCAL kapısı SQL bypass'ını durdurmaz. Çözüm `SECURITY DEFINER` fonksiyonu (bist_owner sahipliğinde, `current_user` kontrolü). Spec dışı; karar gerekir.
- **Yedek geri yükleme testi:** `postgres-ortak` son dökümü geçici konteynerde açılıp doğrulanmalı (ortak `task.md`'de de açık).
- **Canlı gölge kriteri:** şimdi challenger ile champion/baz eşleşmesi aynı satırlar üzerinden; bağımsız pencere (son N canlı gün) eklenmeli.
- **Promote ile kontrol arasındaki kilit:** kontrol ve terfi tek transaction içinde (şu an aynı advisory kilit altında, yeterli); otomatik terfi eklenirse yeniden değerlendir.

## Model yaşam döngüsü
- **Haftalık yeniden eğitim:** `due_for_retrain` var ama cron'a bağlı değil. Otomatik **challenger oluşturma** güvenli (terfi yok); terfi manuel kalmalı.
- **Kohort portföyü mark-to-market:** şu an `equity_curve` günlük MTM içermiyor; drawdown alt sınır.
- **Breaker maliyet varsayımı:** null maliyette 10 bps; gerçek oran doğrulanınca güncelle.

## Veri ve doğrulama
- **Maliyet oranları:** aracı kurum komisyonu ve spread (PROJE açık soru 1). Doğrulanana kadar 10 bps duyarlılık.
- **KAP çapraz kontrolü:** BIST 30 bileşenleri çeyreklik KAP duyurusuyla karşılaştırılmalı (açık soru 3). Şu an borsapy XU030 listesi kaynak.
- **Arife yarım günleri:** borsa takvimiyle doğrulanmalı (hacimden türetilen 17 aday doğrulanmadı).
- **Şubat 2023 deprem kapanış boşlukları:** resmi kaynakla teyit (5 barsız hafta içi gün).
- **Çözücü %15 eşiği:** gerçek BIST verisinde tarama; işlem durdurma sonrası meşru büyük hareketler.
- **DSTKF ve benzeri ardışık limit düşüşleri:** veri dışlama kuralı değil, bilgi notu olarak raporda.
- **Dağıtım tutarlılığı:** `open_adj` ve bölünme sonrası yeniden hesaplama senaryoları için ek test.

## Analitik
- **Newey-West t'nin harici doğrulaması** (statsmodels ile çapraz kontrol).
- **Yıllara göre kararlılık:** faz35 deneylerinde IC ve Brier yıllık alt dönemlerde.
- **Yeni önceden kayıtlı aile (`faz35-v3`):** `faz35-v2` sonucu (hiçbiri geçmedi, piyasa-özellik hipotezi desteklenmedi) sonrası; yalnızca önceden yazılmış bir gerekçeyle.
- **Coğrafi/rejim ayrımı:** yüksek ve düşük volatilite dönemlerinde performans.

## Kapsam genişlemesi (Faz 5, opsiyonel)
- **Bilanço ve KAP özellikleri:** İş Yatırım uç noktaları sunucudan denenmedi (ana sayfa zaman aşımı; `splits` çalışıyor). Önce erişim testi.
- **USD/TRY ve faiz (TCMB EVDS):** `EVDS_KEY` ortam değişkeni olarak; `.env` şablonunda yer alıyor.
- **BIST 100 evreni:** şu an yalnızca BIST 30.
- **Yerel LLM özeti (Ollama):** sunucuda `qwen3:4b`, `bge-m3` var; konteynerden erişim ağ ayarı gerektirir. Yalnızca haftalık yorum için.

## Operasyon ve araçlar
- **Sağlık cron'u:** `deploy/` hazır, sunucuya eklenmedi (task.md'de).
- **Uyarı kanalı:** `ALERT.txt` ve syslog dışında bildirim yok (e-posta/Slack yok). Karar: rapor yeri (PROJE açık soru 5).
- **CI:** yok. Tam takım yerelde; birleşik iş akışı için bir CI değerlendirilebilir.
- **Test süresi:** tam takım 2–10 dk; hızlı alt küme tanımı (`pytest -m fast`).
- **Dal yönetimi:** `main` yok; çalışma dalları `faz-*`. Birleştirme stratejisi ve varsayılan dal kararı gerekir.
- **Splits önbelleğinin iki dalda kopyası:** `cab326c` (faz-4) ve `ff60a45` (deploy-splits-fix) aynı değişiklik; ana dala tek kopya alınmalı.
- **ISG klasörü:** depo içinde, `.gitignore`'da, ayrı bir proje. Taşıma ya da ayırma kararı.

## Lisans ve kullanım (açık kararlar)
- **Ticari kullanım:** varsayılan hayır (PROJE açık soru 4). Evet ise borsapy ve Borsa İstanbul lisansı ayrıca değerlendirilir.
- **Rapor yeri:** yalnızca yerel/özel (açık soru 5); Power BI ya da şifreli HTML sonradan.
