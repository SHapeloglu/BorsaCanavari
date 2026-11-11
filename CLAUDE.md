# CLAUDE.md — BIST Tahmin Laboratuvarı: çalışma kuralları

Bu dosya bu depoda çalışan her Claude oturumu için kurallardır. Tasarımın tek kaynağı `PROJE.md`'dir; çelişki olursa `PROJE.md` kazanır ve önce o güncellenir.

## Oturum başı
1. `PROJE.md`'yi oku (özellikle Bölüm 0, 3, 5, 11, 12, 15 ve son değişiklik günlüğü).
2. `task.md` (sıradaki iş) ve `session.md` (son kayıtlar) oku.
3. `git status` ve `git log --oneline -5` ile durumu doğrula. Bu depoda `main` dalı yok; çalışma dalı `faz-4-yasam-dongusu`.

## Sunucu (Contabo) kuralları
- Sunucuya yazan her adım (imaj, migration, veri yükleme, cron) **önce kuru çalıştırma veya salt okunur kontrol** ile yapılır, sonra gerçek çalıştırma.
- Üretim defterine (`predictions`) doğrudan yazan hiçbir komut elle çalıştırılmaz; canlı akış yalnızca cron'dan çalışır.
- Cron saatleri (19:00, 19:45, cumartesi 10:00 İstanbul) ile çakışan sunucu değişikliği yapılmaz. Günlük çalıştırma sırasında derleme yapılmaz.
- Sunucudaki `/opt/bist-tahmin/.env` (600) asla okunup ekrana ya da commit'e yazılmaz. Yönetici parolası (`postgres-ortak/.env`) yalnızca geçici tünel üzerinden `bist init` için kullanılır, kaydedilmez.
- Ortak servis klasörü `/opt/postgres-ortak` (CLAUDE.md orada geçerlidir): oraya dokunmadan önce `session.md`'ye not düşülür.
- Sunucudaki her değişiklik `session.md` ve `task.md` içine kaydedilir.

## Veri ve yasal sınırlar
- Ham fiyat serileri herkese açık yayımlanmaz; yalnızca türetilmiş metrikler raporlanır (Borsa İstanbul lisans koşulu, PROJE Bölüm 4).
- Her rapora "Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır." notu eklenir.
- Maliyet oranları doğrulanmadan varsayılan dışında kullanılmaz; raporda "DOĞRULANMADI" etiketi taşır.

## Sınama kümesi (holdout)
- `holdout_start` (2026-05-01) sonrası veri **terfi aşaması dışında hiçbir deneyde kullanılmaz**. Geliştirme ve mekanik testler, holdout'u geleceğe kaydırılmış kopya yapılandırmayla ve holdout satırları dışarıda bırakılarak yapılır.
- `--use-holdout` yalnızca gerçek bir terfi kararı için, kayda geçerek kullanılır. Mekanik test için kullanılmaz.

## Test ve geliştirme
- Tam takım için geçici dizin büyük bir diske yönlendirilir (`/tmp` küçük tmpfs olabilir): `TMPDIR=$HOME/.cache/bist-pytest-tmp .venv/bin/python -m pytest -p no:cacheprovider -o addopts=""`.
- Bir commit atmadan önce tam takımın **çıkış kodu 0** olduğu doğrulanır (`echo $?` ile). Hatalı testle commit yapılmaz.
- Paralel ajanlarla çalışılıyorsa her ajanın dosya sahipliği önceden ayrılır; ortak dosyalar (`cli.py`, `PROJE.md`) tek ajanda olur.
- Ağ testleri `BIST_NETWORK_TESTS=1` olmadan çalışmaz.

## Commit ve dal
- Mesajlar Türkçe ya da İngilizce, kısa başlık + gerekçe. Mesaj sonuna Claude katkı satırı eklenir (`Co-Authored-By: Claude ... <noreply@anthropic.com>`); model adı o anda kullanılan modeli gösterir.
- Her faz kendi dalında; birleştirme yalnızca kabul kriterleri ve sunucu doğrulaması tamamlandıktan sonra yapılır.
- Gizli değer (parola, token, anahtar) asla commit edilmez; `.env` `.gitignore`'dadır.

## Yazım
- Kod ve tanımlayıcılar İngilizce; yorum ve belgeler diakritiksiz Türkçe (mevcut stil). Kullanıcıya dönük rapor metinleri normal Türkçe.
