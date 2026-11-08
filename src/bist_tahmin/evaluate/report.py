"""Haftalik / baseline raporu (PROJE.md Bolum 11, 16).

Salt-okunur: veritabanina YAZMAZ. Tek kaynak `predictions` tablosudur; yalnizca cozulmus satirlar
(resolved_at <= as_of) metrik hesabina girer. Istatistik cekirdegi metrics.py ve bootstrap.py'dedir.

Kararlar:
- Brier farki, modelin satir bazli Brier kaybi eksi 'baseline_base_rate_v1' kaybidir; ayni
  (ticker, made_at, horizon_days) satirlari eslestirilir, farklari gune gore dairesel blok bootstrap
  ile aralik tahminine baglanir. Negatif = model daha iyi.
- Eslesen satirlar 2'den az farkli gunden geliyorsa aralik hesaplanmaz (lo/hi None); tek gunluk
  bootstrap dejenere olup excludes_zero'yu sahte True yapardi.
- terfi.min_resolved_dates altindaki her sonuc acik uyariyla "gurultu" diye etiketlenir; excludes_zero
  alani yine hesaplanir ama rapor metninde guvenilir sayilmadigi yazilir.
- Hicbir sayi yuvarlanip sisirilmez; belirsizlik (aralik, n, gun sayisi) her metrigin yaninda durur.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..baselines.rules import baseline_model_version
from .bootstrap import paired_diff_ci
from .metrics import (
    accuracy,
    base_rate_brier,
    brier,
    calibration_table,
    daily_ic,
    effective_sample_size,
    ic_summary,
    log_loss,
)

DISCLAIMER = "Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır."
BASE_RATE_VERSION = baseline_model_version("base_rate")


def no_resolved_text(horizon_days: int | None) -> str:
    h = f"{horizon_days} (horizon_days)" if horizon_days else "horizon_days"
    return f"çözülmüş tahmin yok (ilk vade en az {h} işlem günü sonra)"


ASSUMED_RHO = 0.42            # PROJE.md Bolum 0 olcumu; burada OLCULMEZ, yalnizca referans varsayim
CALIBRATION_BINS = 10

_COLUMNS = ("ticker", "made_at", "horizon_days", "prob_up", "score", "model_version", "model_role",
            "resolved_at", "realized_ret", "realized_rel_ret", "tradable_ret", "label_up", "label_rel_up")


@dataclass
class ModelStats:
    model_version: str
    role: str
    n_predictions: int
    n_dates: int
    brier: float | None = None
    # nokta, lo, hi, excludes_zero (+ n_rows, n_dates, ci, resamples, block_days_used); None = hesaplanamadi
    brier_vs_base_rate: dict | None = None
    log_loss: float | None = None
    accuracy: float | None = None
    ic_mean: float | None = None
    ic_tstat: float | None = None            # Newey-West
    ic_n_days: int = 0
    calibration: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Ek alanlar
    label_base_rate: float | None = None     # bu satirlarda gerceklesen yukselis orani
    brier_const_base_rate: float | None = None   # p*(1-p): ayni satirlarin oranini bilen sabit tahmin (bakis ileri; referans)
    ic_score_source: str | None = None       # 'score' | 'prob_up'
    n_pending: int = 0                       # henuz cozulmemis (vadesi gelmemis) tahmin sayisi


@dataclass
class Report:
    as_of: dt.date
    generated_at: dt.datetime
    models: list[ModelStats]
    effective: dict
    costs_label: str
    portfolio: dict | None
    text: str = ""
    horizon_days: int | None = None


# --- yardimcilar -------------------------------------------------------------------------------------------

def _f(x) -> float | None:
    """NaN/sonsuz -> None; aksi halde float."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def costs_label(cfg: dict) -> str:
    costs = cfg.get("costs", {})
    com, spr = costs.get("commission_bps"), costs.get("spread_bps")
    grid = costs.get("sensitivity_grid_bps")
    grid_txt = f"; duyarlılık ızgarası (bps): {grid}" if grid else ""
    if com is None and spr is None:
        return ("maliyetler DOĞRULANMADI: tahmini/maliyetsiz "
                f"(costs.commission_bps ve costs.spread_bps null){grid_txt}")
    if com is None or spr is None:
        return (f"maliyetler DOĞRULANMADI: tahmini (commission_bps={com}, spread_bps={spr}; "
                f"biri null){grid_txt}")
    return f"maliyetler konfigürasyondan: komisyon {com} bps, spread {spr} bps{grid_txt}"


def _load_resolved(conn, as_of: dt.date, roles, versions, extra_version: str | None) -> pd.DataFrame:
    """Cozulmus satirlar. `extra_version` (baz-oran referansi) filtrelerden bagimsiz olarak da yuklenir."""
    sql = (f"SELECT {', '.join(_COLUMNS)} FROM predictions "
           "WHERE resolved_at IS NOT NULL AND label_up IS NOT NULL AND resolved_at <= %s")
    params: list[Any] = [as_of]
    cond, cparams = [], []
    if roles:
        cond.append("model_role = ANY(%s)")
        cparams.append(list(roles))
    if versions:
        cond.append("model_version = ANY(%s)")
        cparams.append(list(versions))
    if cond:
        filt = " AND ".join(cond)
        if extra_version:
            sql += f" AND (({filt}) OR model_version = %s)"
            params += cparams + [extra_version]
        else:
            sql += f" AND {filt}"
            params += cparams
    sql += " ORDER BY model_version, made_at, ticker"
    rows = conn.execute(sql, params).fetchall()
    df = pd.DataFrame(rows, columns=list(_COLUMNS))
    if len(df):
        df["made_at"] = pd.to_datetime(df["made_at"])
        df["resolved_at"] = pd.to_datetime(df["resolved_at"])
        for c in ("prob_up", "score", "realized_ret", "realized_rel_ret", "tradable_ret"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["label_up"] = df["label_up"].astype(int)
    return df


def _load_pending(conn, as_of: dt.date, roles, versions) -> pd.DataFrame:
    """Tahmini yapilmis ama as_of'a kadar cozulmemis satir sayilari (model basina)."""
    sql = ("SELECT model_version, model_role, count(*) FROM predictions "
           "WHERE made_at <= %s AND (resolved_at IS NULL OR resolved_at > %s)")
    params: list[Any] = [as_of, as_of]
    if roles:
        sql += " AND model_role = ANY(%s)"
        params.append(list(roles))
    if versions:
        sql += " AND model_version = ANY(%s)"
        params.append(list(versions))
    sql += " GROUP BY 1, 2"
    return pd.DataFrame(conn.execute(sql, params).fetchall(), columns=["model_version", "model_role", "n"])


def _brier_vs_ref(g: pd.DataFrame, ref: pd.DataFrame | None, model_version: str, boot: dict, seed: int,
                  min_dates: int, warnings: list[str]) -> dict | None:
    if ref is None or ref.empty:
        warnings.append(f"baz-oran baseline'ı ({BASE_RATE_VERSION}) bulunamadı; Brier farkı hesaplanamadı")
        return None
    if model_version == BASE_RATE_VERSION:
        return {"is_reference": True, "point": 0.0, "lo": 0.0, "hi": 0.0, "excludes_zero": False,
                "n_rows": int(len(g)), "n_dates": int(g["made_at"].nunique())}
    key = ["ticker", "made_at", "horizon_days"]
    a = g[key + ["prob_up", "label_up"]].assign(loss_model=lambda d: (d["prob_up"] - d["label_up"]) ** 2)
    b = ref[key + ["prob_up", "label_up"]].assign(loss_ref=lambda d: (d["prob_up"] - d["label_up"]) ** 2)
    m = a[key + ["loss_model"]].merge(b[key + ["loss_ref"]], on=key, how="inner")
    if m.empty:
        warnings.append(f"{BASE_RATE_VERSION} ile eşleşen (ticker, made_at) satırı yok; Brier farkı hesaplanamadı")
        return None
    n_rows, n_dates = int(len(m)), int(m["made_at"].nunique())
    out: dict[str, Any] = {"is_reference": False, "n_rows": n_rows, "n_dates": n_dates,
                           "n_unmatched_model_rows": int(len(a) - n_rows), "ci": float(boot["ci"]),
                           "resamples": int(boot["resamples"]), "block_days": int(boot["block_days"]),
                           "reliable": n_dates >= min_dates}
    if n_dates < 2:
        out.update(point=float((m["loss_model"] - m["loss_ref"]).mean()), lo=None, hi=None,
                   excludes_zero=False, block_days_used=None)
        warnings.append("eşleşen satırlar tek günden geliyor; blok bootstrap aralığı hesaplanamaz")
        return out
    point, lo, hi, det = paired_diff_ci(
        m, "loss_model", "loss_ref", date_col="made_at", block_days=int(boot["block_days"]),
        resamples=int(boot["resamples"]), ci=float(boot["ci"]), seed=seed)
    out.update(point=_f(point), lo=_f(lo), hi=_f(hi), excludes_zero=bool(det["excludes_zero"]),
               block_days_used=int(det["block_days_used"]))
    if out["lo"] is None or out["hi"] is None:
        out["excludes_zero"] = False
        warnings.append("bootstrap aralığı hesaplanamadı (geçerli replikat yok)")
    return out


def _model_stats(model_version: str, g: pd.DataFrame, ref: pd.DataFrame | None, cfg: dict, seed: int,
                 n_pending: int) -> ModelStats:
    terfi = cfg["terfi"]
    min_dates = int(terfi["min_resolved_dates"])
    boot = terfi["bootstrap"]
    clip = tuple(cfg["prob_clip"])
    warnings: list[str] = []
    g = g.sort_values(["made_at", "ticker"])
    role = str(g["model_role"].iloc[-1])
    n, n_dates = int(len(g)), int(g["made_at"].nunique())
    st = ModelStats(model_version=model_version, role=role, n_predictions=n, n_dates=n_dates,
                    n_pending=n_pending, warnings=warnings)
    p, y = g["prob_up"].to_numpy(float), g["label_up"].to_numpy(float)
    st.brier = _f(brier(p, y))
    st.log_loss = _f(log_loss(p, y, clip=clip))
    st.accuracy = _f(accuracy(p, y))
    st.label_base_rate = _f(y.mean())
    st.brier_const_base_rate = _f(base_rate_brier(y))
    st.brier_vs_base_rate = _brier_vs_ref(g, ref, model_version, boot, seed, min_dates, warnings)
    st.calibration = [
        {k: (None if (isinstance(v, float) and not math.isfinite(v)) else (int(v) if k == "n" else float(v)))
         for k, v in rec.items()}
        for rec in calibration_table(p, y, bins=CALIBRATION_BINS).to_dict("records")
    ]
    # IC: yalnizca 'score' tamamen doluysa skor, degilse prob_up (karisik kaynak gun ici siralamayi bozar).
    use_score = bool(g["score"].notna().all())
    st.ic_score_source = "score" if use_score else "prob_up"
    ic_df = g.assign(_rank_score=g["score"] if use_score else g["prob_up"])
    ic = daily_ic(ic_df, "made_at", "_rank_score", "realized_ret")
    summ = ic_summary(ic, horizon_days=int(cfg["horizon_days"]))
    st.ic_mean, st.ic_tstat, st.ic_n_days = _f(summ["mean_ic"]), _f(summ["t_stat_nw"]), int(summ["n_days"])
    if st.ic_n_days == 0:
        warnings.append("IC tanımsız: skor ya da getiri her gün sabit veya günlük hisse sayısı çok az "
                        "(sabit olasılık üreten baseline'larda beklenir)")
    if n_dates < min_dates:
        warnings.append(f"n_dates={n_dates} < terfi.min_resolved_dates={min_dates}: "
                        "sonuç istatistiksel olarak yetersiz/gürültü")
    return st


def build_report(conn, cfg: dict, *, as_of: dt.date | None = None, seed: int = 0,
                 portfolio_fn: Callable[[pd.DataFrame, dict], dict | None] | None = None,
                 roles: list[str] | tuple[str, ...] | None = None,
                 model_versions: list[str] | tuple[str, ...] | None = None) -> Report:
    """Rapor uretir; veritabanina yazmaz.

    roles / model_versions: raporlanacak modelleri sinirlar (None = hepsi). Baz-oran referansi, filtre
    disinda olsa bile Brier farki icin ayrica okunur ama listede gosterilmez.
    portfolio_fn(df_for_model, cfg) -> dict | None: modelin cozulmus satirlari (predictions sutunlari,
    made_at/resolved_at Timestamp) ile cagrilir; sonuc `Report.portfolio[model_version]` olur.
    """
    tz = ZoneInfo(cfg.get("timezone", "Europe/Istanbul"))
    generated_at = dt.datetime.now(tz)
    as_of = as_of or generated_at.date()
    min_dates = int(cfg["terfi"]["min_resolved_dates"])

    df = _load_resolved(conn, as_of, roles, model_versions, BASE_RATE_VERSION)
    ref = df[df["model_version"] == BASE_RATE_VERSION] if len(df) else None
    listed = df
    if len(df):
        keep = pd.Series(True, index=df.index)
        if roles:
            keep &= df["model_role"].isin(list(roles))
        if model_versions:
            keep &= df["model_version"].isin(list(model_versions))
        listed = df[keep]
    pending = _load_pending(conn, as_of, roles, model_versions)
    pending_by = dict(zip(pending["model_version"], pending["n"].astype(int)))

    models: list[ModelStats] = []
    portfolio: dict | None = {} if portfolio_fn is not None else None
    for mv, g in listed.groupby("model_version", sort=True):
        st = _model_stats(str(mv), g, ref, cfg, seed, int(pending_by.pop(mv, 0)))
        models.append(st)
        if portfolio_fn is not None:
            try:
                portfolio[str(mv)] = portfolio_fn(g.reset_index(drop=True), cfg)
            except Exception as exc:  # rapor portfoy hatasi yuzunden dusmesin; hata acikca gorunur
                portfolio[str(mv)] = None
                st.warnings.append(f"portföy hesabı hata verdi: {type(exc).__name__}: {exc}")
            if portfolio[str(mv)] is None and not any("portföy" in w for w in st.warnings):
                st.warnings.append("portföy fonksiyonu sonuç döndürmedi (None)")
    # Hic cozulmemis satiri olan modeller de listelenir (n=0).
    for mv, n_p in sorted(pending_by.items()):
        role = str(pending.loc[pending["model_version"] == mv, "model_role"].iloc[0])
        models.append(ModelStats(
            model_version=str(mv), role=role, n_predictions=0, n_dates=0, n_pending=int(n_p),
            warnings=["çözülmüş tahmin yok"]))
        if portfolio_fn is not None:
            portfolio[str(mv)] = None
    models.sort(key=lambda s: s.model_version)

    n_resolved = int(len(listed))
    all_dates = int(listed["made_at"].nunique()) if n_resolved else 0
    per_day = listed.groupby("made_at")["ticker"].nunique() if n_resolved else pd.Series(dtype=float)
    mean_universe = _f(per_day.mean()) if len(per_day) else None
    eff: dict[str, Any] = {
        "n_resolved": n_resolved, "n_dates": all_dates, "mean_universe": mean_universe,
        "min_resolved_dates": min_dates, "sufficient": all_dates >= min_dates,
        "assumed_rho": ASSUMED_RHO, "n_eff_per_day_assumed": None, "warning": None,
    }
    if mean_universe:
        eff["n_eff_per_day_assumed"] = _f(effective_sample_size(max(int(round(mean_universe)), 1), ASSUMED_RHO))
    if n_resolved == 0:
        eff["warning"] = no_resolved_text(int(cfg["horizon_days"]))
    elif all_dates < min_dates:
        eff["warning"] = (f"n_dates={all_dates} < terfi.min_resolved_dates={min_dates}: "
                          "sonuç istatistiksel olarak yetersiz/gürültü")

    report = Report(as_of=as_of, generated_at=generated_at, models=models, effective=eff,
                    costs_label=costs_label(cfg), portfolio=portfolio,
                    horizon_days=int(cfg["horizon_days"]))
    report.text = render_markdown(report)
    return report


# --- markdown ----------------------------------------------------------------------------------------------

def _num(x, nd: int = 4, signed: bool = False) -> str:
    if x is None:
        return "yok"
    if abs(x) >= 1e6:      # dejenere durum (ornegin mukemmel sentetik IC): okunabilir bilimsel gosterim
        return f"{x:.3g}"
    return f"{x:+.{nd}f}" if signed else f"{x:.{nd}f}"


def _diff_cell(d: dict | None) -> str:
    if not d:
        return "hesaplanamadı"
    if d.get("is_reference"):
        return "referans model (fark 0)"
    pt = _num(d.get("point"), 4, True)
    if d.get("lo") is None or d.get("hi") is None:
        return f"{pt} [aralık yok] excludes_zero=hayır"
    ez = "evet" if d["excludes_zero"] else "hayır"
    flag = "" if d.get("reliable", True) else " (gün sayısı yetersiz: güvenilmez)"
    return f"{pt} [{_num(d['lo'], 4, True)}; {_num(d['hi'], 4, True)}] excludes_zero={ez}{flag}"


def _render_value(v: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if isinstance(v, dict):
        out: list[str] = []
        for k, x in v.items():
            if isinstance(x, (dict, list)):
                out.append(f"{pad}- {k}:")
                out += _render_value(x, indent + 1)
            else:
                out.append(f"{pad}- {k}: {_scalar(x)}")
        return out
    if isinstance(v, list):
        if v and all(isinstance(r, dict) for r in v) and len({tuple(r) for r in v}) == 1:
            cols = list(v[0])
            lines = [f"{pad}| " + " | ".join(map(str, cols)) + " |", f"{pad}|" + "---|" * len(cols)]
            lines += [f"{pad}| " + " | ".join(_scalar(r[c]) for c in cols) + " |" for r in v]
            return lines
        out = []
        for x in v:
            if isinstance(x, (dict, list)):
                out += [f"{pad}-"] + _render_value(x, indent + 1)
            else:
                out.append(f"{pad}- {_scalar(x)}")
        return out
    return [f"{pad}- {_scalar(v)}"]


def _scalar(x: Any) -> str:
    if isinstance(x, float):
        return "yok" if not math.isfinite(x) else f"{x:.6g}"
    return str(x)


def render_markdown(report: Report) -> str:
    eff, h = report.effective, report.horizon_days
    L: list[str] = []
    L.append("# BIST 30 tahmin ölçüm raporu")
    L.append("")
    L.append(f"- Tarih (as_of): {report.as_of.isoformat()} (yalnızca bu tarihe kadar çözülmüş tahminler)")
    L.append(f"- Oluşturulma: {report.generated_at.strftime('%Y-%m-%d %H:%M %Z')}")
    if h:
        L.append(f"- Ufuk: {h} işlem günü")
    L.append(f"- Maliyet etiketi: {report.costs_label}")
    L.append("")
    L.append("## Örneklem ve etkin örneklem")
    L.append("")
    L.append(f"- Çözülmüş tahmin: {eff['n_resolved']}; farklı gün (made_at): {eff['n_dates']} "
             f"(eşik terfi.min_resolved_dates={eff['min_resolved_dates']})")
    if eff.get("mean_universe") is not None:
        n_eff = eff.get("n_eff_per_day_assumed")
        L.append(f"- Günlük ortalama evren: {eff['mean_universe']:.1f} hisse. Aynı günün hisseleri bağımsız "
                 f"değildir: varsayılan ρ={eff['assumed_rho']} (PROJE.md Bölüm 0 ölçümü, bu raporda ölçülmedi) "
                 f"ile günde yaklaşık {_num(n_eff, 1)} bağımsız gözlem.")
    if eff.get("warning"):
        L.append(f"- **UYARI:** {eff['warning']}")
    L.append("")

    if not report.models or eff["n_resolved"] == 0:
        L.append(f"**{no_resolved_text(h)}**")
        L.append("")

    if report.models:
        L.append("## Özet (her metrik yanında tahmin sayısı n ve farklı gün sayısı)")
        L.append("")
        L.append(f"Brier farkı = model Brier kaybı − {BASE_RATE_VERSION} kaybı (aynı ticker ve made_at satırları, "
                 "eşleştirilmiş blok bootstrap aralığı); negatif = model daha iyi.")
        L.append("")
        L.append("| Model | Rol | n tahmin | n gün | Brier | Brier farkı (baz orana göre) | Log loss | Accuracy | "
                 "IC ort. | IC NW t | IC gün |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for s in report.models:
            L.append(f"| {s.model_version} | {s.role} | {s.n_predictions} | {s.n_dates} | {_num(s.brier)} | "
                     f"{_diff_cell(s.brier_vs_base_rate)} | {_num(s.log_loss)} | {_num(s.accuracy)} | "
                     f"{_num(s.ic_mean)} | {_num(s.ic_tstat, 2)} | {s.ic_n_days} |")
        L.append("")
        L.append("Accuracy yalnızca referanstır (baz oran ~%55 iken 0.5 eşiği neredeyse hep 'yukarı' der); "
                 "tek başına karar vermez. IC: günlük Spearman, Newey-West t "
                 f"(gecikme ufuk−1). Kesin sonuç için n gün sütununa bakın.")
        L.append("")

    for s in report.models:
        L.append(f"## {s.model_version} ({s.role})")
        L.append("")
        L.append(f"- Örneklem: {s.n_predictions} tahmin, {s.n_dates} farklı gün; bekleyen (çözülmemiş): {s.n_pending}")
        if s.n_predictions:
            L.append(f"- Brier: {_num(s.brier)}; gerçekleşen yükseliş oranı {_num(s.label_base_rate)}; "
                     f"baz-oran Brier referansı p(1-p) = {_num(s.brier_const_base_rate)} "
                     "(aynı satırların oranını bilen sabit tahmin; geriye bakan referans)")
            d = s.brier_vs_base_rate
            if d and not d.get("is_reference"):
                L.append(f"- Brier farkı (baz oran baseline'ına göre): {_diff_cell(d)}; eşleşen {d['n_rows']} "
                         f"satır, {d['n_dates']} gün"
                         + (f"; {int(d['ci'] * 100)}% CI, {d['resamples']} yeniden örnekleme, blok "
                            f"{d.get('block_days_used') or d['block_days']} gün" if d.get("ci") else ""))
            elif d is None:
                L.append("- Brier farkı: hesaplanamadı (uyarılara bakın)")
            L.append(f"- Log loss: {_num(s.log_loss)}; accuracy: {_num(s.accuracy)} (n={s.n_predictions})")
            L.append(f"- IC (kaynak: {s.ic_score_source}): ortalama {_num(s.ic_mean)}, Newey-West t "
                     f"{_num(s.ic_tstat, 2)}, {s.ic_n_days} geçerli gün")
            nonempty = [c for c in s.calibration if c["n"] > 0]
            L.append("")
            L.append("Kalibrasyon (olasılık kovası, n, ortalama tahmin, gerçekleşen oran):")
            L.append("")
            L.append("| Kova | n | Ort. tahmin | Gerçekleşen oran |")
            L.append("|---|---|---|---|")
            for c in nonempty:
                L.append(f"| [{c['bin_lo']:.1f}, {c['bin_hi']:.1f}{']' if c['bin_hi'] >= 1.0 else ')'} | "
                         f"{c['n']} | {_num(c['mean_pred'])} | {_num(c['obs_rate'])} |")
        if s.warnings:
            L.append("")
            for w in s.warnings:
                L.append(f"- **UYARI:** {w}")
        L.append("")

    L.append("## Portföy (kohort simülasyonu, maliyet sonrası)")
    L.append("")
    if report.portfolio is None:
        L.append("henüz hesaplanmadı")
    elif not report.portfolio:
        L.append("hesaplanacak model yok")
    else:
        for mv, res in report.portfolio.items():
            L.append(f"### {mv}")
            L.append("")
            if res is None:
                L.append("hesaplanamadı")
            else:
                L += _render_value(res)
            L.append("")
    L.append("")
    L.append(f"Maliyet durumu: {report.costs_label}")
    L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L) + "\n"
