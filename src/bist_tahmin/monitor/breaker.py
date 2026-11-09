"""Devre kesici (PROJE.md Bolum 8 `circuit_breaker`, Bolum 12).

Kural: simule edilmis stratejinin (kohort portfoyu, en ust %20 skor, net getiri) esit agirlikli evrene (benchmark =
tradable_ret ortalamasi) gore GORELI dususu `max_relative_drawdown`'i asarsa model uretimi durur. Mutlak dusus
kullanilmaz (piyasa dusarken model bozulmamis olabilir).

Kararlar
- YALNIZ model satirlari (champion/challenger) degerlendirilir; baseline'lar olcum kontroludur, model iddiasi degildir:
  devre kesici acikken baseline'lar yazilmaya devam eder (predict/daily.py).
- OTOMATIK YENIDEN BASLAMAZ: tek yol `reset(conn, reason)` ve gerekce ZORUNLUDUR. Onceki durum 'history'ye eklenir.
- reset'ten sonra degerlendirme yeniden baslar: `evaluate_after` (reset gunu) ve oncesi made_at'li kohortlar sayilmaz.
  Aksi halde zirveden dusus ayni gecmis veriden hemen tekrar tetiklenirdi (sonsuz dongu).
- Esik STRICT asimla tetiklenir (dusus > esik). Veri yoksa tetiklenmez (kanit yok).
- Ozkaynak gecersizse (<= 0, NaN) tetiklenir: belirsizlikte durmak guvenli taraftir.
- equity_curve MTM icermez (evaluate/portfolio.py): dusus ALT SINIRDIR; gercek dusus daha derin olabilir.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg

from ..evaluate.portfolio import PortfolioResult, equity_curve, relative_drawdown, simulate_cohort_portfolio
from .state import get_state, set_state

STATE_KEY = "circuit_breaker"
TZ = ZoneInfo("Europe/Istanbul")
TOP_FRAC = 0.2


@dataclass(frozen=True)
class BreakerVerdict:
    tripped: bool
    relative_drawdown: float
    detail: str


def _empty_state() -> dict:
    return {"tripped": False, "since": None, "reason": None, "model_version": None, "relative_drawdown": None,
            "evaluate_after": None, "reset_at": None, "reset_reason": None, "history": []}


def evaluate_breaker(
    model_net_ret: pd.Series, benchmark_ret: pd.Series, horizon_days: int, max_relative_drawdown: float
) -> BreakerVerdict:
    """SAF karar: kohort getirilerinden (index = made_at) goreli dususu hesaplar.

    Her kohort sermayenin 1/H'si (toplamsal ozkaynak: 1 + sum(ret/H)); oran = model/benchmark; dusus = oranin
    zirveden en buyuk dususu (`portfolio.relative_drawdown`). Iki seri ortak index'e hizalanir, NaN'lar dusurulur."""
    limit = float(max_relative_drawdown)
    if not (0.0 < limit < 1.0):
        raise ValueError(f"max_relative_drawdown (0, 1) araliginda olmali: {max_relative_drawdown!r}")
    cohorts = pd.DataFrame({"net_ret": pd.Series(model_net_ret, dtype=float),
                            "benchmark_ret": pd.Series(benchmark_ret, dtype=float)}).dropna().sort_index()
    if cohorts.empty:
        return BreakerVerdict(False, 0.0, "cozulmus kohort yok; degerlendirilemedi (tetiklenmedi)")
    res = PortfolioResult(cohorts=cohorts, summary={})
    try:
        rdd = relative_drawdown(equity_curve(res, horizon_days, kind="net"),
                                equity_curve(res, horizon_days, kind="benchmark"))
    except ValueError as exc:   # ozkaynak <= 0 / sonsuz: belirsizlikte dur
        return BreakerVerdict(True, 1.0, f"ozkaynak egrisi gecersiz ({exc}); guvenli tarafta durduruldu")
    tripped = bool(rdd > limit)
    detail = (f"{len(cohorts)} kohort: goreli dusus {rdd:.2%} " + (">" if tripped else "<=") + f" esik {limit:.2%}")
    return BreakerVerdict(tripped, float(rdd), detail)


def model_cohort_returns(
    df: pd.DataFrame, model_version: str, *, cost_bps: float = 0.0, top_frac: float = TOP_FRAC,
    min_universe: int = 5, after: dt.date | str | pd.Timestamp | None = None,
) -> tuple[pd.Series, pd.Series]:
    """Tek modelin COZULMUS satirlarindan (net kohort getirisi, benchmark getirisi); index = made_at.

    `df` kolonlari: made_at, ticker, model_version, score, tradable_ret. `after`: bu tarihten (HARIC) onceki
    kohortlar sayilmaz (reset sonrasi yeniden baslangic)."""
    d = df[df["model_version"] == model_version]
    if after is not None:
        d = d[pd.to_datetime(d["made_at"]) > pd.Timestamp(after)]
    if d.empty:
        empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], name="made_at"))
        return empty, empty
    sim = simulate_cohort_portfolio(d[["made_at", "ticker", "score", "tradable_ret"]], top_frac=top_frac,
                                    cost_bps=cost_bps, min_universe=min_universe)
    return sim.cohorts["net_ret"], sim.cohorts["benchmark_ret"]


def configured_cost_bps(cfg: dict) -> float:
    """costs.commission_bps + spread_bps; ikisi de null iken 0 (oranlar dogrulanmadi; duyarlilik raporda)."""
    costs = cfg.get("costs") or {}
    total = 0.0
    for k in ("commission_bps", "spread_bps"):
        v = costs.get(k)
        if v is not None and math.isfinite(float(v)):
            total += float(v)
    return total


def evaluate_model(df: pd.DataFrame, model_version: str, cfg: dict, *, after=None) -> BreakerVerdict:
    """cfg ile tek model icin devre kesici karari (df = `drift.load_resolved` ciktisi)."""
    net, bench = model_cohort_returns(df, model_version, cost_bps=configured_cost_bps(cfg), after=after)
    return evaluate_breaker(net, bench, int(cfg["horizon_days"]), float(cfg["circuit_breaker"]["max_relative_drawdown"]))


# --- kalici durum -------------------------------------------------------------------------------------------------

def status(conn: psycopg.Connection) -> dict:
    """Devre kesici durumu: tripped, since, reason, model_version, relative_drawdown (+ evaluate_after, reset_*,
    history). Kayit yoksa kapali varsayilan doner."""
    raw = get_state(conn, STATE_KEY)
    out = _empty_state()
    if isinstance(raw, dict):
        out.update(raw)
        if not isinstance(out.get("history"), list):
            out["history"] = []
    return out


def _now(now: dt.datetime | None) -> dt.datetime:
    n = now or dt.datetime.now(dt.timezone.utc)
    if n.tzinfo is None:
        raise ValueError("now tz-aware olmali")
    return n


def trip(conn: psycopg.Connection, verdict: BreakerVerdict, model_version: str, *, now: dt.datetime | None = None) -> bool:
    """Devre kesiciyi acar. Zaten aciksa DEGISTIRMEZ ('since' korunur) ve False doner; ilk acista True."""
    cur = status(conn)
    if cur["tripped"]:
        return False
    new = dict(cur)
    new.update({"tripped": True, "since": _now(now).isoformat(), "reason": verdict.detail,
                "model_version": model_version, "relative_drawdown": verdict.relative_drawdown})
    set_state(conn, STATE_KEY, new)
    return True


def reset(conn: psycopg.Connection, reason: str, *, now: dt.datetime | None = None) -> dict:
    """Devre kesiciyi elle kapatir. Gerekce ZORUNLU (bos/bosluk -> ValueError). Onceki durum 'history'ye eklenir;
    degerlendirme reset gununden sonraki kohortlarla yeniden baslar. Yeni durumu dondurur."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("devre kesici sifirlamak icin acik bir gerekce (reason) zorunlu")
    n = _now(now)
    cur = status(conn)
    prior = {k: cur.get(k) for k in ("tripped", "since", "reason", "model_version", "relative_drawdown")}
    history = list(cur.get("history") or [])
    history.append({**prior, "reset_at": n.isoformat(), "reset_reason": reason.strip()})
    new = _empty_state()
    new.update({"evaluate_after": n.astimezone(TZ).date().isoformat(), "reset_at": n.isoformat(),
                "reset_reason": reason.strip(), "history": history})
    set_state(conn, STATE_KEY, new)
    return new


def evaluate_after_date(st: dict) -> dt.date | None:
    v = st.get("evaluate_after")
    return dt.date.fromisoformat(v) if v else None
