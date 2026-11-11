"""Terfi kurali (PROJE.md Bolum 11 'Karar kurali'): bir challenger ancak 5 kosulun HEPSINI saglarsa onaylanir.

Kanit CANLI defterden (predictions: cozulmus satirlar) gelir.

Karsilastirma referanslari:
- Baz oran (BASELINE_REFERENCE = 'baseline_base_rate_v1'): PROJE Bolum 0 ana olcut. HER ZAMAN uygulanir.
  Brier (kosul 2) ve net getiri (kosul 5) icin challenger baz orana karsi da iyi olmak zorundadir.
- Champion (cagirana verilen, yani registry'deki mevcut champion): PROJE Bolum 11 kosul 2 ve kosul 5 icin
  KARAR VERICI karsilastirma. Champion varsa challenger champion'dan da iyi olmalidir. Champion yoksa yalnizca baz
  orana karsi karar verilir.

Kosullar (her birinin detail'i sayilari icerir; veri yetersizse ok=False ve nedeni yazilir, cokme yok):
  min_gun         eslesen FARKLI made_at gunu >= terfi.min_resolved_dates
  brier           Brier kaybi farki (challenger - referans) eslestirilmis blok bootstrap ile olculur; her
                  karsilastirma icin (champion varsa champion, her zaman baz oran) ayri: fark <= -min_improvement_brier
                  VE Bonferroni'li aralik (ci = 1 - alpha/n_trials) sifiri dislar ve ust uc < 0. Iki karsilastirma da
                  gecmeli.
  sinama_kumesi  holdout_result (runner.run_experiment ciktisi): HEPSI gerekir, eksik/uyumsuz alan = KALDI:
                  (a) experiment.use_holdout == True; (b) experiment.holdout_start == cfg holdout.holdout_start;
                  (c) challenger extra.train_includes_holdout == False; (d) extra.train_max_date (kullanilan en son
                  tarih, end_date dahil) < holdout_start; (e) run_info.spec == challenger extra.spec; (f) main.model_better
                  ve excludes_zero. Kanit yoksa ok=False ('sinama kumesi kaniti yok'); terfi.require_holdout_pass False
                  ise atlanir
  canli_golge     challenger'in kendi cozulmus FARKLI made_at gunu >= terfi.require_live_shadow_days VE ayni
                  eslesmis satirlarda baz orana karsi ortalama Brier farki < 0 (iyilesme)
  net_getiri      maliyet sonrasi kohort portfoyunun benchmark'a gore fazla getirisi (score=prob_up, en ust %20,
                  tradable_ret). Her karsilastirma icin: challenger > referans + terfi.min_improvement_net_return.
                  Champion ve baz oran icin ayri ayri, ayni kosul.

Maliyet (tek yon, bps): cost_bps verilmisse o; yoksa costs.commission_bps ve costs.spread_bps ikisi de doluysa
toplami; yoksa DEFAULT_COST_BPS = 10 bps (PROJE Bolum 11 raporundaki tek yon senaryo; DOGRULANMADI). Devre kesici
(monitor.breaker) ayni kurali kullanir.

Brier her zaman label_up uzerinden hesaplanir (prob_up = P(yukselis) tanimi). label_rel_up ile egitilmis bir
modelin olasiligi bu olcutte cezalandirilir; detail'e not dusulur.

Bu modul karar verir, uygulamaz: onay sonrasi `registry.promote` cagirmak cagirana aittir. CLI bu iki adimi
tek islemde, ayni yasam dongusu kilidi altinda yapar (cli._cmd_models_promote).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ..evaluate.bootstrap import bonferroni_alpha, paired_diff_ci
from ..evaluate.portfolio import simulate_cohort_portfolio, validate_cost_bps

BASELINE_REFERENCE = "baseline_base_rate_v1"
DEFAULT_COST_BPS = 10.0  # PROJE Bolum 11 'net' raporundaki tek yon senaryo; DOGRULANMADI
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

SUB_CHAMPION = "champion"
SUB_BASE = "baz_oran"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    sub: dict[str, bool] = field(default_factory=dict)  # karsilastirma bazinda sonuc (brier/net: champion, baz_oran)


@dataclass
class PromotionDecision:
    approved: bool
    challenger: str
    reference: str
    checks: list[Check] = field(default_factory=list)
    summary: str = ""
    info: list[str] = field(default_factory=list)  # champion verildiginde aciklama notu (kararin parcasi)


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


def _load_params(conn, version: str) -> dict | None:
    """models kaydinin params_json'u; kayit yoksa None."""
    row = conn.execute("SELECT params_json FROM models WHERE version = %s", (version,)).fetchone()
    if row is None:
        return None
    return dict(row[0] or {})


def resolve_cost(cfg: dict, cost_bps: float | None = None) -> tuple[float, str]:
    """Tek yon maliyet (bps) ve etiketi. Breaker da bu kurali kullanir (tutarlilik)."""
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


def _pair(ch: pd.DataFrame, other: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Ayni (ticker, made_at) satirlarini eslestirir. Kolonlar: made_at, ticker, prob_up_ch, prob_up_ref, label_up,
    tradable_ret. Ayni satirin etiketi iki modelde farkliysa veri bozuktur: o satirlar disarida, sayisi dondurulur."""
    m = ch.merge(other[["ticker", "made_at", "prob_up", "label_up"]], on=["ticker", "made_at"],
                 how="inner", suffixes=("_ch", "_ref"))
    mism = int((m["label_up_ch"] != m["label_up_ref"]).sum())
    if mism:
        m = m.loc[m["label_up_ch"] == m["label_up_ref"]]
    m = m.rename(columns={"label_up_ch": "label_up"}).drop(columns=["label_up_ref"])
    m = m.sort_values(["made_at", "ticker"]).reset_index(drop=True)
    return m, mism


# ---------------------------------------------------------------- kosullar

def _check_days(n_days: int, min_dates: int, n_rows: int) -> Check:
    ok = n_days >= min_dates
    return Check(CHECK_DAYS, ok, f"eslesen farkli made_at gunu {n_days} (esik >= {min_dates}), eslesen satir {n_rows}")


def _brier_vs(pairs: pd.DataFrame, ref_word: str, label: str, terfi: dict, alpha_adj: float, seed: int
              ) -> tuple[bool, str]:
    """Tek karsilastirma: challenger ile ref_word arasinda eslestirilmis blok bootstrap Brier farki."""
    min_impr = float(terfi.get("min_improvement_brier", 0.0) or 0.0)
    boot = terfi.get("bootstrap") or {}
    block_days = int(boot.get("block_days", 10))
    n_dates = int(pairs["made_at"].nunique()) if len(pairs) else 0
    if n_dates < 2:
        return False, f"{label}: hesaplanamadi: eslesen gun {n_dates} < 2 (blok bootstrap icin yetersiz)"
    ci = 1.0 - alpha_adj
    resamples = _resamples(int(boot.get("resamples", 2000)), alpha_adj)
    df = pd.DataFrame({
        "made_at": pairs["made_at"].to_numpy(),
        "loss_ch": (pairs["prob_up_ch"] - pairs["label_up"]) ** 2,
        "loss_ref": (pairs["prob_up_ref"] - pairs["label_up"]) ** 2,
    })
    point, lo, hi, det = paired_diff_ci(df, "loss_ch", "loss_ref", date_col="made_at", block_days=block_days,
                                        resamples=resamples, ci=ci, seed=seed)
    ok = bool(point <= -min_impr and det["excludes_zero"] and hi < 0.0)
    text = (f"{label}: Brier farki (challenger - {ref_word}) {_f(point)} (esik <= {-min_impr:.5f}); "
            f"{ci:.2%} blok bootstrap araligi [{_f(lo)}, {_f(hi)}] "
            f"({'sifiri dislar, ust uc < 0' if (det['excludes_zero'] and hi < 0) else 'sifiri dislamiyor ya da iyilesme yonu degil'}); "
            f"{len(df)} satir / {n_dates} gun, blok={det['block_days_used']}, resamples={resamples}")
    return ok, text


def _check_brier(base: pd.DataFrame, champ: pd.DataFrame | None, champion: str | None, terfi: dict,
                 alpha: float, n_trials: int, seed: int, note: str) -> Check:
    alpha_adj = bonferroni_alpha(alpha, n_trials)
    sub: dict[str, bool] = {}
    parts: list[str] = []
    if champion is not None:
        ok_c, t_c = _brier_vs(champ, f"champion '{champion}'", f"champion '{champion}' ile (ANA, karar kosulu)",
                              terfi, alpha_adj, seed)
        sub[SUB_CHAMPION] = ok_c
        parts.append(t_c)
    ok_b, t_b = _brier_vs(base, "baz oran", "baz oran ile (Bolum 0 ek kosul)", terfi, alpha_adj, seed)
    sub[SUB_BASE] = ok_b
    parts.append(t_b)
    detail = "; ".join(parts) + f"; alpha={alpha}, n_trials={n_trials} -> ci={1.0 - alpha_adj:.4%}"
    if note:
        detail += f"; {note}"
    return Check(CHECK_BRIER, all(sub.values()), detail, sub)


def _iso_day(v) -> str | None:
    """Tarihi ISO gune (YYYY-MM-DD) normalize eder; yok ya da gecersizse None (fail-closed icin)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        ts = pd.Timestamp(v)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts) or ts.tzinfo is not None:
        return None
    return ts.normalize().date().isoformat()


def _check_holdout(holdout_result: dict | None, terfi: dict, ch_params: dict | None, cfg_hs: str | None) -> Check:
    """Kosul 3 (sinama kumesi). KABUL icin hepsi gerekir; eksik ya da uyumsuz alan KALDI (fail-closed):
    (a) holdout_result.experiment.use_holdout == True (terfi asamasi erisimi acikca yapilmis);
    (b) experiment.holdout_start == cfg holdout.holdout_start (sonuc gercek yapilandirmanin sinama kumesinde uretilmis);
    challenger egitimi: (c) train_includes_holdout == False, (d) train_max_date < holdout_start;
    (e) holdout sonucu challenger ile ayni spec; (f) main: model baz orandan iyi ve sifirdan ayrisir."""
    if not bool(terfi.get("require_holdout_pass", True)):
        return Check(CHECK_HOLDOUT, True, "atlandi (terfi.require_holdout_pass=false)")
    if holdout_result is None or not isinstance(holdout_result, dict):
        return Check(CHECK_HOLDOUT, False, "sinama kumesi kaniti yok")
    # (a)+(b) erisim ve pencere: sonucun gercek sinama kumesinde uretildiginin kaniti
    exp = holdout_result.get("experiment")
    if not isinstance(exp, dict):
        return Check(CHECK_HOLDOUT, False,
                     "sonuc 'experiment' alani yok: use_holdout ve holdout_start dogrulanamadi "
                     "(eski ya da bilinmeyen surumde uretilmis sonuc; fail-closed)")
    if "use_holdout" not in exp:
        return Check(CHECK_HOLDOUT, False,
                     "experiment.use_holdout eksik: sinama kumesine erisimin acikca yapildigi dogrulanamadi")
    if exp["use_holdout"] is not True:
        return Check(CHECK_HOLDOUT, False,
                     f"experiment.use_holdout={exp['use_holdout']!r}: sinama kumesi terfi asamasinda acilmamis "
                     "(use_holdout=True gerekir)")
    if "holdout_start" not in exp:
        return Check(CHECK_HOLDOUT, False, "experiment.holdout_start eksik: sinama penceresi dogrulanamadi")
    res_hs = _iso_day(exp["holdout_start"])
    if res_hs is None:
        return Check(CHECK_HOLDOUT, False, f"experiment.holdout_start gecersiz tarih: {exp['holdout_start']!r}")
    if cfg_hs is None:
        return Check(CHECK_HOLDOUT, False, "yapilandirmada holdout.holdout_start yok: sinama penceresi dogrulanamadi")
    if res_hs != cfg_hs:
        return Check(CHECK_HOLDOUT, False,
                     f"holdout_start uyusmuyor (sonuc {res_hs}, yapilandirma {cfg_hs}): sonuc bu yapilandirmanin "
                     "sinama kumesinde uretilmemis")
    # (c) challenger egitimi holdout'u disarida tutmus olmali (kayit yoksa ya da bayrak yoksa dogrulanamaz)
    extra = (ch_params or {}).get("extra") or {}
    if "train_includes_holdout" not in extra:
        return Check(CHECK_HOLDOUT, False,
                     "challenger egitim kaydi yok ya da holdout disarida tutuldugu dogrulanamadi "
                     "(extra.train_includes_holdout eksik)")
    if extra["train_includes_holdout"] is not False:
        return Check(CHECK_HOLDOUT, False,
                     "challenger egitimi sinama kumesini icerdi (train_includes_holdout=true): kanit gecersiz")
    # (d) egitimin kullandigi en son veri (end_date dahil) sinama kumesinden once bitmis olmali
    if "train_max_date" not in extra:
        return Check(CHECK_HOLDOUT, False,
                     "challenger egitim kaydinda train_max_date yok: egitimin kullandigi son tarih dogrulanamadi "
                     "(fail-closed)")
    tmax = _iso_day(extra["train_max_date"])
    if tmax is None:
        return Check(CHECK_HOLDOUT, False, f"challenger train_max_date gecersiz: {extra['train_max_date']!r}")
    if not pd.Timestamp(tmax) < pd.Timestamp(cfg_hs):
        return Check(CHECK_HOLDOUT, False,
                     f"challenger egitimi sinama penceresine sizdi: train_max_date {tmax} >= holdout_start {cfg_hs}")
    # (e) holdout sonucu ayni spec'ten olmali
    run_info = holdout_result.get("run_info")
    h_spec = run_info.get("spec") if isinstance(run_info, dict) else None
    c_spec = extra.get("spec")
    if not isinstance(h_spec, dict):
        return Check(CHECK_HOLDOUT, False, "holdout_result spec bilgisi yok (run_info.spec); challenger ile eslestirilemedi")
    if h_spec != c_spec:
        return Check(CHECK_HOLDOUT, False,
                     f"holdout_result spec'i challenger ile uyusmuyor (holdout: {h_spec.get('name')!r}, "
                     f"challenger: {(c_spec or {}).get('name')!r})")
    # (f) sonuc: model baz orandan iyi ve sifirdan ayrisan
    main = holdout_result.get("main")
    if not isinstance(main, dict):
        return Check(CHECK_HOLDOUT, False, "sinama kumesi kaniti yok (holdout_result.main eksik/bos)")
    better, excl = bool(main.get("model_better")), bool(main.get("excludes_zero"))
    info = (f"fark {_f(main.get('diff'))}, aralik [{_f(main.get('lo'))}, {_f(main.get('hi'))}], "
            f"{main.get('n_dates', holdout_result.get('n_dates'))} gun, model_better={better}, excludes_zero={excl}")
    if holdout_result.get("sufficient") is False:
        return Check(CHECK_HOLDOUT, False, f"sinama kumesi ornegi yetersiz (lab: sufficient=false); {info}")
    return Check(CHECK_HOLDOUT, bool(better and excl), info)


def _check_shadow(own_days: int, required: int, matched: pd.DataFrame) -> Check:
    head = f"challenger canli cozulmus farkli made_at gunu {own_days} (esik >= {required})"
    if own_days < required:
        return Check(CHECK_SHADOW, False, head + "; gun sayisi yetersiz")
    if len(matched) == 0:
        return Check(CHECK_SHADOW, False, head + "; eslesen satir yok: baz orana karsi iyilesme olculemedi")
    d = float(((matched["prob_up_ch"] - matched["label_up"]) ** 2
               - (matched["prob_up_ref"] - matched["label_up"]) ** 2).mean())
    ok = bool(d < 0.0)
    n_dates = int(matched["made_at"].nunique())
    return Check(CHECK_SHADOW, ok,
                 head + f"; baz orana karsi ortalama Brier farki (challenger - baz) {d:.5f} "
                 f"(gerekli < 0: {'iyilesme var' if ok else 'iyilesme yok'}), "
                 f"eslesen {len(matched)} satir / {n_dates} gun")


def _mean_excess(pairs: pd.DataFrame, col: str, cost: float) -> tuple[float, int]:
    """Kolon `col` skoru olan kohort portfoyunun kohort basina ortalama fazla net getirisi ve kohort sayisi."""
    port = pairs[["made_at", "ticker", col, "tradable_ret"]].rename(columns={col: "score"})
    s = simulate_cohort_portfolio(port, top_frac=TOP_FRAC, cost_bps=cost).summary
    return float(s["mean_excess_net"]), int(s["n_cohorts"])


def _net_vs(pairs: pd.DataFrame, ref_word: str, label: str, min_impr: float, cost: float) -> tuple[bool, str]:
    if len(pairs) == 0:
        return False, f"{label}: hesaplanamadi: eslesen satir yok"
    ex_ch, n_ch = _mean_excess(pairs, "prob_up_ch", cost)
    ex_ref, n_ref = _mean_excess(pairs, "prob_up_ref", cost)
    if n_ch == 0 or n_ref == 0:
        return False, f"{label}: hesaplanamadi: kohort yok (challenger {n_ch}, {ref_word} {n_ref})"
    ok = bool(ex_ch > ex_ref + min_impr)
    text = (f"{label}: kohort basina net fazla getiri: challenger {_f(ex_ch)} ({n_ch} kohort), "
            f"{ref_word} {_f(ex_ref)} ({n_ref} kohort), fark {_f(ex_ch - ex_ref)}; "
            f"gerekli: challenger > {ref_word} + {min_impr:g}")
    return ok, text


def _check_net(base: pd.DataFrame, champ: pd.DataFrame | None, champion: str | None, terfi: dict,
               cost: float, cost_label: str) -> Check:
    min_impr = float(terfi.get("min_improvement_net_return", 0.0) or 0.0)
    sub: dict[str, bool] = {}
    parts: list[str] = []
    if champion is not None:
        ok_c, t_c = _net_vs(champ, f"champion '{champion}'", f"champion '{champion}' ile (ANA, karar kosulu)",
                            min_impr, cost)
        sub[SUB_CHAMPION] = ok_c
        parts.append(t_c)
    ok_b, t_b = _net_vs(base, "referans (baz oran)", "baz oran ile (Bolum 0 ek kosul)", min_impr, cost)
    sub[SUB_BASE] = ok_b
    parts.append(t_b)
    return Check(CHECK_NET, all(sub.values()), "; ".join(parts) + f"; maliyet {cost:g} bps tek yon ({cost_label})", sub)


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
    """5 kosulun hepsini degerlendirir; approved = hepsi ok. Veri yetersizse cokmez, ilgili kosul ok=False olur.

    Baz oran her zaman karsilastirilir. champion verilirse kosul 2 (Brier) ve kosul 5 (net getiri) icin champion'a
    karsi ek karsilastirma yapilir ve iki karsilastirma da gecmelidir.
    """
    if not isinstance(challenger, str) or not challenger.strip():
        raise ValueError("challenger version bos olamaz")
    if champion is not None and champion == challenger:
        raise ValueError("challenger kendisiyle karsilastirilamaz")
    reference = BASELINE_REFERENCE
    bonferroni_alpha(alpha, n_trials)  # gecersiz alpha/n_trials erken ValueError

    terfi = cfg.get("terfi") or {}
    horizon = int(cfg["horizon_days"])
    min_dates = int(terfi.get("min_resolved_dates", 60))
    shadow_req = int(terfi.get("require_live_shadow_days", 60))
    cost, cost_label = resolve_cost(cfg, cost_bps)

    ch = _load_resolved(conn, challenger, horizon)
    matched, mism = _pair(ch, _load_resolved(conn, reference, horizon))
    n_days = int(matched["made_at"].nunique()) if len(matched) else 0
    own_days = int(ch["made_at"].nunique()) if len(ch) else 0

    champ_pairs = None
    mism_c = 0
    if champion is not None:
        champ_pairs, mism_c = _pair(ch, _load_resolved(conn, champion, horizon))

    ch_params = _load_params(conn, challenger)
    notes = []
    if mism:
        notes.append(f"UYARI: baz oran eslesmesinde {mism} satirda etiket farkli, dislandi")
    if mism_c:
        notes.append(f"UYARI: champion eslesmesinde {mism_c} satirda etiket farkli, dislandi")
    if ch_params and ch_params.get("label") == "label_rel_up":
        notes.append("NOT: challenger label_rel_up ile egitilmis, Brier label_up uzerinden olculdu")
    note = "; ".join(notes)

    checks = [
        _check_days(n_days, min_dates, len(matched)),
        _check_brier(matched, champ_pairs, champion, terfi, alpha, n_trials, seed, note),
        _check_holdout(holdout_result, terfi, ch_params, _iso_day((cfg.get("holdout") or {}).get("holdout_start"))),
        _check_shadow(own_days, shadow_req, matched),
        _check_net(matched, champ_pairs, champion, terfi, cost, cost_label),
    ]
    info: list[str] = []
    if champion is not None:
        info.append(f"champion '{champion}' verildi: kosul 2 (Brier) ve kosul 5 (net getiri) challenger'in "
                    "champion'dan da iyi olmasini ister (karar verici); ayrintilar kosul detayinda")
    approved = all(c.ok for c in checks)
    vs = f"champion '{champion}' ve " if champion is not None else ""
    if approved:
        summary = f"ONAY: {challenger}, {vs}{reference} referansina karsi 5 kosulun hepsini sagladi."
    else:
        failed = ", ".join(c.name for c in checks if not c.ok)
        summary = f"RED: {challenger} terfi edemez; basarisiz kosul(lar): {failed}."
    return PromotionDecision(approved=approved, challenger=challenger, reference=reference,
                             checks=checks, summary=summary, info=info)


def format_decision(decision: PromotionDecision) -> str:
    """Turkce metin ozeti (kosul kosul sonuc ve sayilar)."""
    L = [f"## Terfi kontrolu: {decision.challenger} (referans: {decision.reference})", "", decision.summary, ""]
    for c in decision.checks:
        L.append(f"- [{'GECTI' if c.ok else 'KALDI'}] {c.name}: {c.detail}")
    n_ok = sum(1 for c in decision.checks if c.ok)
    L.append("")
    L.append(f"Sonuc: {n_ok}/{len(decision.checks)} kosul saglandi; karar: {'ONAY' if decision.approved else 'RED'} "
             "(terfi icin tum kosullar gerekir).")
    for s in decision.info:
        L.append(f"Not: {s}")
    L.append("")
    L.append(DISCLAIMER)
    return "\n".join(L)
