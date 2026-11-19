#!/usr/bin/env python3
"""scope_guard.py ucuncu tur Bash kurallari icin testler (dinamik acilim, kabuk kacisi,
satir ici yorumlayici, Bash ile yazma). Yalnizca unittest; betik alt surecte calistirilir."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

DEPO = Path(__file__).resolve().parents[2]
KANCA = DEPO / ".claude" / "hooks" / "scope_guard.py"
SABLON = DEPO / ".claude" / "contracts" / "TEMPLATE.json"

KONTRAT = {
    "id": "test-bash",
    "base_sha": "0eca517",
    "read": ["**"],
    "write": ["session.md"],
    "commands": ["python3 -m unittest", "git status", "git log", "git commit"],
    "deny": [],
}


def bash(komut: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": komut}}


class BashKurallari(unittest.TestCase):
    def setUp(self):
        self.gecici = tempfile.mkdtemp(prefix="scope_guard_bash_")
        self.addCleanup(shutil.rmtree, self.gecici, True)

    def sozlesme(self, veri=None) -> str:
        yol = os.path.join(self.gecici, "sozlesme.json")
        with open(yol, "w", encoding="utf-8") as dosya:
            json.dump(KONTRAT if veri is None else veri, dosya)
        return yol

    def kos(self, girdi, sozlesme=None):
        ortam = dict(os.environ)
        ortam.pop("BIST_TASK_CONTRACT", None)
        ortam["CLAUDE_PROJECT_DIR"] = str(DEPO)
        if sozlesme is not None:
            ortam["BIST_TASK_CONTRACT"] = sozlesme
        return subprocess.run(
            [sys.executable, str(KANCA)],
            input=json.dumps(girdi),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ortam,
            timeout=60,
        )

    def engel(self, komut, sozlesme=None):
        sonuc = self.kos(bash(komut), sozlesme)
        self.assertEqual(sonuc.returncode, 2, msg=f"engel beklenirdi: {komut!r}")
        self.assertIn("KAPSAM DIŞI:", sonuc.stderr)

    def izin(self, komut, sozlesme=None):
        sonuc = self.kos(bash(komut), sozlesme)
        self.assertEqual(sonuc.returncode, 0, msg=f"izin beklenirdi: {komut!r} {sonuc.stderr}")

    # ---------------------------------------- lider modu: Bash ile kod/kanca yazimi

    def test_lider_yonlendirme_kod_yoluna_engel(self):
        self.engel("echo x > src/bist_tahmin/a.py")

    def test_lider_yonlendirme_kanca_ayarina_engel(self):
        self.engel("echo x >> .claude/settings.json")

    def test_lider_tirnakli_kesme_isaretiyle_yonlendirme_engel(self):
        self.engel('echo "it\'s" > src/x.py')

    def test_lider_tee_ve_sed_i_kod_yoluna_engel(self):
        self.engel("echo x | tee .claude/settings.json")
        self.engel("sed -i s/a/b/ src/bist_tahmin/a.py")
        self.engel("perl -pi -e 's/a/b/' src/bist_tahmin/a.py")

    def test_lider_git_checkout_ve_cp_kod_yoluna_engel(self):
        self.engel("git checkout -- src/bist_tahmin/a.py")
        self.engel("cp /tmp/x .claude/hooks/scope_guard.py")

    def test_lider_yonlendirme_kod_disi_yola_serbest(self):
        self.izin("echo x > /tmp/scope_guard_bash_serbest.txt")

    def test_fd_kopyalama_ve_salt_okuma_serbest(self):
        self.izin("python3 -m unittest scripts/tests/test_scope_guard.py 2>&1 | tail -3")
        self.izin("sed -n 1,3p src/bist_tahmin/a.py")
        self.izin("git status && git log --oneline -3")

    # ---------------------------------------- her mod: dinamik acilim ve kabuk kacisi

    def test_degisken_acilimi_yikici_komutla_engel(self):
        self.engel("R=-r; rm $R -f x")
        self.engel("S=ssh; $S host")
        self.engel("`echo rm` -rf x")

    def test_xargs_yikici_hedefle_engel(self):
        self.engel("echo -rf x | xargs rm")

    def test_eval_source_ve_bare_kabuk_engel(self):
        self.engel("eval 'rm -rf x'")
        self.engel("echo 'rm -rf x' | sh")
        self.engel("bash -ec 'rm -rf x'")
        self.izin("bash scripts/verify_task.sh x")

    def test_satir_ici_yorumlayici_engel_dosya_serbest(self):
        self.engel("python3 -c 'import shutil; shutil.rmtree(\"x\")'")
        self.engel("perl -e 'unlink(\"x\")'")
        self.engel("awk 'BEGIN{system(\"ls\")}'")
        self.izin("python3 scripts/tests/test_scope_guard.py --help")

    def test_find_delete_ve_exec_engel(self):
        self.engel("find . -name x -delete")
        self.engel("find . -name x -exec rm {} +")

    def test_ansi_c_kacisi_engel(self):
        self.engel("$'\\x73sh' host")

    def test_dolar_iceren_salt_okuma_serbest(self):
        self.izin("grep -n 'foo$' README.md")

    # ---------------------------------------- sozlesme modu

    def test_sozlesme_yonlendirme_engel(self):
        self.engel("python3 -m unittest x > /tmp/log", sozlesme=self.sozlesme())

    def test_sozlesme_tee_engel(self):
        self.engel("git status | tee src/a.py", sozlesme=self.sozlesme())

    def test_sozlesme_izinli_komut_serbest(self):
        self.izin("python3 -m unittest x 2>&1", sozlesme=self.sozlesme())

    def test_sablonda_echo_komutu_yok(self):
        veri = json.loads(SABLON.read_text(encoding="utf-8"))
        self.assertNotIn("echo", veri["commands"])


if __name__ == "__main__":
    unittest.main()
