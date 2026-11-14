#!/usr/bin/env python3
# PreToolUse kancasi: ajanin kapsamini makine tarafinda zorlar.
#
# Girdi : stdin'de JSON, alanlar "tool_name" ve "tool_input".
# Cikis : 0 = izin, 2 = engel (neden stderr'e yazilir). Baska kod kullanilmaz.
#
# Lider modu (BIST_TASK_CONTRACT tanimli degil):
#   Edit/Write/MultiEdit/NotebookEdit yalnizca KOD_YOLLARI altinda engellenir.
#   Bash icin yalnizca genel yasaklar uygulanir.
# Sozlesme modu (BIST_TASK_CONTRACT = sozlesme JSON dosyasinin yolu):
#   Yazma yalnizca "write" globlarina izinli.
#   Bash yalnizca "commands" onekleriyle baslayan komutlara izinli.
#   "deny" ek regex listesi Bash komutuna ve yazma yoluna uygulanir.
#   "read" alani bilgi amaclidir; kanca tarafindan zorlanmaz.
# Genel yasaklar (her modda, Bash): ssh, scp, docker, git push/pull/merge,
#   git reset --hard, rm -rf, .env dosyasi kullanimi.
# Hata durumu (JSON, sozlesme, beklenmeyen istisna): kapali basarisizlik, exit 2.

import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional

EXIT_IZIN = 0
EXIT_ENGEL = 2

YAZMA_ARACLARI = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
BASH_ARACI = "Bash"

KOD_YOLLARI = ("src/", "tests/", "scripts/", "deploy/", ".claude/hooks/")

SSH_BENZERI = frozenset({"ssh", "scp", "sshpass"})
DOCKER_DESENI = re.compile(r"docker(-compose)?")
ENV_DOSYASI_DESENI = re.compile(r"\.env(\..+)?")
YONLENDIRME_ONEKI = re.compile(r"^[0-9]*[<>&]+")
ATAMA_DESENI = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
KABUK_ISIMLERI = frozenset({"sh", "bash", "zsh", "dash"})
GIT_ARGUMANLI_SECENEKLER = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
GIT_YASAK_ALT_KOMUTLAR = frozenset({"push", "pull", "merge"})
MAX_KABUK_DERINLIGI = 4


class Engel(Exception):
    """Kapsam disi islem: stderr'e 'KAPSAM DIŞI:' ile yazilir, exit 2."""


class KancaHatasi(Exception):
    """Kancanin kendi hatasi (JSON, sozlesme okuma vb.): exit 2."""


class Kontrat:
    """Gorev sozlesmesi. 'read' bilgi amaclidir; zorlanmaz."""

    def __init__(self, veri):
        if not isinstance(veri, dict):
            raise KancaHatasi("sozlesme JSON nesnesi degil")
        for anahtar in ("id", "base_sha"):
            if not isinstance(veri.get(anahtar), str):
                raise KancaHatasi(f"sozlesme '{anahtar}' metin olmali")
        self.read = _metin_listesi(veri, "read")
        self.write = _metin_listesi(veri, "write")
        self.commands = _metin_listesi(veri, "commands")
        try:
            self.deny = [re.compile(r) for r in _metin_listesi(veri, "deny")]
        except re.error as hata:
            raise KancaHatasi(f"sozlesme 'deny' regex hatali: {hata}")
        try:
            onekler = [shlex.split(c) for c in self.commands]
        except ValueError as hata:
            raise KancaHatasi(f"sozlesme 'commands' ayrıştırılamadı: {hata}")
        # Bos onek her komutla eslesir; bu yuzden atilir.
        self.onekler = [o for o in onekler if o]


def _metin_listesi(veri: Dict, anahtar: str) -> List[str]:
    deger = veri.get(anahtar, [])
    if not isinstance(deger, list) or not all(isinstance(x, str) for x in deger):
        raise KancaHatasi(f"sozlesme '{anahtar}' metin listesi olmali")
    return deger


def proje_koku(ortam) -> str:
    kok = ortam.get("CLAUDE_PROJECT_DIR") or str(Path(__file__).resolve().parents[2])
    return os.path.realpath(kok)


def kontrat_yukle(ortam, root: str) -> Optional[Kontrat]:
    ham = ortam.get("BIST_TASK_CONTRACT", "")
    if not ham:
        return None
    yol = ham if os.path.isabs(ham) else os.path.join(root, ham)
    try:
        with open(yol, encoding="utf-8") as dosya:
            veri = json.load(dosya)
    except (OSError, ValueError) as hata:
        raise KancaHatasi(f"sozlesme okunamadi ({ham}): {hata}")
    return Kontrat(veri)


def goreli_yol(yol: str, root: str) -> Optional[str]:
    """Proje kokune gore posix yolu. Proje disindaysa None."""
    mutlak = yol if os.path.isabs(yol) else os.path.join(root, yol)
    gercek = os.path.realpath(mutlak)
    try:
        rel = os.path.relpath(gercek, root)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


# ---------------------------------------------------------------- Bash ayristirma

def _ayirici_uzunlugu(komut: str, i: int, cift: bool) -> int:
    """i konumunda komut ayiricisi varsa uzunlugunu, yoksa 0 dondurur."""
    c = komut[i]
    if c == "`" or komut.startswith("$(", i):
        return 2 if c == "$" else 1  # tirnak icinde de calisir
    if cift:
        return 0
    if komut.startswith("&&", i) or komut.startswith("||", i):
        return 2
    if c in ";|\n()":
        return 1
    if c == "&":
        onceki = komut[i - 1] if i > 0 else ""
        sonraki = komut[i + 1] if i + 1 < len(komut) else ""
        if onceki in ("<", ">") or sonraki == ">":  # 2>&1 ve &> yonlendirmesi
            return 0
        return 1
    return 0


def komut_parcala(komut: str) -> List[str]:
    """Bileşik komutu tirnak disindaki ayiricilardan boler (; && || | ` $( ...)."""
    parcalar: List[str] = []
    tampon: List[str] = []
    tek = cift = False
    i = 0
    while i < len(komut):
        c = komut[i]
        if tek:  # tek tirnak icinde yalnizca kapanis onemli
            if c == "'":
                tek = False
            tampon.append(c)
            i += 1
            continue
        if c == "\\":
            tampon.append(komut[i:i + 2])
            i += 2
            continue
        if c == "'" and not cift:
            tek = True
        elif c == '"':
            cift = not cift
        else:
            uzunluk = _ayirici_uzunlugu(komut, i, cift)
            if uzunluk:
                parcalar.append("".join(tampon))
                tampon = []
                i += uzunluk
                continue
        tampon.append(c)
        i += 1
    parcalar.append("".join(tampon))
    return [p.strip() for p in parcalar if p.strip()]


def _tokenlar(parca: str) -> List[str]:
    try:
        return shlex.split(parca, posix=True)
    except ValueError:
        return parca.split()


def komut_tokenlari(tokenlar: List[str]) -> List[str]:
    """Komutun basindaki VAR=deger atamalarini atar."""
    i = 0
    while i < len(tokenlar) and ATAMA_DESENI.match(tokenlar[i]):
        i += 1
    return tokenlar[i:]


def _git_ihlali(kalan: List[str]) -> Optional[str]:
    i = 0
    while i < len(kalan):
        a = kalan[i]
        if a in GIT_ARGUMANLI_SECENEKLER:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    if i >= len(kalan):
        return None
    alt = kalan[i]
    if alt in GIT_YASAK_ALT_KOMUTLAR:
        return f"git {alt} yasak"
    if alt == "reset" and "--hard" in kalan[i + 1:]:
        return "git reset --hard yasak"
    return None


def _rm_ihlali(kalan: List[str]) -> Optional[str]:
    harfler = set()
    uzun = set()
    for a in kalan:
        if a.startswith("--"):
            uzun.add(a)
        elif a.startswith("-") and len(a) > 1:
            harfler.update(a[1:])
    if ("r" in harfler or "R" in harfler) and "f" in harfler:
        return "rm -rf yasak"
    if "--recursive" in uzun and "--force" in uzun:
        return "rm -rf yasak"
    return None


def _segment_ihlali(tokenlar: List[str], derinlik: int) -> Optional[str]:
    for t in tokenlar:
        taban = os.path.basename(YONLENDIRME_ONEKI.sub("", t))
        if taban in SSH_BENZERI or DOCKER_DESENI.fullmatch(taban):
            return f"{taban} komutu yasak"
        if ENV_DOSYASI_DESENI.fullmatch(taban):
            return ".env dosyası okunamaz ya da işlenemez"
    for i, t in enumerate(tokenlar):
        taban = os.path.basename(t)
        kalan = tokenlar[i + 1:]
        sebep: Optional[str] = None
        if taban == "git":
            sebep = _git_ihlali(kalan)
        elif taban == "rm":
            sebep = _rm_ihlali(kalan)
        elif taban in KABUK_ISIMLERI and "-c" in kalan:
            k = kalan.index("-c")
            if k + 1 < len(kalan):
                sebep = _genel_ihlal(kalan[k + 1], derinlik + 1)
        if sebep:
            return sebep
    return None


def _genel_ihlal(komut: str, derinlik: int = 0) -> Optional[str]:
    if derinlik > MAX_KABUK_DERINLIGI:
        return "cok fazla ic ice kabuk cagrisi"
    for parca in komut_parcala(komut):
        sebep = _segment_ihlali(_tokenlar(parca), derinlik)
        if sebep:
            return f"{sebep} [{parca[:200]}]"
    return None


# ---------------------------------------------------------------- karar

def _yazma_kontrol(arac: str, girdi: Dict, root: str, kontrat: Optional[Kontrat]) -> None:
    yol = girdi.get("file_path") or girdi.get("notebook_path")
    if not isinstance(yol, str) or not yol:
        raise Engel(f"{arac}: hedef dosya yolu belirlenemedi")
    rel = goreli_yol(yol, root)
    if kontrat is None:
        if rel is not None and rel.startswith(KOD_YOLLARI):
            raise Engel(
                f"lider modunda kod yolu '{rel}' dogrudan degistirilemez; "
                "is bir gorev sozlesmesiyle alt ajana verilmeli"
            )
        return
    if rel is None:
        raise Engel(f"'{yol}' proje kokunun disinda; sozlesmede yazma izni yok")
    for rx in kontrat.deny:
        if rx.search(rel) or rx.search(yol):
            raise Engel(f"sozlesmenin yasakli kalibi '{rx.pattern}' yol ile eslesti")
    if not any(fnmatch.fnmatchcase(rel, g) for g in kontrat.write):
        raise Engel(f"'{rel}' sozlesmenin yazilabilir listesinde degil")


def _bash_kontrol(girdi: Dict, kontrat: Optional[Kontrat]) -> None:
    komut = girdi.get("command")
    if not isinstance(komut, str):
        raise KancaHatasi("Bash: 'command' alani eksik ya da metin degil")
    sebep = _genel_ihlal(komut)
    if sebep:
        raise Engel(sebep)
    if kontrat is None:
        return
    for rx in kontrat.deny:
        if rx.search(komut):
            raise Engel(f"sozlesmenin yasakli kalibi '{rx.pattern}' komutla eslesti")
    for parca in komut_parcala(komut):
        tokenlar = komut_tokenlari(_tokenlar(parca))
        if not tokenlar:
            continue
        if not any(tokenlar[:len(o)] == o for o in kontrat.onekler):
            raise Engel(f"komut sozlesmede izinli degil [{parca[:200]}]")


def karar(veri, ortam) -> None:
    """Izin varsa doner; engel ya da kanca hatasinda istisna firlatir."""
    if not isinstance(veri, dict):
        raise KancaHatasi("girdi JSON nesnesi degil")
    arac = veri.get("tool_name")
    if not isinstance(arac, str):
        raise KancaHatasi("'tool_name' alani eksik ya da metin degil")
    girdi = veri.get("tool_input", {})
    if not isinstance(girdi, dict):
        raise KancaHatasi("'tool_input' nesne degil")
    if arac not in YAZMA_ARACLARI and arac != BASH_ARACI:
        return
    root = proje_koku(ortam)
    kontrat = kontrat_yukle(ortam, root)
    if arac == BASH_ARACI:
        _bash_kontrol(girdi, kontrat)
    else:
        _yazma_kontrol(arac, girdi, root, kontrat)


def main() -> int:
    try:
        try:
            veri = json.loads(sys.stdin.read())
        except ValueError as hata:
            raise KancaHatasi(f"girdi JSON degil: {hata}")
        karar(veri, os.environ)
    except Engel as hata:
        sys.stderr.write(f"KAPSAM DIŞI: {hata}\n")
        return EXIT_ENGEL
    except KancaHatasi as hata:
        sys.stderr.write(f"KANCA HATASI: {hata}\n")
        return EXIT_ENGEL
    except Exception as hata:  # kapali basarisizlik
        sys.stderr.write(f"KANCA HATASI: beklenmeyen hata: {type(hata).__name__}: {hata}\n")
        return EXIT_ENGEL
    return EXIT_IZIN


if __name__ == "__main__":
    sys.exit(main())
