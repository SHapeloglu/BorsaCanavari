"""Faz 3.5 veri seti insa edici testleri: agirlikla sentetik/DB'siz; tek test gomulu Postgres kullanir."""

from __future__ import annotations

import datetime as dt
import math
import uuid

import numpy as np
import pandas as pd
import psycopg
import pytest
from conftest import APP_PASSWORD

from bist_tahmin.backtest import dataset as ds_mod
from bist_tahmin.backtest.dataset import (
    FRAME_COLUMNS,
    Dataset,
    build_dataset,
    build_dataset_from_frames,
    compute_labels,
    load_dataset,
    save_dataset,
)
from bist_tahmin.calendar import TradingCalendar
from bist_tahmin.config import load_config
from bist_tahmin.data import store
from bist_tahmin.data.universe import Membership, members_on
from bist_tahmin.db import APP_ROLE, run_init, with_overrides
from bist_tahmin.features.build import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    FeatureSet,
    build_features,
    make_feature_row,
)

CFG = {"horizon_days": 5, "label_threshold": 0.0}
N = 130


def _frame(close_adj, dates, *, open_adj=None, volume=None) -> pd.DataFrame:
    close_adj = np.asarray(close_adj, dtype=float)
    open_adj = close_adj if open_adj is None else np.asarray(open_adj, dtype=float)
    volume = np.full(len(close_adj), 1000.0) if volume is None else np.asarray(volume, dtype=float)
    return pd.DataFrame(
        {"open": open_adj, "high": close_adj, "low": close_adj, "close": close_adj, "close_adj": close_adj,
         "open_adj": open_adj, "volume": volume},
        index=pd.DatetimeIndex(dates, name="date"),
    )


def _walk(rng, n, start=100.0, vol=0.015):
    return start * np.exp(np.cumsum(rng.normal(0.0003, vol, n)))


def _world(tickers=("AAA", "BBB", "CCC", "DDD", "EEE"), n=N, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n)
    hist = {}
    for t in tickers:
        c = _walk(rng, n)
        hist[t] = _frame(c, dates, open_adj=c * (1 + rng.normal(0, 0.004, n)), volume=rng.uniform(500, 1500, n))
    xu = _frame(_walk(rng, n, 9000.0, 0.01), dates, volume=np.full(n, 5e6))
    return hist, xu, TradingCalendar(dates)


def _members(tickers, frm=dt.date(2020, 1, 1), to=None):
    return [Membership(t, frm, to) for t in tickers]


def _build(hist, xu, cal, mem, cfg=CFG, **kw):
    kw.setdefault("preflight_dates", 0)
    return build_dataset_from_frames(hist, xu, cal, mem, cfg, **kw)


@pytest.fixture(scope="module")
def world():
    hist, xu, cal = _world()
    mem = _members(hist)
    return hist, xu, cal, mem, _build(hist, xu, cal, mem)


# --- sema ----------------------------------------------------------------------------------------------------

def test_schema_order_and_dtypes(world):
    hist, xu, cal, mem, ds = world
    f = ds.frame
    assert isinstance(ds, Dataset)
    assert list(f.columns) == ["date", "ticker", *FEATURE_NAMES, "end_date", "fwd_ret", "fwd_rel_ret",
                               "tradable_ret", "label_up", "label_rel_up"]
    assert list(f.columns) == list(FRAME_COLUMNS)
    assert ds.feature_names == list(FEATURE_NAMES)
    assert (ds.horizon_days, ds.label_threshold) == (5, 0.0)
    assert f["date"].dtype == np.dtype("datetime64[ns]") and f["end_date"].dtype == np.dtype("datetime64[ns]")
    assert f["date"].dt.tz is None
    assert f["ticker"].map(type).eq(str).all()
    for c in [*FEATURE_NAMES, "fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"]:
        assert f[c].dtype == np.float64, c
    assert isinstance(f.index, pd.RangeIndex) and f.index.start == 0
    keys = list(zip(f["date"], f["ticker"]))
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    # ozellik sutunlari tam dolu (build_features sonlu degerleri doner)
    assert np.isfinite(f[list(FEATURE_NAMES)].to_numpy()).all()
    m = ds.meta
    assert m["feature_version"] == FEATURE_VERSION and m["n_rows"] == len(f)
    assert m["date_min"] == f["date"].min().date().isoformat() and m["date_max"] == f["date"].max().date().isoformat()
    assert m["built_at"] and m["skipped_tickers"] == []


# --- tarihli uyelik --------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_dated_membership_only_in_range():
    hist, xu, cal = _world(("AAA", "BBB", "CCC"))
    d70, d100 = cal.days[70].date(), cal.days[100].date()
    mem = [*_members(["AAA"]), Membership("BBB", d70, d100)]       # CCC hic uye degil
    f = _build(hist, xu, cal, mem).frame
    assert set(f["ticker"]) == {"AAA", "BBB"}
    b_days = f.loc[f["ticker"] == "BBB", "date"]
    assert b_days.min() == cal.days[70] and b_days.max() == cal.days[99]    # yari acik: valid_to dahil degil
    assert len(b_days) == 30
    a_days = f.loc[f["ticker"] == "AAA", "date"]
    assert a_days.min() == cal.days[59]       # ilk 59 gunde 60 bar gecmis yok -> build_features dislar, satir yok
    assert a_days.max() == cal.days[-1] and len(a_days) == N - 59


def test_features_equal_build_features_on_members_only():
    hist, xu, cal = _world(("AAA", "BBB", "CCC", "DDD"))
    d80 = cal.days[80].date()
    mem = [*_members(["AAA", "BBB", "CCC"]), Membership("DDD", d80, None)]
    f = _build(hist, xu, cal, mem).frame
    for pos in (59, 79, 80, 100, N - 1):
        d = cal.days[pos]
        members = members_on(mem, d.date())
        fs = build_features({t: hist[t] for t in members}, xu, d, cal)      # yalniz o gunun uyeleri
        got = f[f["date"] == d].set_index("ticker")
        assert sorted(got.index) == sorted(fs.rows)
        for t, row in fs.rows.items():
            assert [got.loc[t, n] for n in FEATURE_NAMES] == [row.features[n] for n in FEATURE_NAMES], (d, t)


def test_parallel_equals_sequential():
    hist, xu, cal = _world(("AAA", "BBB", "CCC"))
    mem = _members(hist)
    a = _build(hist, xu, cal, mem, n_jobs=1).frame
    b = _build(hist, xu, cal, mem, n_jobs=2).frame
    pd.testing.assert_frame_equal(a, b)


def test_member_without_prices_skipped_and_recorded():
    hist, xu, cal = _world(("AAA", "BBB"))
    mem = _members(["AAA", "BBB", "ZZZ"])          # ZZZ (ornegin delist) icin fiyat yok
    ds = _build(hist, xu, cal, mem)
    assert set(ds.frame["ticker"]) == {"AAA", "BBB"}
    assert ds.meta["skipped_tickers"] == ["ZZZ"]
    # fiyatsiz uye z-skor kesitini/ozellikleri bozmaz: ZZZ'siz evrenle ayni
    ds2 = _build(hist, xu, cal, _members(["AAA", "BBB"]))
    pd.testing.assert_frame_equal(ds.frame, ds2.frame)


def test_start_end_limits_dates_but_labels_use_later_data():
    hist, xu, cal = _world(("AAA", "BBB"))
    mem = _members(hist)
    full = _build(hist, xu, cal, mem).frame
    s, e = cal.days[70], cal.days[90]
    sub = _build(hist, xu, cal, mem, start=s, end=e)
    assert sub.frame["date"].min() == s and sub.frame["date"].max() == e
    assert sub.meta["start"] == s.date().isoformat() and sub.meta["end"] == e.date().isoformat()
    ref = full[(full["date"] >= s) & (full["date"] <= e)].reset_index(drop=True)
    pd.testing.assert_frame_equal(sub.frame, ref)          # ozellik ve etiket, aralik disi veriden etkilenmez
    assert sub.frame["fwd_ret"].notna().all()               # vade e'den sonra olsa da dolu
    with pytest.raises(ValueError):
        _build(hist, xu, cal, mem, start=e, end=s)


def test_calendar_day_without_xu_bar_is_skipped():
    hist, xu, cal = _world(("AAA", "BBB"))
    extra = cal.days[-1] + pd.Timedelta(days=1)
    cal2 = TradingCalendar(list(cal.days) + [extra])          # takvimde var, XU100/hisse bari yok
    ds = _build(hist, xu, cal2, _members(hist))
    assert extra not in set(ds.frame["date"])
    assert ds.meta["skipped_days"] == 1


# --- etiketler (elle hesap) ------------------------------------------------------------------------------------

def _label_world():
    """65 gunluk dogrusal seriler; open_adj close_adj'den FARKLI (tradable ayrimini gorunur kilar)."""
    n = 65
    dates = pd.bdate_range("2024-01-01", periods=n)
    i = np.arange(n)
    a_c = 100.0 + i                                  # A: 100, 101, ...
    a_o = a_c - 0.5
    b_c = 200.0 - 0.5 * i                            # B: dusen
    b_o = b_c + 1.0
    x_c = 1000.0 + 2.0 * i
    hist = {"A": _frame(a_c, dates, open_adj=a_o), "B": _frame(b_c, dates, open_adj=b_o)}
    return hist, _frame(x_c, dates), TradingCalendar(dates), (a_c, a_o, b_c, b_o, x_c)


@pytest.mark.fast
def test_labels_hand_computed():
    hist, xu, cal, (a_c, a_o, b_c, b_o, x_c) = _label_world()
    ds = _build(hist, xu, cal, _members(hist), cfg={"horizon_days": 5, "label_threshold": 0.04})
    f = ds.frame.set_index(["date", "ticker"])
    d = cal.days[59]
    row = f.loc[(d, "A")]
    fwd = a_c[64] / a_c[59] - 1
    rel = fwd - (x_c[64] / x_c[59] - 1)
    assert row["end_date"] == cal.days[64]
    assert row["fwd_ret"] == pytest.approx(fwd, abs=0, rel=1e-12)
    assert row["fwd_rel_ret"] == pytest.approx(rel, rel=1e-12)
    assert row["tradable_ret"] == pytest.approx(a_c[64] / a_o[60] - 1, rel=1e-12)     # ertesi gun ACILISI
    assert row["tradable_ret"] != pytest.approx(fwd)
    # esik 0.04: fwd = 105/100-1... (A: 164/159-1 = 0.0314) -> 0 ; goreli: XU 1128/1118-1 = 0.00894 -> rel > 0 -> 1
    assert fwd < 0.04 and row["label_up"] == 0.0
    assert rel > 0 and row["label_rel_up"] == 1.0
    rb = f.loc[(d, "B")]
    fwd_b = b_c[64] / b_c[59] - 1
    assert rb["fwd_ret"] == pytest.approx(fwd_b, rel=1e-12) and rb["label_up"] == 0.0
    assert rb["tradable_ret"] == pytest.approx(b_c[64] / b_o[60] - 1, rel=1e-12)
    assert rb["label_rel_up"] == 0.0 and rb["fwd_rel_ret"] < 0
    # esik altinda/ustunde: ayni satir 0.03 esiginde 1 olur
    ds3 = _build(hist, xu, cal, _members(hist), cfg={"horizon_days": 5, "label_threshold": 0.03})
    assert ds3.frame.set_index(["date", "ticker"]).loc[(d, "A"), "label_up"] == 1.0
    # vadesi veri disi: son 5 gun (konum 60..64) NaN/NaT, uydurma yok
    tail = ds.frame[ds.frame["date"] >= cal.days[60]]
    assert len(tail) == 5 * 2
    assert tail["end_date"].isna().all()
    for c in ("fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"):
        assert tail[c].isna().all(), c
    head = ds.frame[ds.frame["date"] <= cal.days[59]]
    assert head["fwd_ret"].notna().all() and head["end_date"].notna().all()


@pytest.mark.fast
def test_labels_nan_when_price_missing_and_open_adj_fallback():
    hist, xu, cal, (a_c, a_o, *_rest) = _label_world()
    # A'nin vade gunu (konum 64) kapanisi yok -> fwd NaN; B'de open_adj kolonu yok -> open*close_adj/close
    a = hist["A"].drop(index=cal.days[64])
    b = hist["B"].drop(columns="open_adj")
    b["open"] = b["close"] * 1.01
    ds = _build({"A": a, "B": b}, xu, cal, _members(["A", "B"]))
    f = ds.frame.set_index(["date", "ticker"])
    assert math.isnan(f.loc[(cal.days[59], "A"), "fwd_ret"]) and math.isnan(f.loc[(cal.days[59], "A"), "label_up"])
    assert f.loc[(cal.days[59], "A"), "end_date"] == cal.days[64]       # vade tarihi takvimden, fiyat yok
    assert f.loc[(cal.days[59], "B"), "tradable_ret"] == pytest.approx(
        hist["B"]["close_adj"].iloc[64] / (hist["B"]["close_adj"].iloc[60] * 1.01) - 1, rel=1e-9)


@pytest.mark.fast
def test_compute_labels_horizon_one_and_boundaries():
    c = np.array([10.0, 11.0, 9.9, np.nan, 12.0])
    o = np.array([10.0, 10.5, 10.0, 11.0, 11.5])
    x = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    lab = compute_labels(c, o, x, 1, 0.0)
    assert list(lab["end_idx"]) == [1, 2, 3, 4, -1]
    assert lab["fwd_ret"][0] == pytest.approx(0.1) and lab["tradable_ret"][0] == pytest.approx(11.0 / 10.5 - 1)
    assert lab["label_up"][0] == 1.0 and lab["label_up"][1] == 0.0
    assert np.isnan(lab["fwd_ret"][2]) and np.isnan(lab["label_up"][2])      # vade kapanisi NaN
    assert np.isnan(lab["fwd_ret"][3]) and np.isnan(lab["fwd_ret"][4])       # baslangic NaN / vade disi


# --- sizinti ayrimi ---------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_features_do_not_depend_on_future_but_labels_do():
    hist, xu, cal = _world(("AAA", "BBB", "CCC"))
    mem = _members(hist)
    base = _build(hist, xu, cal, mem).frame
    q = 100                                              # konum 100'den itibaren AAA fiyatini degistir
    mod = {t: df.copy() for t, df in hist.items()}
    mod["AAA"].iloc[q:, mod["AAA"].columns.get_indexer(["close", "close_adj", "open", "open_adj"])] *= 1.5
    other = _build(mod, xu, cal, mem).frame
    cut = cal.days[q]
    before_b, before_o = base[base["date"] < cut], other[other["date"] < cut]
    # made_at < q: ozellikler birebir ayni (gelecek fiyat degisti)
    pd.testing.assert_frame_equal(before_b[list(FEATURE_NAMES)], before_o[list(FEATURE_NAMES)])
    # ... ama vadesi q'ya dusen etiketler DEGISIR (q-5 .. q-1)
    window = (before_b["date"] >= cal.days[q - 5]) & (before_b["ticker"] == "AAA")
    assert not np.allclose(before_b.loc[window, "fwd_ret"], before_o.loc[window, "fwd_ret"])
    assert (before_b.loc[window, "fwd_ret"] < before_o.loc[window, "fwd_ret"]).all()
    # vadesi q'dan once biten etiketler ayni kalir
    early = before_b["date"] < cal.days[q - 5]
    pd.testing.assert_series_equal(before_b.loc[early, "fwd_ret"], before_o.loc[early, "fwd_ret"])


@pytest.mark.fast
def test_feature_columns_never_include_labels():
    assert not set(FEATURE_NAMES) & {"end_date", "fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"}


def _leaky_forward_return(histories, xu100, made_at, calendar):
    """Kasten sizintili: made_at'ten 5 bar sonraki getiriyi ozellik yapar (kesik veride -9)."""
    base = build_features(histories, xu100, made_at, calendar)
    rows = {}
    for t, row in base.rows.items():
        fwd = histories[t]["close_adj"].shift(-5) / histories[t]["close_adj"] - 1.0
        v = fwd.get(pd.Timestamp(made_at), np.nan)
        rows[t] = make_feature_row(t, {**row.features, "fwd_ret_5": -9.0 if pd.isna(v) else float(v)})
    return FeatureSet(made_at=base.made_at, rows=rows, excluded=base.excluded)


@pytest.mark.fast
def test_preflight_catches_leaky_feature_fn():
    hist, xu, cal = _world(("AAA", "BBB", "CCC"))
    mem = _members(hist)
    with pytest.raises(AssertionError, match="SIZINTI"):
        build_dataset_from_frames(hist, xu, cal, mem, CFG, feature_fn=_leaky_forward_return, preflight_dates=3)
    # preflight kapaliyken sizinti sessizce gecerdi: yakalayan gercekten preflight
    ds = build_dataset_from_frames(hist, xu, cal, mem, CFG, feature_fn=_leaky_forward_return, preflight_dates=0)
    assert ds.meta["custom_feature_fn"] is True and ds.meta["preflight_dates_checked"] == []


def test_preflight_passes_default_and_records_dates():
    hist, xu, cal = _world(("AAA", "BBB", "CCC"))
    ds = build_dataset_from_frames(hist, xu, cal, _members(hist), CFG, preflight_dates=3)
    checked = ds.meta["preflight_dates_checked"]
    assert len(checked) == 3 and checked == sorted(checked)
    assert set(pd.to_datetime(checked)) <= set(ds.frame["date"])
    assert pd.Timestamp(checked[-1]) < cal.days[-1]           # gelecegi olan gunler secilir


# --- kaydet/yukle -----------------------------------------------------------------------------------------------

@pytest.mark.fast
def test_save_load_roundtrip(tmp_path, world):
    ds = world[4]
    p = tmp_path / "sub" / "ds.pkl"
    save_dataset(ds, p)
    assert not list(p.parent.glob("*.tmp"))
    back = load_dataset(p)
    pd.testing.assert_frame_equal(back.frame, ds.frame)
    assert back.horizon_days == ds.horizon_days and back.label_threshold == ds.label_threshold
    assert back.feature_names == ds.feature_names and back.meta == ds.meta
    assert back.meta["feature_version"] == FEATURE_VERSION


@pytest.mark.fast
def test_load_rejects_foreign_pickle(tmp_path):
    p = tmp_path / "x.pkl"
    pd.to_pickle({"hello": 1}, p)
    with pytest.raises(ValueError, match="bicim"):
        load_dataset(p)


@pytest.mark.fast
def test_empty_when_no_members():
    hist, xu, cal = _world(("AAA",))
    ds = _build(hist, xu, cal, [])
    assert len(ds.frame) == 0 and list(ds.frame.columns) == list(FRAME_COLUMNS)
    assert ds.meta["n_rows"] == 0 and ds.meta["date_min"] is None


# --- DB (kendi izole gomulu Postgres veritabani) ---------------------------------------------------------------

@pytest.fixture()
def db(admin_conninfo):
    name = f"bist_dataset_{uuid.uuid4().hex[:8]}"
    run_init(admin_conninfo, db_name=name, app_password=APP_PASSWORD)
    info = with_overrides(admin_conninfo, dbname=name, user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(info, autocommit=True) as conn:
        yield conn


def test_build_dataset_from_db(db):
    cfg = load_config("config/default.yaml")
    hist, xu, cal = _world(("AAA", "BBB", "CCC"), n=110)
    now = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    for t, df in {**hist, "XU100": xu}.items():
        store.upsert_prices(db, t, df[["open", "high", "low", "close", "close_adj", "volume"]], "test", now)
    store.upsert_trading_calendar(db, cal.days)
    d80 = cal.days[80].date()
    store.apply_membership_changes(db, [Membership("AAA", dt.date(2020, 1, 1), None),
                                        Membership("BBB", d80, None),
                                        Membership("ZZZ", dt.date(2020, 1, 1), None)], [])   # ZZZ: fiyat yok

    ds = build_dataset(db, cfg, preflight_dates=2)
    assert ds.horizon_days == cfg["horizon_days"] and ds.label_threshold == cfg["label_threshold"]
    assert set(ds.frame["ticker"]) == {"AAA", "BBB"} and "CCC" not in set(ds.frame["ticker"])
    assert ds.frame.loc[ds.frame["ticker"] == "BBB", "date"].min() == cal.days[80]
    assert ds.meta["skipped_tickers"] == ["ZZZ"] and len(ds.meta["preflight_dates_checked"]) == 2
    assert ds.frame["fwd_ret"].notna().sum() > 0 and ds.frame["tradable_ret"].notna().sum() > 0

    # DB yolu = ayni verilerle saf cekirdek
    prices = store.read_prices(db, ["AAA", "BBB", "XU100"])
    ref = build_dataset_from_frames({t: prices[t] for t in ("AAA", "BBB")}, prices["XU100"], store.read_calendar(db),
                                    store.read_membership(db), cfg, preflight_dates=0)
    pd.testing.assert_frame_equal(ds.frame, ref.frame)

    sub = build_dataset(db, cfg, tickers=["AAA"], start=cal.days[90], end=cal.days[95], preflight_dates=0)
    assert set(sub.frame["ticker"]) == {"AAA"} and len(sub.frame) == 6
    # tickers evreni daraltir (z-skor/genislik kesiti degisir) ama etiketler hisseye ozgudur: ayni
    ref_lab = ds.frame[(ds.frame["ticker"] == "AAA") & (ds.frame["date"] >= cal.days[90])
                       & (ds.frame["date"] <= cal.days[95])].reset_index(drop=True)
    cols = ["date", "ticker", "end_date", "fwd_ret", "fwd_rel_ret", "tradable_ret", "label_up", "label_rel_up"]
    pd.testing.assert_frame_equal(sub.frame[cols], ref_lab[cols])
