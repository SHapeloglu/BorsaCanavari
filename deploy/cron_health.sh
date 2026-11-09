#!/bin/bash
# BIST tahmin: saglik kontrolu sarmalayicisi (hafta ici 20:30 Istanbul; deploy/bist-tahmin.cron).
# `bist health` cikis kodu: 0 saglikli, 1 uyari, 2 kritik. Cikis != 0 ise (docker/zaman asimi hatalari dahil):
#   - $DIR/logs/ALERT.txt'ye zaman damgali TEK satir eklenir (okuyan kisi dosyayi izler; sorun giderilince elle silinir),
#   - `logger -t bist-tahmin` ile syslog'a yazilir.
# Basari halinde ALERT.txt'ye DOKUNULMAZ. Tam cikti her zaman $DIR/logs/health.log'a gider.
# Ortam: BIST_DIR (varsayilan /opt/bist-tahmin; testler icin), ONLY_IST_HOUR (cron_run.sh ile ayni saat korumasi).
set -u
if [ -n "${ONLY_IST_HOUR:-}" ] && [ "$(TZ=Europe/Istanbul date +%H)" != "$(printf '%02d' "$((10#$ONLY_IST_HOUR))")" ]; then
  exit 0
fi
DIR="${BIST_DIR:-/opt/bist-tahmin}"
LOG="$DIR/logs/health.log"
ALERT="$DIR/logs/ALERT.txt"
mkdir -p "$DIR/logs"
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then mv -f "$LOG" "$LOG.1"; fi
cd "$DIR" || exit 2
STAMP="$(date '+%F %T %Z')"
OUT="$(timeout 5m docker compose run --rm --no-deps -T bist health 2>&1)"
RC=$?
{
  echo "=== $STAMP basla: bist health ==="
  printf '%s\n' "$OUT"
  echo "=== $(date '+%F %T %Z') bitti: cikis kodu $RC ==="
} >> "$LOG"
if [ "$RC" -ne 0 ]; then
  # sorunlu kontrol satirlarini tek satira topla; hic yoksa (docker hatasi vb.) son satirlari al
  SUMMARY="$(printf '%s\n' "$OUT" | grep -E '^\[(UYARI|KRITIK)\]|^(KRITIK|hata)' | head -n 6 | tr '\n' '|' | sed 's/|$//; s/|/ | /g')"
  [ -z "$SUMMARY" ] && SUMMARY="$(printf '%s\n' "$OUT" | tail -n 3 | tr '\n' ' ')"
  LINE="$STAMP bist health cikis=$RC: $SUMMARY"
  printf '%s\n' "$LINE" >> "$ALERT"
  if command -v logger >/dev/null 2>&1; then logger -t bist-tahmin -- "$LINE" || true; fi
fi
exit "$RC"
