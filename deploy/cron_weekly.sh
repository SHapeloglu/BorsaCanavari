#!/bin/bash
# Haftalik rapor: markdown'i KONAKTA /opt/bist-tahmin/reports/ altina yazar (konteyner icine yazilirsa kaybolur).
# Salt okunur bir islemdir (veritabanina yazmaz). Rapor yatirim tavsiyesi degildir.
set -u
# Saat korumasi: cron (Debian 3.0pl1) CRON_TZ desteklemez ve sunucu Europe/Berlin'dedir (yaz/kis saati degisir).
# Satirlar Berlin saatiyle IKI olasi saat icin konur; bu kontrol yalnizca gercek Istanbul saati eslesince calistirir.
if [ -n "${ONLY_IST_HOUR:-}" ] && [ "$(TZ=Europe/Istanbul date +%H)" != "$(printf '%02d' "$((10#$ONLY_IST_HOUR))")" ]; then
  exit 0
fi
DIR=/opt/bist-tahmin
OUT="$DIR/reports/rapor-$(date +%F).md"
LOG="$DIR/logs/weekly-report.log"
mkdir -p "$DIR/logs" "$DIR/reports"
cd "$DIR" || exit 2
{
  echo "=== $(date '+%F %T %Z') basla: bist report weekly ==="
  timeout 15m docker compose run --rm --no-deps -T bist report weekly > "$OUT.tmp"
  RC=$?
  if [ "$RC" -eq 0 ]; then mv -f "$OUT.tmp" "$OUT"; echo "rapor: $OUT"; else rm -f "$OUT.tmp"; fi
  echo "=== $(date '+%F %T %Z') bitti: cikis kodu $RC ==="
  exit $RC
} >> "$LOG" 2>&1
