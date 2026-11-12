"""Walk-forward bolmeleri (PROJE.md Bolum 5 purge, Bolum 13 test_purge). Saf: DB yok."""

from __future__ import annotations

import pandas as pd
import pytest

from bist_tahmin.backtest.splits import Fold, assert_no_label_overlap, make_folds

# 20 is gunu: idx 0..19
DAYS = pd.bdate_range("2026-01-05", periods=20)


def kw(**over):
    base = dict(horizon_days=2, purge_days=2, min_train_days=6, retrain_every_days=4)
    base.update(over)
    return base


def idx(folds):
    """Fold'u (train_end idx, test idx listesi) olarak ozetler."""
    pos = {d: i for i, d in enumerate(DAYS)}
    return [(pos[f.train_end], [pos[d] for d in f.test_dates]) for f in folds]


@pytest.mark.fast
def test_hand_computed_boundaries_and_short_last_block():
    folds = make_folds(DAYS, **kw())
    # ilk test = idx 6 (ilk 6 gun yalnizca egitimde); train_end = test_ilk - purge(2)
    assert idx(folds) == [
        (4, [6, 7, 8, 9]),
        (8, [10, 11, 12, 13]),
        (12, [14, 15, 16, 17]),
        (16, [18, 19]),  # son blok kisa
    ]
    assert [f.fold_id for f in folds] == [0, 1, 2, 3]
    assert all(isinstance(f, Fold) for f in folds)


@pytest.mark.fast
def test_no_overlap_no_gaps_and_increasing_train_end():
    folds = make_folds(DAYS, **kw())
    all_test = [d for f in folds for d in f.test_dates]
    assert len(all_test) == len(set(all_test))  # test kumeleri orgusuz
    assert list(all_test) == list(DAYS[6:])  # bosluksuz, ardisik
    ends = [f.train_end for f in folds]
    assert ends == sorted(ends) and len(set(ends)) == len(ends)


@pytest.mark.fast
def test_purge_label_window_never_crosses_first_test_day():
    """Egitim satiri d (d <= train_end) icin etiket bitisi d+horizon <= ilk test gunu (test_purge)."""
    for purge, horizon in [(2, 2), (3, 2), (5, 5)]:
        folds = make_folds(DAYS, horizon_days=horizon, purge_days=purge, min_train_days=8, retrain_every_days=3)
        pos = {d: i for i, d in enumerate(DAYS)}
        for f in folds:
            train_days = DAYS[DAYS <= f.train_end]
            label_end_idx = pd.Series([pos[d] + horizon for d in train_days])
            assert label_end_idx.max() <= pos[f.test_dates[0]]
            assert pos[f.test_dates[0]] - pos[f.train_end] == purge
            ends = pd.Series([DAYS[min(i, len(DAYS) - 1)] for i in label_end_idx])
            assert_no_label_overlap(ends, f.test_dates[0])


@pytest.mark.fast
def test_min_train_days_first_test_after_min_train():
    folds = make_folds(DAYS, **kw(min_train_days=10))
    assert folds[0].test_dates[0] == DAYS[10]
    # ilk min_train_days gun hicbir test kumesinde yok
    assert not set(DAYS[:10]) & {d for f in folds for d in f.test_dates}


@pytest.mark.fast
def test_not_enough_days_returns_empty():
    assert make_folds(DAYS[:6], **kw()) == []
    assert make_folds(DAYS[:0], **kw()) == []


@pytest.mark.fast
def test_start_end_restrict_test_range_inclusive():
    folds = make_folds(DAYS, **kw(), start=DAYS[9], end=DAYS[16])
    # blok sinirlari korunur: ilk blok 6..9 -> yalniz 9; son blok 14..17 -> 14..16
    assert idx(folds) == [(4, [9]), (8, [10, 11, 12, 13]), (12, [14, 15, 16])]
    assert [f.fold_id for f in folds] == [0, 1, 2]
    # start/end verilmeden hesaplanan ayni test gunleri icin train_end degismez
    full = {d: f.train_end for f in make_folds(DAYS, **kw()) for d in f.test_dates}
    assert all(full[d] == f.train_end for f in folds for d in f.test_dates)


@pytest.mark.fast
def test_end_before_first_test_gives_empty():
    assert make_folds(DAYS, **kw(), end=DAYS[5]) == []
    assert make_folds(DAYS, **kw(), start=DAYS[19] + pd.Timedelta(days=5)) == []


@pytest.mark.fast
def test_training_starts_at_first_day_expanding():
    folds = make_folds(DAYS, **kw())
    sizes = [int((DAYS <= f.train_end).sum()) for f in folds]
    assert sizes == [5, 9, 13, 17]  # genisleyen pencere, days[0]'dan


@pytest.mark.parametrize(
    "over",
    [
        dict(purge_days=1, horizon_days=2),  # purge < horizon
        dict(horizon_days=0, purge_days=0),
        dict(min_train_days=0),
        dict(min_train_days=1, purge_days=2),  # min_train < purge
        dict(retrain_every_days=0),
        dict(retrain_every_days=2.5),
        dict(retrain_every_days=True),
    ],
)
@pytest.mark.fast
def test_bad_parameters(over):
    with pytest.raises(ValueError):
        make_folds(DAYS, **kw(**over))


@pytest.mark.fast
def test_bad_days_and_range():
    with pytest.raises(ValueError):
        make_folds(DAYS[::-1], **kw())
    with pytest.raises(ValueError):
        make_folds(DAYS.append(DAYS[:1]).sort_values(), **kw())  # tekrar
    with pytest.raises(ValueError):
        make_folds(list(DAYS), **kw())  # DatetimeIndex degil
    with pytest.raises(ValueError):
        make_folds(DAYS, **kw(), start=DAYS[10], end=DAYS[5])


@pytest.mark.fast
def test_assert_no_label_overlap():
    first = pd.Timestamp("2026-03-10")
    ok = pd.Series(pd.to_datetime(["2026-03-02", "2026-03-10"]))  # esitlik serbest
    assert_no_label_overlap(ok, first)
    assert_no_label_overlap(pd.Series([], dtype="datetime64[ns]"), first)
    bad = pd.Series(pd.to_datetime(["2026-03-02", "2026-03-11"]))
    with pytest.raises(AssertionError, match="kesiyor"):
        assert_no_label_overlap(bad, first)
    with pytest.raises(AssertionError, match="bilinmiyor"):
        assert_no_label_overlap(pd.Series(pd.to_datetime(["2026-03-02", None])), first)
