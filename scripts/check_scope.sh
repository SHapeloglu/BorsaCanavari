#!/usr/bin/env bash
# Bir dalin degisen dosyalarinin gorev yazma listesi icinde olup olmadigini denetler.
# Kullanim: scripts/check_scope.sh <taban-sha> <dal> <glob...>
#   glob'lar bash case kalibi olarak uygulanir; tirnakla verin (ornek: 'scripts/*').
# Cikis: 0 kapsam tamam; 1 kapsam disi dosya ya da bos fark; 2 kullanim ya da git hatasi.
set -u

if [ "$#" -lt 3 ]; then
  echo "Kullanım: check_scope.sh <taban-sha> <dal> <glob...>" >&2
  exit 2
fi

base="$1"
branch="$2"
shift 2

case "$base" in -*|"") echo "HATA: geçersiz taban: $base" >&2; exit 2 ;; esac
case "$branch" in -*|"") echo "HATA: geçersiz dal: $branch" >&2; exit 2 ;; esac

tmp="$(mktemp)" || exit 2
trap 'rm -f -- "$tmp"' EXIT

# -z: yollar NUL ile ayrilir (bosluk/ozel karakter guvenli); --no-renames: yeniden adlandirmada
# eski ve yeni yol ayri ayri denetlenir.
if ! git diff -z --name-only --no-renames "$base..$branch" -- > "$tmp"; then
  echo "HATA: git diff basarisiz ($base..$branch)" >&2
  exit 2
fi

n=0
bad=0
while IFS= read -r -d '' f; do
  n=$((n + 1))
  ok=0
  for pat in "$@"; do
    case "$f" in
      $pat) ok=1; break ;;
    esac
  done
  if [ "$ok" -eq 0 ]; then
    echo "KAPSAM DIŞI: $f"
    bad=$((bad + 1))
  fi
done < "$tmp"

if [ "$n" -eq 0 ]; then
  echo "DEGISIKLIK YOK"
  exit 1
fi
if [ "$bad" -gt 0 ]; then
  echo "KAPSAM BAŞARISIZ ($bad / $n dosya kapsam dışı)"
  exit 1
fi
echo "KAPSAM TAMAM ($n dosya)"
exit 0
