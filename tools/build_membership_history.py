"""Ceyreklik BIST 30 donem dosyalarindan (reference/bist30/*.json) araliklara dayali uyelik CSV'sini uretir.

Cikti: reference/bist30/membership_history.csv  (ticker,valid_from,valid_to,confidence)
- Araliklar yari aciktir [valid_from, valid_to); valid_to bos = hala uye.
- Ardisik donemlerde sureklilik gosteren hisse tek aralikta birlestirilir.
- Kod degisiklikleri GUNCEL koda cevrilir (borsapy gecmis fiyatlari yeni kodla verir): KOZAL->TRALT, KOZAA->TRMET.
- Guven duzeyi: aralik boyunca ortak donemlerin EN DUSUGU.
Kaynaklarin kendisi (URL'ler, notlar) JSON dosyalarindadir; tam listeler resmi duyurulardaki eklenen/cikarilan
paylarin cipalar arasi zincirlenmesiyle turetilmistir (bkz. PROJE.md Bolum 15, Faz 3.5 on kosulu).
Kullanim: python tools/build_membership_history.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "reference" / "bist30"
FILES = ["bist30_2019_2021.json", "bist30_2022_2024h1.json", "bist30_2024h2_2026.json"]
CODE_MAP = {"KOZAL": "TRALT", "KOZAA": "TRMET"}
RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}


def _level(text) -> str:
    """Guven duzeyi: ilk sozcuk (bir dosya 'high for added/removed...; medium for the full list (derived)' yazmis:
    sinirlar (eklenen/cikarilan) resmi duyurudan ve uc noktaya zincirle dogrulandigi icin ilk sozcuk gecerlidir)."""
    word = (str(text or "unknown").strip().split() or ["unknown"])[0].lower().strip(",;:")
    return word if word in RANK else "unknown"


def load_periods() -> list[dict]:
    out = []
    for f in FILES:
        out += json.loads((ROOT / f).read_text(encoding="utf-8"))["periods"]
    out.sort(key=lambda p: p["start"])
    for p in out:
        p["tickers"] = sorted({CODE_MAP.get(t, t) for t in p["tickers"]})
    return out


def build_intervals(periods: list[dict]) -> list[tuple[str, str, str, str]]:
    open_: dict[str, tuple[str, int]] = {}  # ticker -> (valid_from, min_rank)
    rows: list[tuple[str, str, str, str]] = []
    inv = {v: k for k, v in RANK.items()}
    for p in periods:
        members = set(p["tickers"])
        rank = RANK.get(_level(p.get("confidence")), 0)
        for t in sorted(set(open_) - members):           # bu donemde yok => aralik bu donemin basinda kapanir
            vf, r = open_.pop(t)
            rows.append((t, vf, p["start"], inv[r]))
        for t in sorted(members):
            if t in open_:
                vf, r = open_[t]
                open_[t] = (vf, min(r, rank))
            else:
                open_[t] = (p["start"], rank)
    for t, (vf, r) in open_.items():
        rows.append((t, vf, "", inv[r]))
    rows.sort(key=lambda x: (x[0], x[1]))
    return rows


def main() -> None:
    rows = build_intervals(load_periods())
    out = ROOT / "membership_history.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["ticker", "valid_from", "valid_to", "confidence"])
        w.writerows(rows)
    print(f"{len(rows)} aralik yazildi: {out}")


if __name__ == "__main__":
    main()
