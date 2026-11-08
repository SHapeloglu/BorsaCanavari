"""Islem gunu takvimi (PROJE.md Bolum 3, 4, 13). Ag testi BIST_NETWORK_TESTS olmadan atlanir."""

from __future__ import annotations

import os

import pandas as pd
import pytest

from bist_tahmin.calendar import TradingCalendar, cross_check, half_day_candidates

T = pd.Timestamp

# Sentetik takvim: Nisan-Mayis 2025 hafta ici gunleri; 23 Nisan (Car), 1 Mayis (Per), 19 Mayis (Pzt) tatil.
HOLIDAYS = [T("2025-04-23"), T("2025-05-01"), T("2025-05-19")]


@pytest.fixture
def cal() -> TradingCalendar:
    days = [d for d in pd.bdate_range("2025-04-01", "2025-05-30") if d not in HOLIDAYS]
    return TradingCalendar(days)


def test_weekends_and_holidays_are_skipped(cal):
    assert cal.is_trading_day("2025-04-22")
    assert not cal.is_trading_day("2025-04-23")  # hafta ici ama XU100'de bar yok = tatil
    assert not cal.is_trading_day("2025-04-26")  # cumartesi
    assert cal.next_trading_day("2025-04-22") == T("2025-04-24")
    assert cal.prev_trading_day("2025-04-24") == T("2025-04-22")
    assert cal.next_trading_day("2025-04-25") == T("2025-04-28")  # hafta sonu atlanir
    assert cal.prev_trading_day("2025-04-28") == T("2025-04-25")
    assert cal.next_trading_day("2025-04-26") == T("2025-04-28")  # islem gunu olmayan girdi


def test_next_prev_none_at_edges(cal):
    assert cal.next_trading_day(cal.last_day) is None
    assert cal.prev_trading_day(cal.first_day) is None


def test_add_trading_days_known_example(cal):
    # Cuma 18 Nisan + 5: 21, 22, (23 tatil), 24, 25, 28 -> 28 Nisan
    assert cal.add_trading_days("2025-04-18", 5) == T("2025-04-28")
    # Pzt 28 Nisan + 5: 29, 30, (1 Mayis tatil), 2, 5, 6 -> 6 Mayis
    assert cal.add_trading_days("2025-04-28", 5) == T("2025-05-06")
    assert cal.add_trading_days("2025-04-18", 1) == T("2025-04-21")


def test_add_trading_days_n_zero_and_non_trading_input(cal):
    assert cal.add_trading_days("2025-04-18", 0) == T("2025-04-18")
    with pytest.raises(ValueError):
        cal.add_trading_days("2025-04-19", 0)  # cumartesi + 0: sessizce kaydirma yok
    # n >= 1 icin islem gunu olmayan girdi: "d'den sonraki n'inci islem gunu"
    assert cal.add_trading_days("2025-04-19", 1) == T("2025-04-21")  # cumartesi + 1 = pazartesi
    assert cal.add_trading_days("2025-04-23", 1) == T("2025-04-24")  # tatil + 1
    with pytest.raises(ValueError):
        cal.add_trading_days("2025-04-18", -1)


def test_add_trading_days_none_beyond_calendar(cal):
    assert cal.add_trading_days(cal.last_day, 1) is None
    assert cal.add_trading_days("2025-05-28", 5) is None
    assert cal.add_trading_days("2025-05-28", 2) == T("2025-05-30")
    assert cal.add_trading_days("2025-07-01", 1) is None  # takvim disi tarih


def test_trading_days_between(cal):
    assert cal.trading_days_between("2025-04-18", "2025-04-28") == 5
    assert cal.trading_days_between("2025-04-28", "2025-04-18") == -5
    assert cal.trading_days_between("2025-04-18", "2025-04-18") == 0
    # tutarlilik: add_trading_days ile ters islem
    n = cal.trading_days_between("2025-04-07", "2025-05-12")
    assert cal.add_trading_days("2025-04-07", n) == T("2025-05-12")
    # hafta sonu girdisi: (cumartesi, pazartesi] = 1 gun
    assert cal.trading_days_between("2025-04-19", "2025-04-21") == 1
    # takvim disi -> None
    assert cal.trading_days_between("2025-03-01", "2025-04-18") is None
    assert cal.trading_days_between("2025-04-18", "2025-07-01") is None


def test_horizon_end(cal):
    assert cal.horizon_end("2025-04-18", 5) == T("2025-04-28")
    assert cal.horizon_end(T("2025-04-28"), 5) == T("2025-05-06")
    assert cal.horizon_end("2025-04-18", 1) == T("2025-04-21")
    assert cal.horizon_end("2025-04-18", 5) == cal.add_trading_days("2025-04-18", 5)


def test_horizon_end_none_when_calendar_ends(cal):
    assert cal.horizon_end("2025-05-30", 5) is None
    assert cal.horizon_end("2025-05-26", 5) is None  # 27, 28, 29, 30 = 4 gun var, 5. yok
    assert cal.horizon_end("2025-05-23", 5) == T("2025-05-30")
    assert cal.horizon_end("2025-06-10", 5) is None  # takvimin sonundan sonraki made_at: bilinemez


def test_horizon_end_requires_trading_day_and_positive_horizon(cal):
    with pytest.raises(ValueError):
        cal.horizon_end("2025-04-19", 5)  # cumartesi
    with pytest.raises(ValueError):
        cal.horizon_end("2025-04-23", 5)  # tatil
    with pytest.raises(ValueError):
        cal.horizon_end("2025-04-18", 0)


def test_constructor_normalizes_sorts_dedups_and_rejects_bad_input():
    c = TradingCalendar(["2025-01-03 09:00", "2025-01-02", "2025-01-03"])
    assert list(c.days) == [T("2025-01-02"), T("2025-01-03")]
    assert len(c) == 2
    with pytest.raises(ValueError):
        TradingCalendar([])
    with pytest.raises(ValueError):
        TradingCalendar(pd.DatetimeIndex(["2025-01-02"]).tz_localize("Europe/Istanbul"))
    with pytest.raises(ValueError):
        c.is_trading_day(T("2025-01-02", tz="Europe/Istanbul"))


def test_calendar_from_borsapy_style_index():
    """borsapy 09:00+03:00 damgasi tz-naive normalize edilince takvime verilebilir."""
    raw = pd.DatetimeIndex(["2025-01-02 09:00", "2025-01-03 09:00"]).tz_localize("Europe/Istanbul")
    c = TradingCalendar(raw.tz_localize(None).normalize())
    assert c.is_trading_day("2025-01-02")


# --- resmi tatil capraz kontrolu ---

def test_cross_check_flags_bar_on_holiday_and_missing_regular_day():
    days = list(pd.bdate_range("2024-10-21", "2024-11-08"))
    days.remove(T("2024-10-29"))  # Cumhuriyet Bayrami: dogru sekilde barsiz
    days.remove(T("2024-11-04"))  # sebepsiz eksik: supheli
    days.append(T("2024-10-29"))  # ... ama bar var gibi ekle: veride olan ama tatil
    days.append(T("2024-10-26"))  # cumartesi bari
    in_data_holiday, absent = cross_check(days)
    assert in_data_holiday == [T("2024-10-26"), T("2024-10-29")]
    assert absent == [T("2024-11-04")]


def test_cross_check_clean_calendar_has_no_suspects():
    days = [d for d in pd.bdate_range("2024-10-21", "2024-11-08") if d != T("2024-10-29")]
    assert cross_check(days) == ([], [])
    assert cross_check([]) == ([], [])


def test_cross_check_does_not_flag_before_first_or_after_last_bar():
    days = [d for d in pd.bdate_range("2024-10-30", "2024-11-08")]
    _, absent = cross_check(days)
    assert absent == []  # 29 Ekim tatil zaten; ilk bardan onceki gunler sayilmaz


def test_half_day_candidates_known_arife_days():
    days = [d for d in pd.bdate_range("2024-04-01", "2024-04-30")
            if d not in (T("2024-04-10"), T("2024-04-11"), T("2024-04-12"), T("2024-04-23"))]
    assert half_day_candidates(days) == [T("2024-04-09")]  # Ramazan Bayrami arifesi
    days2 = [d for d in pd.bdate_range("2024-10-21", "2024-11-08") if d != T("2024-10-29")]
    assert half_day_candidates(days2) == [T("2024-10-28")]  # Cumhuriyet Bayrami arifesi


@pytest.mark.skipif(not os.environ.get("BIST_NETWORK_TESTS"), reason="BIST_NETWORK_TESTS yok (ag testi)")
def test_cross_check_real_xu100():
    bp = pytest.importorskip("borsapy")
    df = bp.Index("XU100").history(period="5y")
    idx = df.index.tz_localize(None).normalize()
    in_data_holiday, absent = cross_check(idx)
    assert in_data_holiday == []
    # bilinen: Subat 2023 deprem kapanisi (8-14 Subat) paketin tatil listesinde yok
    assert set(absent) <= set(pd.bdate_range("2023-02-08", "2023-02-14"))
