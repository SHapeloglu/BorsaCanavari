#!/usr/bin/env bash
# K1 birlestirme betigi. YALNIZCA INSAN CALISTIRIR; ajanlar calistirmaz (ORCHESTRATION.md §4).
# Kullanim: K1_ONAY=<dal-adi> tools/birlestir_k1.sh <dal> <izin-dosyasi>
#
# Adimlar:
#   (a) K1_ONAY degeri <dal> ile birebir eslesmeli;
#   (b) kapsam kapisi (tools/kapsam_kapisi.sh) gecmeli; taban = ana agac HEAD ile dalin ortak atasi;
#   (c) ana agacta commit edilmemis degisiklik ile dal arasinda dosya kesisimi olmamali;
#   (d) git merge --no-ff <dal>. Push YAPMAZ.
# Ana agac: ANA_AGAC ortam degiskeni (varsayilan /home/yeliz/BarsaCanavari).
set -euo pipefail

die() {
  echo "DUR: $*" >&2
  exit 1
}

if [ "$#" -ne 2 ]; then
  echo "kullanim: K1_ONAY=<dal> $0 <dal> <izin-dosyasi>" >&2
  exit 2
fi

branch="$1"
allow_file="$2"
ana="${ANA_AGAC:-/home/yeliz/BarsaCanavari}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
kapsam="${script_dir}/kapsam_kapisi.sh"

# (a) Insan onayi: K1_ONAY tam olarak dal adina esit olmali.
if [ "${K1_ONAY:-}" != "${branch}" ]; then
  die "K1 onayi eksik ya da eslesmiyor (K1_ONAY='${K1_ONAY:-}', dal='${branch}')."
fi

if [ ! -f "${allow_file}" ]; then
  echo "HATA: izin dosyasi yok: ${allow_file}" >&2
  exit 2
fi
allow_abs="$(readlink -f -- "${allow_file}")"

if ! git -C "${ana}" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "HATA: ana agac git deposu degil: ${ana}" >&2
  exit 2
fi
git -C "${ana}" rev-parse --verify --quiet "${branch}^{commit}" >/dev/null \
  || die "dal bulunamadi: ${branch}"
base="$(git -C "${ana}" merge-base HEAD "${branch}")" \
  || die "ortak ata bulunamadi (HEAD ile ${branch})"

# (b) Kapsam kapisi. Betik cwd'deki depoda calisir; alt kabukta ana agac secilir.
if ! (cd "${ana}" && bash "${kapsam}" "${base}" "${branch}" "${allow_abs}"); then
  die "kapsam kapisi basarisiz (taban ${base}); birlestirme yapilmadi."
fi

# (c) Commit edilmemis degisiklik ile dal arasinda kesisim olmamali.
branch_files="$(git -C "${ana}" diff --name-only "${base}" "${branch}")"
local_files="$(git -C "${ana}" diff --name-only; git -C "${ana}" diff --cached --name-only)"
overlap=""
while IFS= read -r f; do
  [ -z "${f}" ] && continue
  if grep -Fxq -- "${f}" <<< "${branch_files}"; then
    overlap+="${f}"$'\n'
  fi
done <<< "${local_files}"
if [ -n "${overlap}" ]; then
  printf 'KESISIM (commit edilmemis degisiklik, dal ile ayni dosya):\n%s' "${overlap}" >&2
  die "ana agactaki degisiklikleri once commit et ya da geri al; insan karari."
fi

# (d) Birlestirme. Hata olursa betik geri almaz; durumu insan inceler.
echo "Birlestiriliyor: ${branch} -> HEAD ($(git -C "${ana}" branch --show-current || true))"
if ! git -C "${ana}" merge --no-ff -m "Birlestir: ${branch} (K1 onayi)" "${branch}"; then
  die "git merge basarisiz (cakisma olabilir). Durum: git -C ${ana} status. Betik geri almadi."
fi

git -C "${ana}" log --oneline -1
echo "Birlestirme tamam. Push YAPILMADI; gerekiyorsa insan elle yapar."
