"""Terfi kurali (PROJE.md Bolum 11 'Karar kurali'): bir challenger ancak 5 kosulun HEPSINI saglarsa onaylanir.

Kanit CANLI defterden (predictions: cozulmus satirlar) gelir; challenger ve referans, ayni (ticker, made_at)
satirlarinda eslestirilir. Referans = verilen champion version'u; champion None ise
'baseline_base_rate_v1' (asil cita: gecmis yukselis orani).

Kosullar (her birinin detail'i sayilari icerir; veri yetersizse ok=False ve nedeni yazilir, cokme yok):
  min_gun         eslesen FARKLI made_at gunu >= terfi.min_resolved_dates
  brier           eslestirilmis blok bootstrap: Brier kaybi (challenger - referans) <= -min_improvement_brier VE
                  Bonferroni'li aralik (ci = 1 - alpha/n_trials) sifiri dislar ve ust uc < 0
  sinama_kumesi   holdout_result (lab.evaluate ciktisi) main.model_better ve excludes_zero; kanit yoksa ok=False
                  ('sinama kumesi kaniti yok'); terfi.require_holdout_pass False ise atlanir
  canli_golge     challenger'in kendi cozulmus FARKLI made_at gunu >= terfi.require_live_shadow_days
  net_getiri      maliyet sonrasi benchmark'a gore net fazla getiri (kohort portfoyu, score=prob_up,
                  tradable_ret): challenger > referans + terfi.min_improvement_net_return

Maliyet (tek yon, bps): cost_bps verilmisse o; yoksa costs.commission_bps ve costs.spread_bps ikisi de doluysa
toplami; yoksa 10 bps (duyarlilik izgarasindaki ortanca deger, DOGRULANMADI/tahmini).

Brier her zaman label_up uzerinden hesaplanir (prob_up = P(yukselis) tanimi). label_rel_up ile egitilmis bir
modelin olasiligi bu olcutte cezalandirilir; detail'e not dusulur.

Bu modul karar verir, uygulamaz: onay sonrasi `registry.promote` cagirmak cagirana aittir.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..evaluate.bootstrap import bonferroni_alpha, paired_diff_ci
from ..evaluate.portfolio import simulate_cohort_portfolio, validate_cost_bps

BASELINE_REFERENCE = "baseline_base_rate_v1"
DEFAULT_COST_BPS = 10.0
TOP_FRAC = 0.2           # PROJE.md Bolum 11: kohort portfoyu en ust %20
TAIL_DRAWS_MIN = 10      # her uc kuyrukta en az bu kadar bootstrap replikati
MAX_RESAMPLES = 200_000
DISCLAIMER = "Bu çıktı yatırım tavsiyesi değildir; yalnızca model ölçüm amaçlıdır."

CHECK_DAYS = "min_gun"
CHECK_BRIER = "brier"
CHECK_HOLDOUT = "sinama_kumesi"
CHECK_SHADOW = "canli_golge"
CHECK_NET = "net_getiri"
CHECK_NAMES = (CHECK_DAYS, CHECK_BRIER, CHECK_HOLDOUT, CHECK_SHADOW, CHECK_NET)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class PromotionDecision:
    approved: bool
    challenger: str
    reference: str
    checks: list[Check] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------- veri

def _load_resolved(conn, version: str, horizon_days: int) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT ticker, made_at, prob_up, label_up, tradable_ret FROM predictions"
        " WHERE model_version = %s AND horizon_days = %s AND resolved_at IS NOT NULL"
        " ORDER BY made_at, ticker",
        (version, int(horizon_days)),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["ticker", "made_at", "prob_up", "label_up", "tradable_ret"])
    df["made_at"] = pd.to_datetime(df["made_at"])
    for c in ("prob_up", "label_up", "tradable_ret"):
        df[c] = pd.to_numeric(df[c]).astype(float)
    return df


def _resolve_cost(cfg: dict, cost_bps: float | None) -> tuple[float, str]:
    if cost_bps is not None:
        return validate_cost_bps(cost_bps), "belirtilen"
    costs = cfg.get("costs") or {}
    com, spr = costs.get("commission_bps"), costs.get("spread_bps")
    if com is not None and spr is not None:
        return validate_cost_bps(float(com) + float(spr)), "yapilandirilmis (komisyon+spread)"
    return DEFAULT_COST_BPS, "varsayilan tahmini, DOGRULANMADI"


def _resamples(cfg_resamples: int, alpha_adj: float) -> int:
    needed = math.ceil(2 * TAIL_DRAWS_MIN / alpha_adj)
    return min(max(int(cfg_resamples), needed), MAX_RESAMPLES)


def _f(x, nd: int = 5) -> str:
    return "yok" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{nd}f}"


# ---------------------------------------------------------------- kosullar

def _check_days(n_days: int, min_dates: int, n_rows: int) -> Check:
    ok = n_days >= min_dates
    return Check(CHECK_DAYS, ok, f"eslesen farkli made_at gunu {n_days} (esik >= {min_dates}), eslesen satir {n_rows}")


def _check_brier(matched: pd.DataFrame, terfi: dict, alpha: float, n_trials: int, seed: int,
                 note: str) -> Check:
    min_impr = float(terfi.get("min_improvement_brier", 0.0) or 0.0)
    alpha_adj = bonferroni_alpha(alpha, n_trials)
    ci = 1.0 - alpha_adj
    boot = terfi.get("bootstrap") or {}
    block_days = int(boot.get("block_days", 10))
    n_dates = int(matched["made_at"].nunique()) if len(matched) else 0
    head = f"alpha={alpha}, n_trials={n_trials} -> ci={ci:.4%}"
    if n_dates < 2:
        return Check(CHECK_BRIER, False, f"hesaplanamadi: eslesen gun {n_dates} < 2 (blok bootstrap icin yetersiz); {head}")
    resamples = _resamples(int(boot.get("resamples", 2000)), alpha_adj)
    df = pd.DataFrame({
        "made_at": matched["made_at"].to_numpy(),
        "loss_ch": (matched["prob_up_ch"] - matched["label_up"]) ** 2,
        "loss_ref": (matched["prob_up_ref"] - matched["label_up"]) ** 2,
    })
    point, lo, hi, det = paired_diff_ci(df, "loss_ch", "loss_ref", date_col="made_at", block_days=block_days,
                                        resamples=resamples, ci=ci, seed=seed)
    ok = bool(point <= -min_impr and det["excludes_zero"] and hi < 0.0)
    detail = (f"Brier farki (challenger - referans) {_f(point)} (esik <= {-min_impr:.5f}); "
              f"{ci:.2%} blok bootstrap araligi [{_f(lo)}, {_f(hi)}] "
              f"({'sifiri dislar, ust uc < 0' if (det['excludes_zero'] and hi < 0) else 'sifiri dislamiyor ya da iyilesme yonu degil'}); "
              f"{len(df)} satir / {n_dates} gun, blok={det['block_days_used']}, resamples={resamples}; {head}")
    if note:
        detail += f"; {note}"
    return Check(CHECK_BRIER, ok, detail)


def _check_holdout(holdout_result: dict | None, terfi: dict) -> Check:
    if not bool(terfi.get("require_holdout_pass", True)):
        return Check(CHECK_HOLDOUT, True, "atlandi (terfi.require_holdout_pass=false)")
    if holdout_result is None:
        return Check(CHECK_HOLDOUT, False, "sinama kumesi kaniti yok")
    main = holdout_result.get("main") if isinstance(holdout_result, dict) else None
    if not isinstance(main, dict):
        return Check(CHECK_HOLDOUT, False, "sinama kumesi kaniti yok (holdout_result.main eksik/bos)")
    better, excl = bool(main.get("model_better")), bool(main.get("excludes_zero"))
    info = (f"fark {_f(main.get('diff'))}, aralik [{_f(main.get('lo'))}, {_f(main.get('hi'))}], "
            f"{main.get('n_dates', holdout_result.get('n_dates'))} gun, model_better={better}, excludes_zero={excl}")
    if holdout_result.get("sufficient") is False:
        return Check(CHECK_HOLDOUT, False, f"sinama kumesi ornegi yetersiz (lab: sufficient=false); {info}")
    return Check(CHECK_HOLDOUT, bool(better and excl), info)


def _check_shadow(own_days: int, required: int) -> Check:
    return Check(CHECK_SHADOW, own_days >= required,
                 f"challenger canli cozulmus farkli made_at gunu {own_days} (esik >= {required})")


def _check_net(matched: pd.DataFrame, terfi: dict, cost: float, cost_label: str) -> Check:
    min_impr = float(terfi.get("min_improvement_net_return", 0.0) or 0.0)
    head = f"maliyet {cost:g} bps tek yon ({cost_label})"
    if len(matched) == 0:
        return Check(CHECK_NET, False, f"hesaplanamadi: eslesen satir yok; {head}")
    res = {}
    for key, col in (("ch", "prob_up_ch"), ("ref", "prob_up_ref")):
        port = matched[["made_at", "ticker", col, "tradable_ret"]].rename(columns={col: "score"})
        res[key] = simulate_cohort_portfolio(port, top_frac=TOP_FRAC, cost_bps=cost).summary
    n_ch, n_ref = res["ch"]["n_cohorts"], res["ref"]["n_cohorts"]
    if n_ch == 0 or n_ref == 0:
        return Check(CHECK_NET, False, f"hesaplanamadi: kohort yok (challenger {n_ch}, referans {n_ref}); {head}")
    ex_ch, ex_ref = res["ch"]["mean_excess_net"], res["ref"]["mean_excess_net"]
    ok = bool(ex_ch > ex_ref + min_impr)
    return Check(CHECK_NET, ok,
                 f"kohort basina net fazla getiri: challenger {_f(ex_ch)} ({n_ch} kohort), "
                 f"referans {_f(ex_ref)} ({n_ref} kohort); gerekli: challenger > referans + {min_impr:g}; {head}")


# ---------------------------------------------------------------- ana giris

def promote_check(
    conn,
    cfg: dict,
    challenger: str,
    champion: str | None = None,
    *,
    holdout_result: dict | None = None,
    n_trials: int = 1,
    alpha: float = 0.05,
    seed: int = 0,
    cost_bps: float | None = None,
) -> PromotionDecision:
    """5 kosulun hepsini degerlendirir; approved = hepsi ok. Veri yetersizse cokmez, ilgili kosul ok=False olur."""
    if not isinstance(challenger, str) or not challenger.strip():
        raise ValueError("challenger version bos olamaz")
    reference = champion if champion is not None else BASELINE_REFERENCE
    if reference == challenger:
        raise ValueError("challenger kendisiyle karsilastirilamaz")
    bonferroni_alpha(alpha, n_trials)  # gecersiz alpha/n_trials erken ValueError

    terfi = cfg.get("terfi") or {}
    horizon = int(cfg["horizon_days"])
    min_dates = int(terfi.get("min_resolved_dates", 60))
    shadow_req = int(terfi.get("require_live_shadow_days", 60))
    cost, cost_label = _resolve_cost(cfg, cost_bps)

    ch = _load_resolved(conn, challenger, horizon)
    ref = _load_resolved(conn, reference, horizon)
    matched = ch.merge(ref[["ticker", "made_at", "prob_up", "label_up"]], on=["ticker", "made_at"],
                       how="inner", suffixes=("_ch", "_ref"))
    mism = int((matched["label_up_ch"] != matched["label_up_ref"]).sum())
    if mism:  # ayni satirin etiketi iki modelde farkli olamaz; olursa veri bozuk: o satirlar disarida
        matched = matched.loc[matched["label_up_ch"] == matched["label_up_ref"]]
    matched = matched.rename(columns={"label_up_ch": "label_up"}).drop(columns=["label_up_ref"])
    matched = matched.sort_values(["made_at", "ticker"]).reset_index(drop=True)
    n_days = int(matched["made_at"].nunique()) if len(matched) else 0
    own_days = int(ch["made_at"].nunique()) if len(ch) else 0

    note = ""
    if mism:
        note = f"UYARI: {mism} satirda etiket iki modelde farkli, dislandi"
    row = conn.execute("SELECT params_json->>'label' FROM models WHERE version = %s", (challenger,)).fetchone()
    if row and row[0] == "label_rel_up":
        note = (note + "; " if note else "") + "NOT: challenger label_rel_up ile egitilmis, Brier label_up uzerinden olculdu"

    checks = [
        _check_days(n_days, min_dates, len(matched)),
        _check_brier(matched, terfi, alpha, n_trials, seed, note),
        _check_holdout(holdout_result, terfi),
        _check_shadow(own_days, shadow_req),
        _check_net(matched, terfi, cost, cost_label),
    ]
    approved = all(c.ok for c in checks)
    if approved:
        summary = f"ONAY: {challenger}, {reference} referansina karsi 5 kosulun hepsini sagladi."
    else:
        failed = ", ".join(c.name for c in checks if not c.ok)
        summary = f"RED: {challenger} terfi edemez; basarisiz kosul(lar): {failed}."
    return PromotionDecision(approved=approved, challenger=challenger, reference=reference,
                             checks=checks, summary=summary)


def format_decision(decision: PromotionDecision) -> str:
    """Turkce metin ozeti (kosul kosul sonuc ve sayilar)."""
    L = [f"## Terfi kontrolu: {decision.challenger} (referans: {decision.reference})", "", decision.summary, ""]
    for c in decision.checks:
        L.append(f"- [{'GECTI' if c.ok else 'KALDI'}] {c.name}: {c.detail}")
    n_ok = sum(1 for c in decision.checks if c.ok)
    L.append("")
    L.append(f"Sonuc: {n_ok}/{len(decision.checks)} kosul saglandi; karar: {'ONAY' if decision.approved else 'RED'} "
             "(terfi icin tum kosullar gerekir).")
    L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)
