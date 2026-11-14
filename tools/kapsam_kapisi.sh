#!/usr/bin/env bash
# Kapsam kapisi: mekanik kontrol (ORCHESTRATION.md §3, katman 3).
# Kullanim: tools/kapsam_kapisi.sh <taban-sha> <dal> <izin-dosyasi>
#
# <taban-sha> ile <dal> arasinda degisen dosyalar (git diff --name-only) izin
# dosyasiyla karsilastirilir. Izin dosyasi: her satir bir tam dosya yolu ya da
# 'dizin/' ile biten bir onek. '#' ile baslayan ve bos satirlar yok sayilir.
#
# Cikis kodlari: 0 hepsi izinli; 1 izin disi dosya var (listelenir);
#                2 kullanim ya da ortam hatasi (taban/dal yok, izin dosyasi okunamiyor).
# Dosya yazmaz. Git deposu cwd'de olmalidir.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "kullanim: $0 <taban-sha> <dal> <izin-dosyasi>" >&2
  exit 2
fi

base="$1"
branch="$2"
allow_file="$3"

if ! git rev-parse --verify --quiet "${base}^{commit}" >/dev/null; then
  echo "HATA: taban bulunamadi: ${base}" >&2
  exit 2
fi
if ! git rev-parse --verify --quiet "${branch}^{commit}" >/dev/null; then
  echo "HATA: dal bulunamadi: ${branch}" >&2
  exit 2
fi
if [ ! -f "${allow_file}" ] || [ ! -r "${allow_file}" ]; then
  echo "HATA: izin dosyasi okunamadi: ${allow_file}" >&2
  exit 2
fi

# Dosya izin listesinde mi? Dizin girdisi (sonu '/') onek olarak eslesir.
is_allowed() {
  local file="$1" entry
  while IFS= read -r entry || [ -n "${entry}" ]; do
    entry="${entry%$'\r'}"
    case "${entry}" in
      ''|'#'*) continue ;;
    esac
    if [[ "${entry}" == */ ]]; then
      if [[ "${file}" == "${entry}"* ]]; then
        return 0
      fi
    elif [ "${file}" = "${entry}" ]; then
      return 0
    fi
  done < "${allow_file}"
  return 1
}

# core.quotePath=false: Turkce karakterli yollar tirnakli gelmesin diye.
changed="$(git -c core.quotePath=false diff --name-only "${base}" "${branch}")" \
  || { echo "HATA: git diff basarisiz (${base}..${branch})" >&2; exit 2; }

violations=0
checked=0
while IFS= read -r file; do
  [ -z "${file}" ] && continue
  checked=$((checked + 1))
  if ! is_allowed "${file}"; then
    echo "IZIN DISI: ${file}"
    violations=$((violations + 1))
  fi
done <<< "${changed}"

if [ "${violations}" -gt 0 ]; then
  echo "KAPSAM KAPISI BASARISIZ: ${violations}/${checked} dosya izin listesinde degil (${base}..${branch})"
  exit 1
fi

echo "KAPSAM KAPISI TAMAM: ${checked} dosya, hepsi izinli (${base}..${branch})"
exit 0
