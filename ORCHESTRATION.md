# ORCHESTRATION.md — çoklu ajan çalışma kuralları

Bu dosya, bu depoda birden fazla ajanın birlikte çalıştığı işlerin kurallarıdır. `CLAUDE.md` genel kuralları, `PROJE.md` tasarımı tanımlar; bu dosya **ajanların nasıl koordine olduğunu** tanımlar. Çelişkide `PROJE.md` ve `CLAUDE.md` kazanır.

Sürüm: 4 (2026-11-24). Sürüm 4: zorlama katmanı sayısı üçten dörde çıktı (kapsam kancası eklendi, §3). Sürüm 2'de "izinler araç düzeyinde zorlanmıyor" notu kaldırılmıştı; zorlama alt ajan tanımları, proje izin ayarları ve mekanik kapılarla yapılır (bkz. §3). Sürüm 3 ile: kapsam kapısı betiğe bağlandı (§3), birleştirme yalnızca insan betiğiyle yapılır (§4), kapsam dışı talep akışı ve izin dosyası şablonu eklendi (§4, §8).

## 1. Roller

| Rol | Nerede | Ne yapar | Ne yapmaz |
|---|---|---|---|
| **Lider** | Ana oturum | Görevi parçalar, alt ajanlara görev şablonuyla verir, sonuçları ve kanıtları toplar, birleştirme önerir. | Kod yazmaz: ana oturumda Edit/Write kullanılmaz. Kendi çıktısını doğrulamaz. |
| **Uygulayıcı** | `.claude/agents/uygulayici.md` | Tek görevi kendi worktree'sinde ve izin listesi içinde yapar. | Kapsam dışına çıkmaz; birleştirmez; sunucu, veritabanı, ağa dokunmaz. |
| **Doğrulayıcı** | `.claude/agents/dogrulayici.md` | Uygulayıcının iddialarını bağımsız olarak yeniden üretir. Salt okunur. | Dosya değiştirmez, düzeltmez, commit etmez. |
| **İnsan** | — | Tek onay kapısı K1'i (birleştirme) karar verir; sunucu ve dış veri işlerini kendisi çalıştırır. | — |

Lider ile uygulayıcı aynı ajan olamaz. Doğrulayıcı, doğrulanan görevi yazan ajan olamaz.

## 2. Çalışma ortamı

- Her uygulayıcı kendi **git worktree**'sinde ve kendi dalında çalışır:
  `git worktree add ../wt-<gorev> -b agent/<gorev> <taban-SHA>`
  Taban: görev belirtmediyse `faz-4-yasam-dongusu`'nun o anki SHA'sı. Taban SHA görev metninde açıkça yazılır; herkes aynı SHA'dan başlar.
- Ana çalışma ağacı (`/home/yeliz/BarsaCanavari`) uygulayıcılar tarafından **yazılmaz**.
- **Python yolu (zorunlu):** Sanal ortam `bist_tahmin`'i ana ağacın `src/` klasöründen editable yükler. Worktree içinde pytest koşarken her komut `PYTHONPATH=<worktree>/src` ile başlar; aksi halde ana ağacın kodu sınanır. İlk komutta `bist_tahmin.__file__` yolu raporlanır. (Bu kural pilot sırasında gerekçesiyle eklendi.)
- Görev bitince worktree kalır; birleştirme yalnızca insanın çalıştırdığı `tools/birlestir_k1.sh` ile yapılır (§4).

## 3. Zorlama katmanları

Kural metni tek başına yetmez; dört katman birlikte çalışır:

1. **Araç listesi (alt ajan tanımı):** Uygulayıcıda Edit/Write vardır, doğrulayıcıda yoktur. Lider ana oturumdur; "kod yazmaz" kuralı ana oturumun kendi davranışıyla sağlanır, bu yüzden lider kod değişikliğini alt ajana devreder.
2. **Proje izin ayarları (`.claude/settings.json`, deny):** ssh, scp, docker, `git push|pull|merge`, `git reset --hard`, `rm -rf`, `.env` okuma. Bu tek başına kapsamı çizmez ama sunucu ve ağ işlerini araç düzeyinde engeller.
3. **Kapsam kapısı (mekanik betik, `tools/kapsam_kapisi.sh`):** `tools/kapsam_kapisi.sh <taban-sha> <dal> <izin-dosyasi>` çalıştırılır. `git diff --name-only <taban-sha> <dal>` çıktısındaki her dosya izin dosyasında değilse dal doğrulayıcıya gitmeden geri çevrilir. Betik dosya yazmaz; lider de çalıştırabilir.
   - **İzin dosyası:** görevin yazılabilir kümesini tanımlayan düz metin dosyası. Her satır bir tam dosya yolu ya da `dizin/` ile biten bir önektir. `#` ile başlayan satırlar ve boş satırlar yok sayılır.
   - **Çıkış kodları:** `0` hepsi izinli; `1` izin dışı dosya var (her biri listelenir); `2` kullanım ya da ortam hatası (taban ya da dal yok, izin dosyası okunamıyor).

4. **Kapsam kancası (`PreToolUse`, `.claude/hooks/scope_guard.py`, `.claude/settings.json` içinde `hooks`):** her Edit, Write ve Bash çağrısından önce çalışır. Genel yasaklar (ssh, scp, docker, `git push|pull|merge`, `rm` ile özyineleme ve zorlama, `.env`) her modda geçerlidir. Kanca yapılandırması ve `.claude/hooks/`, `.claude/contracts/` ajan tarafından yazılamaz. Görev sözleşmesi (`BIST_TASK_CONTRACT`) verildiğinde yazma ve komutlar sözleşmedeki listeyle sınırlanır.

Katman 1–3 bash komutlarını ayrıştırmaz; kanca (4) sezgisel bir denetim ekler ve bilinen sınırları vardır (betik başlığında yazılı). Bu yüzden doğrulayıcı kanıtı bağımsız olarak yeniden üretir (§5).

## 4. Onay kapıları

**Tek onay kapısı: K1 — birleştirme.** Bir dal ana dala ya da `faz-*` zincirine girmeden önce insan karar verir. Kapı açılmadan:

| Koşul | Kim kontrol eder |
|---|---|
| Kapsam kapısı: diff izin dosyasının içinde (`tools/kapsam_kapisi.sh`) | Mekanik betik |
| Yasak komut ya da dosya kullanılmamış (uygulayıcı raporu + doğrulayıcı) | Doğrulayıcı |
| Hedefli testler ve tam takım çıkış kodu 0 (`echo $?`) | Doğrulayıcı |
| Doğrulayıcının açık "DOĞRULANDI" dışında bulgusu yok; "KANIT YOK" ve "ÇELİŞİYOR" sıfır | Lider özet yazar, doğrulayıcı raporuna bakar |
| Lider özeti: değişen dosyalar, neden, test sayıları, açık riskler | Lider |

**K1 birleştirme yalnızca `tools/birlestir_k1.sh` ile yapılır.** Bu betiği yalnızca insan çalıştırır: `K1_ONAY=<dal> tools/birlestir_k1.sh <dal> <izin-dosyasi>`. Betik sırayla şunları denetler: `K1_ONAY` dal adıyla birebir eşleşiyor mu; kapsam kapısı geçiyor mu; ana ağaçta commit edilmemiş değişiklik ile dal arasında dosya kesişimi var mı. Hepsi geçerse `git merge --no-ff` yapar. Push etmez. Lider ve ajanlar `git merge` çalıştırmaz; `.claude/settings.json` içindeki `Bash(git merge:*)` yasağı kalır.

**Döngünün dışında kalanlar (kapı değil, yasak):**

- **Sunucuya yazan her adım** (imaj, migration, veri yükleme, cron, dosya): hiçbir alt ajanın aracı yok. Lider bir çalıştırma planı yazar (kuru çalıştırma komutu, geri dönüş etiketi, cron saatleri ile çakışma kontrolü); insan çalıştırır. CLAUDE.md'deki sunucu kuralları geçerlidir.
- **Dış veri ve ağ** (yeni kaynak, ham fiyatın dışarı çıkması, lisans etkisi): alt ajanlar erişemez. Karar insanındır; PROJE Bölüm 4 ve 18 geçerlidir.

Ara adımlar (test yazmak, yeniden düzenleme, rapor üretmek, yerel deney) için onay gerekmez.

**Kapsam dışı görülürse:** Uygulayıcı kapsamı kendi başına genişletmez. Durur, lidere önerisini yazar; lider gerekirse insana sorar.

### Kapsam dışı talep

Uygulayıcı izin dışındaki bir dosyaya ihtiyaç duyarsa o dosyayı kendisi değiştirmez. Üç satır yazar:

- **Neden:** dosyanın görev için neden gerektiği.
- **Dosya:** tam yol.
- **Risk:** dosya değiştirilmezse ya da değiştirilirse ne olur.

Lider bu üç satırı insana iletir. İnsan tek kelimeyle yanıt verir: `ONAY` ya da `RED`. Onaysız kapsam dışına çıkılmaz. Onaylanırsa izin dosyası güncellenir ve kapsam kapısı yeniden çalıştırılır; reddedilirse dosya dala girmez.

Bu konu açık: CLAUDE.md:35 ile K1 uyumu kullanıcı kararı bekliyor. 2026-11-19'da beş dal kullanıcı onayıyla faz-4'e birleşti; kanca birleştirmeyi engellediği için birleştirme insan tarafından yapılır.

## 5. Doğrulayıcı kuralları

- **Bağımsızlık:** Doğrulayıcı, doğrulanan görevi yazmamış olmalı; yalnızca SHA ve iddiayı alır, uygulayıcının akıl yürütmesini görmez.
- **Kanıt (her bulgu için üç parça, hepsi commit SHA'lı):**
  1. `<sha>:<dosya>:<satır>` ve satırın iddiayla karşılaştırması,
  2. Bulguyu yeniden üreten komut,
  3. Komutun gerçek çıktısı ve çıkış kodu.
- **Sınıflandırma:** DOĞRULANDI · KANIT YOK · ÇELİŞİYOR · DOĞRULANAMADI (ortam). Üç kanıttan biri eksikse bulgu reddedilir; "geçersiz" ile "doğrulanamadı" raporda ayrı görünür.
- **Sayılar:** Doğrulayıcı uygulayıcının sayılarını kopyalamaz; kendi komutunu çalıştırır.

## 6. Bitiş koşulu ve döngü sınırı

- **Bitti:** K1 koşulları sağlanmış ve açık "KANIT YOK / ÇELİŞİYOR" bulgusu yoksa.
- **Tur:** Uygulayıcı → mekanik kapı → doğrulayıcı → (bulgu varsa) düzeltme. Bir tur bu dört adımın tamamı.
- **Durdurma:** İki turda bulgu kapanmıyorsa döngü durur; lider özet yazıp insana döner. Üçüncü tura geçilmez.
- Ajanın kendi "tamam" demesi bitiş sayılmaz.
- Her alt ajan görevinde zaman sınırı vardır (komut başına `timeout`). Bir ajan sessizce takılırsa lider onu beklemez; durumu `task.md`'den okuyup yeniden başlatır.

## 7. Durum dosyası ve kesintiye dayanıklılık

- Her görevin durumu `task.md` içinde tutulur: görev adı, dal, taban SHA, son çalıştırılan komut, sonuç, açık bulgu.
- Görevler **yeniden başlatılabilir** (idempotent): worktree zaten varsa yeniden oluşturulmaz, dal zaten varsa yeniden açılmaz; komutlar önce durum kontrolü yapar.
- Oturum ya da sunucu kesilirse lider `task.md`'den devam eder; yarım kalan işi sessizce tekrarlamaz, önce `git -C <wt> status` ve `git log` ile durumu doğrular.
- Alt ajan sonuçları oturum kesilince kaybolabilir; kalıcı durum yalnızca dosyalardadır.
- Arka plan izleyicisi (`Monitor`, `ScheduleWakeup`) ile bekleme yapılmaz; bağımlılık yalnızca görev sonucudur.

## 8. Görev şablonu

```markdown
### Görev: <kısa ad>
- Dal: agent/<ad>; worktree: ../wt-<ad>
- Taban SHA: <sha>
- Hedef: <tek cümle, ölçülebilir>
- Okunabilir: <yollar>
- İzin dosyası: tools/... ya da görev dalına özel dosya
- İzinli komutlar: PYTHONPATH'li pytest; git status|diff|log|add|commit (yalnızca kendi dalında)
- Yasak: §3 deny listesi + bu göreve özel olanlar
- Kabul: <hedefli testler çıkış 0; tam takım lider/doğrulayıcı tarafından; sayılar>
- Kapsam dışı görülürse: dur, lidere bildir
- Rapor: değişen dosyalar, komutlar ve çıkış kodları, import kaynağı, açık riskler
```

## 9. Geçmişten dersler (bu projede yaşandı)

- **Çıkış kodunu kontrol etmeden commit:** 14 hatalı testle bir commit atıldı. Kural: commit öncesi `echo $?` = 0.
- **Ajan "tamam" dedi, doğrulama farklıydı:** Doğrulayıcı ve tam takım kuralı bundan doğdu.
- **Yarım kalan ajanlar:** Sunucu yeniden başladığında üç ajan bildirimsiz kesildi. Durum dosyası ve idempotent görevler bu yüzden zorunlu.
- **Kapsam kayması:** Bir ajan kendi başına sunucu ve veri üzerinde iş yaptı; bu yüzden sunucu ve ağ işleri alt ajan araçlarından çıkarıldı.
- **Geçici disk:** Tam takım `/tmp` (tmpfs) dolunca gömülü Postgres kurtarma moduna girdi. Her tam takım koşusu `TMPDIR` ayarlı olmalı.
- **Yanlış kod sınandı:** Editable kurulum nedeniyle worktree testleri ana ağacın kodunu import etti. `PYTHONPATH` kuralı bundan doğdu (§2).
- **Holdout:** Mekanik test gerçek sınama kümesini tüketebilirdi. Holdout'a dokunan her görev, lidere ve insana açıkça belirtilir (`CLAUDE.md`).
- **Alt ajan türleri oturumda yüklenir (sürüm 3):** `.claude/agents/*.md` tanımları oturum başında yüklenir; oturum yeniden başlatılmadan tür olarak çağrılamaz. Bu oturumda genel ajan ve bu dosyanın kuralları kullanıldı; araç kısıtı zorlanmadı.
