#!/usr/bin/env bash
# Hedefli pytest kosusu icin tek, sabit giris noktasi.
# Kullanim: scripts/verify_task.sh <pytest-hedefleri...>
#   hedef: tests/dosya.py veya tests/dosya.py::test_adi (depo koküne göre)
# Cikis: pytest'in cikis kodu aynen; kullanim/dogrulama hatasi 2.
# Son satir: VERIFY_EXIT=<kod>
set -u

usage() {
  echo "Kullanım: verify_task.sh <pytest-hedefleri...>" >&2
  echo "  ornek: verify_task.sh tests/test_metrics.py" >&2
  echo "  ornek: verify_task.sh tests/test_metrics.py::test_brier_constant_half_is_quarter" >&2
  exit 2
}

[ "$#" -ge 1 ] || usage

# Ag testleri bu betikte yasak; degisken tanimliysa (bos olsa bile) reddet.
if [ -n "${BIST_NETWORK_TESTS+x}" ]; then
  echo "HATA: BIST_NETWORK_TESTS ayarli; ag testleri bu betikte calistirilmaz." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)" || exit 2
ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)" || exit 2

# Hedef dogrulama: kapsam disi yol ya da bayrak enjeksiyonu yok; dosya var olmali.
for t in "$@"; do
  case "$t" in
    -*) echo "HATA: bayrak benzeri hedef kabul edilmez: $t" >&2; exit 2 ;;
    /*) echo "HATA: mutlak yol kabul edilmez: $t" >&2; exit 2 ;;
    *..*) echo "HATA: '..' iceren hedef kabul edilmez (kapsam disi yol): $t" >&2; exit 2 ;;
  esac
  file="${t%%::*}"
  case "$file" in
    *.py) ;;
    *) echo "HATA: hedef .py dosyasi olmali: $t" >&2; exit 2 ;;
  esac
  if [ ! -f "$ROOT/$file" ]; then
    echo "HATA: hedef dosya yok: $file" >&2
    exit 2
  fi
  if [ "$file" != "$t" ]; then
    # dosya::test_adi biciminde: fonksiyon adi (son bilesen, [parametre] haric) dosyada tanimli olmali.
    name="${t##*::}"
    name="${name%%\[*}"
    if [ -z "$name" ]; then
      echo "HATA: test adi bos: $t" >&2
      exit 2
    fi
    if ! grep -Fq "def ${name}(" "$ROOT/$file"; then
      echo "HATA: test bulunamadi: $t" >&2
      exit 2
    fi
  fi
done

# Python: once worktree'nin kendi .venv'i, yoksa ana agacin sanal ortami.
if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
elif [ -x /home/yeliz/BarsaCanavari/.venv/bin/python ]; then
  PY=/home/yeliz/BarsaCanavari/.venv/bin/python
else
  echo "HATA: Python sanal ortami bulunamadi (.venv yok)." >&2
  exit 2
fi

TMP_DIR="$HOME/.cache/bist-pytest-tmp"
mkdir -p "$TMP_DIR" || { echo "HATA: gecici dizin olusturulamadi: $TMP_DIR" >&2; exit 2; }
export TMPDIR="$TMP_DIR"
export PYTHONPATH="$ROOT/src"

# Import kaynagi kaniti: ilk cikti satiri; bist_tahmin mutlaka bu depo src/ altindan gelmeli.
imp="$(cd "$ROOT" && "$PY" -c 'import bist_tahmin; print(bist_tahmin.__file__)')" || {
  echo "HATA: bist_tahmin import edilemedi (PYTHON=$PY)." >&2
  exit 2
}
echo "$imp"
case "$imp" in
  "$ROOT/src/"*) ;;
  *) echo "HATA: import kaynagi bu worktree src/ altinda degil: $imp" >&2; exit 2 ;;
esac
echo "PYTHON: $PY"

cd "$ROOT" || exit 2
nice -n 15 timeout 600 "$PY" -m pytest -p no:cacheprovider -o addopts="" -q "$@"
rc=$?
echo "VERIFY_EXIT=$rc"
exit "$rc"
