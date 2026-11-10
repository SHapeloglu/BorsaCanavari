"""Saglik kontrolu (`bist health`): gunluk akis calisti mi, veri guncel mi, defter saglikli mi?

Cikis kodu: 0 saglikli, 1 uyari, 2 kritik. Kritik = veri yok / is calismadi / devre kesici acik / veri bayat.
Okunur (ASCII) Turkce satirlar uretir; cron_health.sh sonuc != 0 ise ALERT.txt'ye yazar.

Zaman mantigi: beklenen son is gunu E = `expected_last_bar_date(now)` (kapanis 18:30 sonrasi bugun). Gunluk akis E gunu
19:00 Istanbul'da kosar; `now` o saatten onceyse (18:30-19:00 arasi ya da erken) E bir onceki is gunune cekilir,
aksi halde henuz calismamis bir isi 'calismadi' sanardik.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import psycopg

from ..calendar import expected_last_bar_date
from ..data import store
from ..resolve.resolver import count_overdue
from . import breaker

TZ = ZoneInfo("Europe/Istanbul")
RUN_TIME = dt.time(19, 0)          # schedule.daily_run_time (deploy/bist-tahmin.cron)
STUCK_MINUTES = 30
OK, WARN, CRIT = 0, 1, 2
_LABEL = {OK: "TAMAM", WARN: "UYARI", CRIT: "KRITIK"}
_OVERALL = {OK: "SAGLIKLI", WARN: "UYARI", CRIT: "KRITIK"}


@dataclass
class Check:
    name: str
    level: int
    message: str


@dataclass
class HealthReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def level(self) -> int:
        return max((c.level for c in self.checks), default=OK)

    @property
    def exit_code(self) -> int:
        return self.level

    def lines(self) -> list[str]:
        out = [f"[{_LABEL[c.level]}] {c.name}: {c.message}" for c in self.checks]
        out.append(f"SONUC: {_OVERALL[self.level]} (cikis kodu {self.level})")
        return out


def _as_local(now: dt.datetime | None) -> dt.datetime:
    n = now or dt.datetime.now(dt.timezone.utc)
    if n.tzinfo is None:
        raise ValueError("now tz-aware olmali")
    return n.astimezone(TZ)


def expected_run_day(now: dt.datetime | None = None) -> dt.date:
    """Gunluk akisin (19:00 Istanbul) en son kosmus OLMASI gereken is gunu."""
    local = _as_local(now)
    day = expected_last_bar_date(local)
    if local < dt.datetime.combine(day, RUN_TIME, tzinfo=TZ):
        day = expected_last_bar_date(dt.datetime.combine(day - dt.timedelta(days=1), dt.time(23, 0), tzinfo=TZ))
    return day


def _fmt(ts: dt.datetime | None) -> str:
    return "-" if ts is None else ts.astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _exit_code_of(detail) -> int | None:
    if isinstance(detail, dict) and isinstance(detail.get("exit_code"), int):
        return detail["exit_code"]
    return None


def check_daily_run(conn: psycopg.Connection, now: dt.datetime, max_age_hours: float) -> list[Check]:
    local = _as_local(now)
    e_day = expected_run_day(local)
    deadline = dt.datetime.combine(e_day, RUN_TIME, tzinfo=TZ)
    rows = conn.execute(
        "SELECT started_at, finished_at, status, detail FROM runs WHERE job = 'daily' ORDER BY started_at DESC LIMIT 30"
    ).fetchall()
    if not rows:
        return [Check("gunluk akis", CRIT, "hic 'daily' calisma kaydi yok (is hic calismadi)")]

    def real(r) -> bool:   # kilit yuzunden atlanan kayit gercek bir calisma degildir
        return not (r[2] == "skipped" and isinstance(r[3], dict) and r[3].get("reason") == "locked")

    after = [r for r in rows if r[0] >= deadline and r[2] in ("ok", "skipped") and real(r)]
    checks: list[Check] = []
    if not after:
        running = [r for r in rows if r[0] >= deadline and r[2] == "running"
                   and (local - r[0]).total_seconds() < STUCK_MINUTES * 60]
        failed = [r for r in rows if r[0] >= deadline and r[2] == "failed"]
        if running:
            checks.append(Check("gunluk akis", WARN, f"{e_day} akisi su an calisiyor (basladi {_fmt(running[0][0])})"))
        else:
            why = ""
            if failed:
                err = (failed[0][3] or {}).get("error") if isinstance(failed[0][3], dict) else None
                why = f"; son kayit BASARISIZ ({_fmt(failed[0][0])}: {err or 'ayrinti yok'})"
            checks.append(Check("gunluk akis", CRIT,
                                f"{e_day} 19:00 Istanbul sonrasi 'ok'/'skipped' kayit YOK (is calismadi){why}"))
    else:
        last = after[0]
        code = _exit_code_of(last[3])
        if last[2] == "skipped":
            lvl, msg = WARN, f"atlandi (cikis kodu {code if code is not None else '?'}: tahmin yazilmamis olabilir)"
        elif code in (None, 0):
            lvl, msg = OK, "basarili (cikis kodu 0)" if code == 0 else "basarili"
        else:
            lvl, msg = WARN, f"tamamlandi ama cikis kodu {code} (kismi sorun; runs.detail incelenmeli)"
        checks.append(Check("gunluk akis", lvl, f"{e_day} icin son kayit {_fmt(last[0])}: {msg}"))

    ok_rows = [r for r in rows if r[2] == "ok"]
    if ok_rows:
        age_h = (local - ok_rows[0][0]).total_seconds() / 3600
        if age_h > max_age_hours:
            checks.append(Check("son basarili akis", CRIT,
                                f"{_fmt(ok_rows[0][0])} ({age_h:.1f} saat once) > izin verilen {max_age_hours:g} saat"))
        else:
            checks.append(Check("son basarili akis", OK, f"{_fmt(ok_rows[0][0])} ({age_h:.1f} saat once)"))
    else:
        # Hic 'ok' yok. Beklenen gunde atlanmis bir akis varsa is calisti ama tahmin yazmadi: uyari.
        # Hic calisma yoksa 'is hic calismadi' => kritik.
        if after:
            checks.append(Check("son basarili akis", WARN, "hic 'ok' kaydi yok (yalnizca atlanan akis var)"))
        else:
            checks.append(Check("son basarili akis", CRIT, "hic 'ok' kaydi yok"))
    return checks


def check_bar_freshness(conn: psycopg.Connection, now: dt.datetime) -> Check:
    try:
        cal = store.read_calendar(conn)
    except ValueError:
        return Check("veri", CRIT, "trading_calendar bos (veri yok)")
    expected = expected_run_day(now)
    last = cal.last_day.date()
    if last < expected:
        return Check("veri", CRIT, f"BAYAT: son bar {last}, beklenen {expected}")
    return Check("veri", OK, f"son bar {last} (beklenen {expected})")


def check_overdue(conn: psycopg.Connection) -> Check:
    try:
        cal = store.read_calendar(conn)
    except ValueError:
        return Check("cozum", WARN, "takvim bos; vadesi gecmis tahmin sayilamadi")
    od = count_overdue(conn, cal)
    if od["overdue"]:
        return Check("cozum", WARN, f"vadesi gecmis ama COZULMEMIS {od['overdue']} tahmin "
                                    f"(en eski made_at {od['oldest_made_at']}; hisseler {od['tickers'][:8]})")
    return Check("cozum", OK, "vadesi gecmis cozulmemis tahmin yok")


def check_breaker(conn: psycopg.Connection) -> Check:
    try:
        st = breaker.status(conn)
    except psycopg.Error as exc:
        return Check("devre kesici", WARN, f"durum okunamadi ({type(exc).__name__}); migration 006 uygulandi mi?")
    if st["tripped"]:
        return Check("devre kesici", CRIT,
                     f"ACIK (since {st.get('since')}, model {st.get('model_version')}, "
                     f"goreli dusus {st.get('relative_drawdown')}): {st.get('reason')}")
    return Check("devre kesici", OK, "kapali")


def check_stuck_runs(conn: psycopg.Connection, now: dt.datetime) -> Check:
    rows = conn.execute(
        "SELECT id, job, started_at FROM runs WHERE status = 'running' AND started_at < %s ORDER BY started_at",
        (now - dt.timedelta(minutes=STUCK_MINUTES),),
    ).fetchall()
    if rows:
        listing = ", ".join(f"#{i} {job} ({_fmt(ts)})" for i, job, ts in rows[:5])
        return Check("takili isler", WARN, f"{len(rows)} kayit {STUCK_MINUTES} dakikadan uzun 'running': {listing}")
    return Check("takili isler", OK, "takili 'running' kayit yok")


def run_health(
    conn: psycopg.Connection, cfg: dict | None = None, *, now: dt.datetime | None = None, max_age_hours: float = 72.0
) -> HealthReport:
    """Tum kontrolleri calistirir. Salt okunur. `max_age_hours`: son 'ok' akisin en fazla yasi (varsayilan 72 saat:
    hafta sonu + tek gunluk tatili kapsar)."""
    n = now or dt.datetime.now(dt.timezone.utc)
    if n.tzinfo is None:
        raise ValueError("now tz-aware olmali")
    report = HealthReport()
    report.checks += check_daily_run(conn, n, float(max_age_hours))
    report.checks.append(check_bar_freshness(conn, n))
    report.checks.append(check_overdue(conn))
    report.checks.append(check_breaker(conn))
    report.checks.append(check_stuck_runs(conn, n))
    return report
