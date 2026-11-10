"""source.py: normalizasyon (tz/kolon/index) ve ham-fiyat dogrulamasi. Ag gerektirmeyenler varsayilan kosuda calisir."""

from __future__ import annotations

import os

import pandas as pd
import pytest

from bist_tahmin.data.source import (
    COLUMNS,
    BorsapySource,
    SourceError,
    bonus_events,
    check_raw_vs_adjusted,
    normalize_frame,
)

network = pytest.mark.skipif(not os.environ.get("BIST_NETWORK_TESTS"), reason="BIST_NETWORK_TESTS ayarli degil")


def _borsapy_like(closes, start="2026-05-11", mult=1.0):
    idx = pd.date_range(start, periods=len(closes), freq="B", tz="Europe/Istanbul") + pd.Timedelta(hours=9)
    idx.name = "Date"
    return pd.DataFrame(
        {"Open": [c * mult for c in closes], "High": [c * mult + 1 for c in closes],
         "Low": [c * mult - 1 for c in closes], "Close": [c * mult for c in closes],
         "Volume": [1000.0] * len(closes)},
        index=idx,
    )


def test_normalize_index_and_columns():
    raw = _borsapy_like([10, 11, 12])
    adj = _borsapy_like([5, 5.5, 6])
    out = normalize_frame(raw, adj)
    assert list(out.columns) == COLUMNS
    assert out.index.name == "date"
    assert out.index.tz is None
    assert (out.index == out.index.normalize()).all()  # 09:00 damgasi gitti
    assert out.index[0] == pd.Timestamp("2026-05-11")  # UTC'ye kayip tarih degismedi
    assert out["close"].tolist() == [10, 11, 12]
    assert out["close_adj"].tolist() == [5, 5.5, 6]
    assert out.index.is_monotonic_increasing and out.index.is_unique


def test_normalize_without_adj_copies_close():
    out = normalize_frame(_borsapy_like([10, 11]), None)
    assert out["close_adj"].tolist() == [10, 11]


def test_normalize_drops_duplicate_dates_keeps_last():
    raw = _borsapy_like([10, 11, 12])
    dup = pd.concat([raw, raw.iloc[[1]].assign(Close=99.0)]).sort_index()
    out = normalize_frame(dup, None)
    assert out.index.is_unique and out.loc["2026-05-12", "close"] == 99.0


def test_normalize_empty():
    out = normalize_frame(pd.DataFrame(), None)
    assert out.empty and list(out.columns) == COLUMNS and out.index.name == "date"


def test_bonus_events_ratio():
    splits = pd.DataFrame(
        {"BonusFromCapital": [100.0, 0.0, 0.0], "BonusFromDividend": [0.0, 20.0, 0.0], "RightsIssue": [0, 0, 30.0]},
        index=pd.to_datetime(["2026-05-14", "2020-01-02", "2019-01-03"]),
    )
    ev = bonus_events(splits)
    assert ev == [(pd.Timestamp("2020-01-02"), 1.2), (pd.Timestamp("2026-05-14"), 2.0)]  # bedelli sayilmaz


def _frame(close, close_adj):
    idx = pd.date_range("2026-05-11", periods=len(close), freq="B", name="date")
    return pd.DataFrame({"close": close, "close_adj": close_adj}, index=idx)


def test_check_raw_passes_when_unadjusted():
    df = _frame([800.0, 810.0, 405.0, 400.0], [400.0, 405.0, 405.0, 400.0])
    check_raw_vs_adjusted(df, [(pd.Timestamp("2026-05-13"), 2.0)], "X")  # hata yok


def test_check_raw_raises_when_raw_equals_adjusted():
    """borsapy adjust=False sessizce duzeltmeli seri dondururse (splits alinamadi) yakalanir."""
    df = _frame([400.0, 405.0, 405.0, 400.0], [400.0, 405.0, 405.0, 400.0])
    with pytest.raises(SourceError):
        check_raw_vs_adjusted(df, [(pd.Timestamp("2026-05-13"), 2.0)], "X")


def test_check_raw_ignores_events_outside_range():
    df = _frame([400.0, 405.0], [400.0, 405.0])
    check_raw_vs_adjusted(df, [(pd.Timestamp("2020-01-01"), 2.0)], "X")


def test_borsapy_source_splits_failure_is_error(monkeypatch):
    """splits alinamazsa ham fiyat dogrulanamaz: sessizce close=close_adj yazmak yerine hata."""
    class FakeTicker:
        def __init__(self, _):
            pass

        @property
        def splits(self):
            raise TimeoutError("isyatirim zaman asimi")

    class FakeBp:
        Ticker = FakeTicker

    src = BorsapySource.__new__(BorsapySource)
    src._bp = FakeBp
    src._splits_cache = {}
    with pytest.raises(SourceError):
        src.history("BIMAS", "2026-01-01")


class _CountingBp:
    """Sahte borsapy: Ticker.splits borsapy gibi ornek basina bir kez cekilir (cached_property);
    history(adjust=False) ayni ornekte splits okur (_unadjust_prices)."""

    def __init__(self, fail_times=0):
        self.fetches = 0
        self.fail_left = fail_times
        outer = self

        class Ticker:
            def __init__(self, _):
                self._splits = None

            @property
            def splits(self):
                if self._splits is None:
                    outer.fetches += 1
                    if outer.fail_left > 0:
                        outer.fail_left -= 1
                        raise TimeoutError("isyatirim zaman asimi")
                    self._splits = pd.DataFrame(
                        {"BonusFromCapital": [100.0], "BonusFromDividend": [0.0], "RightsIssue": [0.0]},
                        index=pd.to_datetime(["2026-05-13"]),
                    )
                return self._splits

            @property
            def dividends(self):
                return pd.DataFrame({"Amount": []})

            def history(self, period="1mo", interval="1d", start=None, end=None, actions=False,
                        adjust=True, auto_adjust=False):
                if not adjust:
                    try:
                        self.splits
                    except Exception:
                        pass  # borsapy _unadjust_prices davranisi: hatayi yutar
                    return _borsapy_like([800, 810, 405, 400])
                return _borsapy_like([400, 405, 405, 400])

        self.Ticker = Ticker


def _source(bp):
    src = BorsapySource.__new__(BorsapySource)
    src._bp = bp
    src._splits_cache = {}
    return src


def test_splits_fetched_once_per_ticker_in_fetch_flow():
    """history + corporate_actions (fetch.py akisi): splits gercek cagrisi hisse basina bir kez."""
    bp = _CountingBp()
    src = _source(bp)
    df = src.history("BIMAS", "2026-05-08", "2026-05-20")
    ca = src.corporate_actions("BIMAS")
    assert bp.fetches == 1
    assert (ca["kind"] == "split").sum() == 1  # onbellekten de bedelsiz olay kayitlari gelir
    assert df["close_adj"].tolist() == [400, 405, 405, 400]


def test_splits_cache_is_per_ticker_and_per_instance():
    bp = _CountingBp()
    src = _source(bp)
    src.history("BIMAS", "2026-05-08", "2026-05-20")
    src.history("THYAO", "2026-05-08", "2026-05-20")
    assert bp.fetches == 2  # iki hisse, ikisi de bir kez
    other = _source(_CountingBp())
    other.history("BIMAS", "2026-05-08", "2026-05-20")
    assert other._bp.fetches == 1  # baska ornek kendi onbellegini kurar


def test_splits_failure_raises_and_is_not_cached():
    bp = _CountingBp(fail_times=1)
    src = _source(bp)
    with pytest.raises(SourceError):
        src.history("BIMAS", "2026-05-08", "2026-05-20")
    assert "BIMAS" not in src._splits_cache
    df = src.history("BIMAS", "2026-05-08", "2026-05-20")  # tekrar denenir ve basarili olur
    assert bp.fetches == 2 and len(df) == 4
    assert "BIMAS" in src._splits_cache


def test_corporate_actions_failure_propagates_unchanged_and_not_cached():
    """corporate_actions eskiden splits hatasini sarmiyordu; bu davranis korunur ve onbellek bos kalir."""
    bp = _CountingBp(fail_times=1)
    src = _source(bp)
    with pytest.raises(TimeoutError):
        src.corporate_actions("BIMAS")
    assert "BIMAS" not in src._splits_cache
    assert len(src.corporate_actions("BIMAS")) == 1


@network
@pytest.mark.network
def test_network_raw_price_has_jump_on_bimas_bonus_day():
    src = BorsapySource()
    df = src.history("BIMAS", "2026-05-08", "2026-05-20")
    day = pd.Timestamp("2026-05-14")
    raw_ret = df["close"].pct_change().loc[day]
    adj_ret = df["close_adj"].pct_change().loc[day]
    assert raw_ret < -0.4          # ham seride bedelsiz gunu ~%-49 sicrama
    assert abs(adj_ret) < 0.1      # duzeltmeli seride sicrama yok
    assert df.index.tz is None and df.index.name == "date"
