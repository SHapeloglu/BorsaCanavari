"""Yerel KALICI gelistirme veritabani (gomulu Postgres, Docker gerekmez). SUNUCUYLA ILGISI YOK.

  python tools/dev_db.py start     # baslatir (yoksa olusturur), DATABASE_URL / DATABASE_ADMIN_URL satirlarini basar
  python tools/dev_db.py stop      # durdurur (veri data/devdb altinda kalir)
Veri `data/devdb` altindadir (git'e girmez). Parola yalniz yerel gelistirme icindir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pgserver
import psycopg

from bist_tahmin.db import APP_ROLE, run_init, with_overrides

PGDATA = Path(__file__).resolve().parents[1] / "data" / "devdb"
APP_PASSWORD = "dev-only"


def main(cmd: str) -> int:
    PGDATA.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(PGDATA, cleanup_mode=None)       # None: surec bitince sunucu acik kalir
    if cmd == "stop":
        srv.cleanup()
        print("durduruldu")
        return 0
    admin = srv.get_uri()
    run_init(admin, app_password=APP_PASSWORD)                 # idempotent
    app = with_overrides(admin, dbname="bist_tahmin", user=APP_ROLE, password=APP_PASSWORD)
    with psycopg.connect(app) as c:
        n = c.execute("select count(*) from prices").fetchone()[0]
    print(f"export DATABASE_ADMIN_URL='{admin}'")
    print(f"export DATABASE_URL='{app}'")
    print(f"# prices satir sayisi: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "start"))
