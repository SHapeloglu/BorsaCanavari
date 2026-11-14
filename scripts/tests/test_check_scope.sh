#!/usr/bin/env bash
# check_scope.sh icin kendi kendini test eden kanit.
# Gecici bir git deposu kurar (taban + dal commit'i), dort senaryo dener:
#   (a) hepsi izinli -> 0   (b) bir dosya izinsiz -> 1
#   (c) bos fark -> 1       (d) gecersiz SHA -> 2
# Biri tutmazsa 1 ile cikar. Gecici dizin trap ile silinir.
set -u
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
CHECK="$HERE/../check_scope.sh"
if [ ! -f "$CHECK" ]; then
  echo "HATA: $CHECK bulunamadi" >&2
  exit 1
fi

TMPROOT="$(mktemp -d)" || exit 1
cleanup() {
  if [ -n "$TMPROOT" ] && [ -d "$TMPROOT" ]; then
    rm -r -- "$TMPROOT"
  fi
}
trap cleanup EXIT

REPO="$TMPROOT/repo"
mkdir -p "$REPO" || exit 1
cd "$REPO" || exit 1

git init -q -b main . || exit 1
G=(git -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false)

# Taban commit
echo base > README.md
"${G[@]}" add README.md || exit 1
"${G[@]}" commit -q -m base || exit 1
BASE="$(git rev-parse HEAD)" || exit 1

# Dal commit: izinli dosyalar (scripts/, tests/) ve bosluklu bir yol
git checkout -q -b feat-ok || exit 1
mkdir -p scripts tests || exit 1
echo a > scripts/a.sh
echo b > "scripts/bosluk adi.sh"
echo c > tests/t.py
git add -A || exit 1
"${G[@]}" commit -q -m feat || exit 1

FAILS=0

expect_rc() {
  local name="$1" want="$2" got="$3"
  if [ "$got" -eq "$want" ]; then
    echo "PASS $name (cikis $got)"
  else
    echo "FAIL $name: beklenen cikis $want, gercek $got" >&2
    FAILS=$((FAILS + 1))
  fi
}

expect_text() {
  local name="$1" needle="$2" hay="$3"
  case "$hay" in
    *"$needle"*) echo "PASS $name (cikti icinde: $needle)" ;;
    *) echo "FAIL $name: cikti icinde yok: $needle" >&2; echo "$hay" >&2; FAILS=$((FAILS + 1)) ;;
  esac
}

run_check() {
  OUT="$("$CHECK" "$@" 2>&1)"
  RC=$?
}

# (a) hepsi izinli
run_check "$BASE" feat-ok 'scripts/*' 'tests/*'
expect_rc "(a) hepsi izinli" 0 "$RC"
expect_text "(a) tamam mesaji" "KAPSAM TAMAM (3 dosya)" "$OUT"

# (b) tests/t.py izinli listede yok -> izinsiz
run_check "$BASE" feat-ok 'scripts/*'
expect_rc "(b) bir dosya izinsiz" 1 "$RC"
expect_text "(b) kapsam disi satiri" "KAPSAM DIŞI: tests/t.py" "$OUT"

# (c) bos fark (taban = dal)
run_check "$BASE" "$BASE" 'scripts/*'
expect_rc "(c) bos fark" 1 "$RC"
expect_text "(c) degisiklik yok mesaji" "DEGISIKLIK YOK" "$OUT"

# (d) gecersiz SHA
run_check deadbeefdeadbeefdeadbeefdeadbeefdeadbeef feat-ok 'scripts/*'
expect_rc "(d) gecersiz SHA" 2 "$RC"

if [ "$FAILS" -ne 0 ]; then
  echo "TEST BASARISIZ: $FAILS kontrol tutmadi" >&2
  exit 1
fi
echo "TUM SENARYOLAR GECTI"
exit 0
