#!/bin/bash
# BIST tahmin: cron sarmalayicisi. Kullanim: cron_run.sh <ad> <bist alt komutu ve argumanlari...>
#   ornek: cron_run.sh run-daily run-daily
#          RETRY=1 cron_run.sh run-daily run-daily   (bugun zaten basariliysa hicbir sey yapmaz)
#          ONLY_IST_HOUR=19 ...                      (yalniz Istanbul saati 19:xx ise calisir; asagidaki not)
# - flock: ayni isin iki kopyasi ust uste binmez (uygulama ici advisory lock'a ek, ucuz ilk savunma)
# - timeout: takilan bir calistirma advisory lock'u ve baglantiyi suresiz tutmasin
# - cikis kodu log'a yazilir: 0 tamam, 1 kismi sorun, 3 bayat/gec, 4 kilitli, 124 zaman asimi
set -u
# Saat korumasi: cron (Debian 3.0pl1) CRON_TZ desteklemez ve sunucu Europe/Berlin'dedir (yaz/kis saati degisir).
# Satirlar Berlin saatiyle IKI olasi saat icin konur; bu kontrol yalnizca gercek Istanbul saati eslesince calistirir.
if [ -n "${ONLY_IST_HOUR:-}" ] && [ "$(TZ=Europe/Istanbul date +%H)" != "$(printf '%02d' "$((10#$ONLY_IST_HOUR))")" ]; then
  exit 0
fi
NAME="$1"; shift
DIR=/opt/bist-tahmin
LOG="$DIR/logs/$NAME.log"
OKMARK="$DIR/logs/$NAME.ok-$(date +%F)"
mkdir -p "$DIR/logs" "$DIR/reports"
find "$DIR/logs" -name "$NAME.ok-*" -mtime +7 -delete 2>/dev/null
# yeniden deneme modu: bugun basariyla bittiyse atla
if [ "${RETRY:-0}" = "1" ] && [ -e "$OKMARK" ]; then
  echo "=== $(date '+%F %T %Z') atlandi: bugun zaten basarili ($NAME) ===" >> "$LOG"
  exit 0
fi
# log 5 MB'i asarsa bir kopya sakla (basit donusum)
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5242880 ]; then mv -f "$LOG" "$LOG.1"; fi
cd "$DIR" || exit 2
{
  echo "=== $(date '+%F %T %Z') basla: bist $* ==="
  flock -n "$DIR/logs/$NAME.lock" \
    timeout 45m docker compose run --rm --no-deps -T bist "$@"
  RC=$?
  echo "=== $(date '+%F %T %Z') bitti: cikis kodu $RC ==="
  [ "$RC" -eq 0 ] && touch "$OKMARK"
  exit $RC
} >> "$LOG" 2>&1
