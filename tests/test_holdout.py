"""Sinama kumesi kilidi (PROJE.md Bolum 5). Saf: DB yok."""

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd
import pytest

from bist_tahmin.backtest.guard import HoldoutGuard, HoldoutLocked

HS = "2026-05-01"


def frame():
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-04-20", "2026-04-27", "2026-04-28", "2026-05-01", "2026-05-04"]),
            # etiket bitisi: 5 gun sonra (kabaca); 04-27 ve 04-28'inki holdout'a uzanir
            "end_date": pd.to_datetime(["2026-04-27", "2026-05-04", "2026-05-05", "2026-05-08", "2026-05-11"]),
            "v": range(5),
        }
    )


@pytest.mark.fast
def test_check_locked_raises_for_holdout_dates():
    g = HoldoutGuard(pd.Timestamp(HS))
    g.check()  # bos
    g.check("2026-04-30", dt.date(2026, 4, 1), pd.Timestamp("2026-04-30 23:00"))
    for bad in ["2026-05-01", dt.date(2026, 5, 2), pd.Timestamp("2026-06-01"), np.datetime64("2026-05-01")]:
        with pytest.raises(HoldoutLocked):
            g.check(bad)
    with pytest.raises(HoldoutLocked):
        g.check("2026-01-01", "2026-05-01")  # biri yeterli
    assert issubclass(HoldoutLocked, RuntimeError)


@pytest.mark.fast
def test_check_accepts_arrays_and_rejects_nat():
    g = HoldoutGuard(pd.Timestamp(HS))
    g.check(pd.DatetimeIndex(["2026-01-01", "2026-04-30"]))
    with pytest.raises(HoldoutLocked):
        g.check(pd.Series(pd.to_datetime(["2026-01-01", "2026-05-03"])))
    with pytest.raises(ValueError):
        g.check(pd.NaT)


@pytest.mark.fast
def test_clip_end():
    assert HoldoutGuard(pd.Timestamp(HS)).clip_end() == pd.Timestamp("2026-04-30")
    assert HoldoutGuard(pd.Timestamp(HS), use_holdout=True).clip_end() is None


@pytest.mark.fast
def test_filter_frame_drops_holdout_rows_when_locked():
    out = HoldoutGuard(pd.Timestamp(HS)).filter_frame(frame())
    assert list(out["v"]) == [0, 1, 2]  # date >= holdout_start dusurulur, etiket bitisine bakilmaz


@pytest.mark.fast
def test_filter_frame_drops_rows_whose_label_reaches_holdout():
    out = HoldoutGuard(pd.Timestamp(HS)).filter_frame(frame(), end_col="end_date")
    assert list(out["v"]) == [0]  # yalniz etiketi holdout'tan once biten satir kalir
    assert (out["end_date"] < pd.Timestamp(HS)).all()


@pytest.mark.fast
def test_filter_frame_drops_unknown_label_end_conservatively():
    df = frame()
    df.loc[0, "end_date"] = pd.NaT
    out = HoldoutGuard(pd.Timestamp(HS)).filter_frame(df, end_col="end_date")
    assert len(out) == 0


@pytest.mark.fast
def test_filter_frame_custom_columns_and_missing_column():
    g = HoldoutGuard(pd.Timestamp(HS))
    df = frame().rename(columns={"date": "made_at"})
    assert list(g.filter_frame(df, date_col="made_at")["v"]) == [0, 1, 2]
    with pytest.raises(ValueError):
        g.filter_frame(df)  # "date" yok
    with pytest.raises(ValueError):
        g.filter_frame(frame(), end_col="yok")


@pytest.mark.fast
def test_filter_frame_does_not_mutate_input():
    df = frame()
    before = df.copy()
    HoldoutGuard(pd.Timestamp(HS)).filter_frame(df, end_col="end_date")
    pd.testing.assert_frame_equal(df, before)


@pytest.mark.fast
def test_unlocked_passes_everything_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="bist_tahmin.backtest.guard"):
        g = HoldoutGuard(pd.Timestamp(HS), use_holdout=True)
    assert any("KILIDI ACIK" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    g.check("2030-01-01", dt.date(2026, 5, 1))
    out = g.filter_frame(frame(), end_col="end_date")
    assert len(out) == 5
    assert out is not None and out.index.equals(frame().index)
    assert "ACIK" in g.access_note() and "2026-05-01" in g.access_note()


@pytest.mark.fast
def test_locked_does_not_warn_and_note_says_locked(caplog):
    with caplog.at_level(logging.WARNING, logger="bist_tahmin.backtest.guard"):
        g = HoldoutGuard(pd.Timestamp(HS))
    assert not caplog.records
    assert "kilitli" in g.access_note()


@pytest.mark.fast
def test_unlocked_copy_is_independent():
    df = frame()
    out = HoldoutGuard(pd.Timestamp(HS), use_holdout=True).filter_frame(df)
    out.loc[0, "v"] = 99
    assert df.loc[0, "v"] == 0


@pytest.mark.fast
def test_from_config():
    cfg = {"holdout": {"holdout_start": HS}}
    g = HoldoutGuard.from_config(cfg)
    assert g.holdout_start == pd.Timestamp(HS) and g.use_holdout is False
    cfg2 = {"holdout": {"holdout_start": dt.date(2026, 5, 1)}}
    assert HoldoutGuard.from_config(cfg2, use_holdout=True).use_holdout is True


@pytest.mark.fast
def test_holdout_start_normalized_to_midnight():
    g = HoldoutGuard("2026-05-01 15:00")
    assert g.holdout_start == pd.Timestamp(HS)
