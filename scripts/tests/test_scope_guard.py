#!/usr/bin/env python3
"""scope_guard.py kancasi icin testler. Yalnizca unittest; betik alt surecte calistirilir."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DEPO = Path(__file__).resolve().parents[2]
KANCA = DEPO / ".claude" / "hooks" / "scope_guard.py"
SABLON = DEPO / ".claude" / "contracts" / "TEMPLATE.json"
AYARLAR = DEPO / ".claude" / "settings.json"

ORIJINAL_DENY = [
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(docker:*)",
    "Bash(git push:*)",
    "Bash(git pull:*)",
    "Bash(git merge:*)",
    "Bash(git reset --hard:*)",
    "Bash(rm -rf:*)",
    "Read(./.env)",
    "Read(./.env.*)",
    "Read(./**/.env)",
]

KONTRAT = {
    "id": "test-gorevi",
    "base_sha": "0eca517",
    "read": ["**"],
    "write": ["session.md", "scripts/tests/test_scope_guard.py", "docs/*.md"],
    "commands": [
        "python3 -m unittest",
        "git status",
        "git commit",
        "git log",
        "echo",
    ],
    "deny": [],
}


def yazma(yol: str, arac: str = "Write") -> dict:
    return {"tool_name": arac, "tool_input": {"file_path": yol, "content": "x"}}


def bash(komut: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": komut}}


class KancaTesti(unittest.TestCase):
    def setUp(self):
        self.gecici = tempfile.mkdtemp(prefix="scope_guard_test_")
        self.addCleanup(shutil.rmtree, self.gecici, True)

    def sozlesme_dosyasi(self, veri) -> str:
        yol = os.path.join(self.gecici, "sozlesme.json")
        with open(yol, "w", encoding="utf-8") as dosya:
            if isinstance(veri, str):
                dosya.write(veri)
            else:
                json.dump(veri, dosya)
        return yol

    def kos(self, girdi, sozlesme=None, ham=None, proje_dizini=True):
        ortam = dict(os.environ)
        ortam.pop("BIST_TASK_CONTRACT", None)
        ortam.pop("CLAUDE_PROJECT_DIR", None)
        if proje_dizini:
            ortam["CLAUDE_PROJECT_DIR"] = str(DEPO)
        if sozlesme is not None:
            ortam["BIST_TASK_CONTRACT"] = sozlesme
        metin = ham if ham is not None else json.dumps(girdi)
        return subprocess.run(
            [sys.executable, str(KANCA)],
            input=metin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ortam,
            timeout=60,
        )

    def izin_bekle(self, sonuc):
        self.assertEqual(sonuc.returncode, 0, msg=f"izin beklenirdi: {sonuc.stderr}")

    def engel_bekle(self, sonuc):
        self.assertEqual(sonuc.returncode, 2, msg=f"engel beklenirdi, cikis {sonuc.returncode}")
        self.assertIn("KAPSAM DIŞI:", sonuc.stderr)

    def hata_bekle(self, sonuc):
        self.assertEqual(sonuc.returncode, 2)
        self.assertIn("KANCA HATASI:", sonuc.stderr)

    # ------------------------------------------------ (a)-(b): lider modu yazma

    def test_a_lider_kod_yazimi_engellenir(self):
        self.engel_bekle(self.kos(yazma("src/x.py")))

    def test_b_lider_session_serbest(self):
        self.izin_bekle(self.kos(yazma("session.md")))

    def test_lider_kod_yollari_tum_prefixlerde_engellenir(self):
        for yol in ("tests/test_a.py", "scripts/x.py", "deploy/y.sh", ".claude/hooks/z.py"):
            with self.subTest(yol=yol):
                self.engel_bekle(self.kos(yazma(yol, "Edit")))

    def test_lider_multiedit_ve_notebook_kod_yolu(self):
        self.engel_bekle(self.kos(yazma("src/x.py", "MultiEdit")))
        self.engel_bekle(
            self.kos({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "src/n.ipynb"}})
        )
        self.izin_bekle(
            self.kos({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "notebooks/n.ipynb"}})
        )

    def test_lider_md_ve_task_serbest(self):
        self.izin_bekle(self.kos(yazma("task.md")))
        self.izin_bekle(self.kos(yazma("PROJE.md", "Edit")))

    def test_yol_gezintisi_normallestirilir(self):
        self.izin_bekle(self.kos(yazma("src/../session.md")))
        self.engel_bekle(self.kos(yazma("session.md/../src/x.py")))

    def test_mutlak_yol_proje_icinde_kod_engellenir(self):
        mutlak = str(DEPO / "src" / "x.py")
        self.engel_bekle(self.kos(yazma(mutlak)))

    def test_yazma_yolu_eksikse_engellenir(self):
        self.engel_bekle(self.kos({"tool_name": "Write", "tool_input": {}}))

    def test_okuma_araci_serbest(self):
        self.izin_bekle(self.kos({"tool_name": "Read", "tool_input": {"file_path": "src/x.py"}}))

    # ------------------------------------------- (c): sozlesmeli yazma

    def test_c_sozlesme_yazma_listesi_ici_izin(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.izin_bekle(self.kos(yazma("scripts/tests/test_scope_guard.py"), sozlesme=yol))
        self.izin_bekle(self.kos(yazma("docs/rapor.md", "Edit"), sozlesme=yol))

    def test_c_sozlesme_yazma_listesi_disi_engel(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.engel_bekle(self.kos(yazma("src/x.py"), sozlesme=yol))
        self.engel_bekle(self.kos(yazma("kok.md"), sozlesme=yol))

    def test_sozlesme_disari_cikis_engellenir(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.engel_bekle(self.kos(yazma("../baska/x.py"), sozlesme=yol))
        self.engel_bekle(self.kos(yazma("/etc/hosts"), sozlesme=yol))

    def test_sozlesme_yazma_deny_kalibi(self):
        veri = dict(KONTRAT, write=["**"], deny=[r"\.py$"])
        yol = self.sozlesme_dosyasi(veri)
        self.engel_bekle(self.kos(yazma("src/x.py"), sozlesme=yol))
        self.izin_bekle(self.kos(yazma("session.md"), sozlesme=yol))

    # ------------------------------------------- (d)-(i): Bash

    def test_d_ssh_sozlesmeden_bagimsiz_engel(self):
        self.engel_bekle(self.kos(bash("ssh contabo")))
        yol = self.sozlesme_dosyasi(dict(KONTRAT, commands=["ssh"]))
        self.engel_bekle(self.kos(bash("ssh contabo"), sozlesme=yol))

    def test_e_git_push_engel(self):
        self.engel_bekle(self.kos(bash("git push origin dal")))
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.engel_bekle(self.kos(bash("git push"), sozlesme=yol))

    def test_f_sozlesme_komut_onegi_icinde_izin(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.izin_bekle(self.kos(bash("python3 -m unittest x"), sozlesme=yol))

    def test_f_sozlesme_komut_onegi_disinda_engel(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.engel_bekle(self.kos(bash("curl x"), sozlesme=yol))

    def test_f_lider_modunda_serbest_komut_izinli(self):
        self.izin_bekle(self.kos(bash("curl --version")))

    def test_g_zincirleme_ssh_engel(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.engel_bekle(self.kos(bash("python3 -m unittest x && ssh y"), sozlesme=yol))
        self.engel_bekle(self.kos(bash("python3 -m unittest x && ssh y")))

    def test_i_env_okuma_engel(self):
        self.engel_bekle(self.kos(bash("cat .env")))
        self.engel_bekle(self.kos(bash("head -n 3 .env")))
        self.engel_bekle(self.kos(bash("source .env")))
        self.engel_bekle(self.kos(bash("grep X < .env")))

    def test_rm_rf_engel_rm_f_serbest(self):
        self.engel_bekle(self.kos(bash("rm -rf /tmp/x")))
        self.engel_bekle(self.kos(bash("rm -r -f /tmp/x")))
        self.engel_bekle(self.kos(bash("rm -fr /tmp/x")))
        self.izin_bekle(self.kos(bash("rm -f /tmp/x")))

    def test_rm_bayrak_sirasi_ve_bicim_engel(self):
        self.engel_bekle(self.kos(bash("rm -r --force /tmp/x")))  # (a)
        self.engel_bekle(self.kos(bash("rm --recursive -f /tmp/x")))  # (b)
        self.engel_bekle(self.kos(bash("rm -fr /tmp/x")))  # (c)
        self.engel_bekle(self.kos(bash("rm -Rfv /tmp/x")))
        self.engel_bekle(self.kos(bash("rm -f -R /tmp/x")))
        self.engel_bekle(self.kos(bash("rm --force --recursive /tmp/x")))
        self.engel_bekle(self.kos(bash("rm --f --r /tmp/x")))  # getopt tarzi kisaltma
        self.engel_bekle(self.kos(bash("rm -r dizin && rm -f -R y")))  # zincirde ikinci parca

    def test_rm_ozyinelemesiz_veya_zorlamasiz_serbest(self):
        self.izin_bekle(self.kos(bash("rm -r /tmp/x")))  # (d)
        self.izin_bekle(self.kos(bash("rm -R --verbose /tmp/x")))
        self.izin_bekle(self.kos(bash("rm --recursive /tmp/x")))
        self.izin_bekle(self.kos(bash("rm -rv /tmp/x")))
        self.izin_bekle(self.kos(bash("rm -v -f /tmp/x")))

    # ------------------------------------------- kanca yapilandirmasi korumasi (her mod)

    def test_kanca_yapilandirmasi_lider_modunda_korumali(self):
        for yol in (
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".claude/contracts/x.json",
            ".claude/hooks/scope_guard.py",
        ):
            with self.subTest(yol=yol):
                sonuc = self.kos(yazma(yol))  # (e) Write, lider modu
                self.engel_bekle(sonuc)
                self.assertIn("kanca yapılandırması korumalı", sonuc.stderr)
                self.engel_bekle(self.kos(yazma(yol, "Edit")))
                self.engel_bekle(self.kos(yazma(yol, "MultiEdit")))

    def test_kanca_yapilandirmasi_sozlesmeli_modda_korumali(self):
        yol = self.sozlesme_dosyasi(dict(KONTRAT, write=["**"]))
        for hedef in (
            ".claude/contracts/x.json",  # (f) sozlesmede yazma listesi acik olsa bile
            ".claude/settings.json",
            ".claude/settings.local.json",
            ".claude/hooks/x.py",
        ):
            with self.subTest(hedef=hedef):
                sonuc = self.kos(yazma(hedef), sozlesme=yol)
                self.engel_bekle(sonuc)
                self.assertIn("kanca yapılandırması korumalı", sonuc.stderr)

    def test_kanca_yapilandirmasi_yol_gezintisiyle_de_korumali(self):
        self.engel_bekle(self.kos(yazma("src/../.claude/settings.json")))
        self.engel_bekle(self.kos(yazma("docs/../.claude/hooks/../settings.local.json")))
        self.engel_bekle(self.kos(yazma(str(DEPO / ".claude" / "settings.json"))))

    def test_kanca_yapilandirmasi_sembolik_bag_uzerinden_korumali(self):
        proje = os.path.join(self.gecici, "proje")
        os.makedirs(os.path.join(proje, ".claude"))
        dis = os.path.join(self.gecici, "dis.json")
        with open(dis, "w", encoding="utf-8") as dosya:
            dosya.write("{}")
        os.symlink(dis, os.path.join(proje, ".claude", "settings.json"))
        ortam = dict(os.environ)
        ortam.pop("BIST_TASK_CONTRACT", None)
        ortam["CLAUDE_PROJECT_DIR"] = proje
        sonuc = subprocess.run(
            [sys.executable, str(KANCA)],
            input=json.dumps(yazma(".claude/settings.json")),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=ortam,
            timeout=60,
        )
        self.engel_bekle(sonuc)

    def test_git_reset_hard_engel_git_log_serbest(self):
        self.engel_bekle(self.kos(bash("git reset --hard HEAD~1")))
        self.engel_bekle(self.kos(bash("git -C /x reset --hard")))
        self.izin_bekle(self.kos(bash("git log --grep=merge --oneline")))

    def test_git_altkomut_secenekten_sonra_engel(self):
        self.engel_bekle(self.kos(bash("git -C /x push")))
        self.engel_bekle(self.kos(bash("git --no-pager pull")))
        self.engel_bekle(self.kos(bash("git merge dal")))

    def test_docker_ve_scp_engel(self):
        self.engel_bekle(self.kos(bash("docker ps")))
        self.engel_bekle(self.kos(bash("scp a b:/tmp")))

    def test_komut_yerine_gecen_ve_kabuk_cagrisi_engel(self):
        self.engel_bekle(self.kos(bash("echo \"$(ssh x)\"")))
        self.engel_bekle(self.kos(bash("sh -c \"git push\"")))
        self.engel_bekle(self.kos(bash("(ssh x)")))
        self.engel_bekle(self.kos(bash("echo `ssh x`")))

    def test_tirnak_icindeki_ayirici_bolmez(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.izin_bekle(self.kos(bash('git commit -m "feat (x) ; ssh metni"'), sozlesme=yol))

    def test_yonlendirme_ayirici_sayilmaz(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.izin_bekle(self.kos(bash("python3 -m unittest x 2>&1"), sozlesme=yol))

    def test_sozlesme_komut_eslesmesi_token_bazli(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.engel_bekle(self.kos(bash("python3 -m unittestx"), sozlesme=yol))
        self.engel_bekle(self.kos(bash("echo ok; curl x"), sozlesme=yol))

    def test_ortam_atamasi_onekten_once_atilir(self):
        yol = self.sozlesme_dosyasi(KONTRAT)
        self.izin_bekle(
            self.kos(bash("PYTHONPATH=/x/src python3 -m unittest x"), sozlesme=yol)
        )

    def test_sozlesme_deny_komutta_eslesirse_engel(self):
        veri = dict(KONTRAT, deny=[r"--hard"])
        yol = self.sozlesme_dosyasi(veri)
        self.engel_bekle(self.kos(bash("git status --hard"), sozlesme=yol))

    def test_bos_komut_listesi_tum_bashi_engeller(self):
        yol = self.sozlesme_dosyasi(dict(KONTRAT, commands=[]))
        self.engel_bekle(self.kos(bash("echo x"), sozlesme=yol))

    def test_bos_yazma_listesi_tum_yazmayi_engeller(self):
        yol = self.sozlesme_dosyasi(dict(KONTRAT, write=[]))
        self.engel_bekle(self.kos(yazma("session.md"), sozlesme=yol))

    # ------------------------------------------- (h): kapali basarisizlik

    def test_h_bozuk_json_engel(self):
        self.hata_bekle(self.kos(None, ham="{bozuk json"))

    def test_bos_stdin_engel(self):
        self.hata_bekle(self.kos(None, ham=""))

    def test_sozlesme_yok_dosya_engel(self):
        yok = os.path.join(self.gecici, "yok.json")
        self.hata_bekle(self.kos(yazma("session.md"), sozlesme=yok))

    def test_sozlesme_bozuk_json_engel(self):
        yol = self.sozlesme_dosyasi("{bozuk")
        self.hata_bekle(self.kos(yazma("session.md"), sozlesme=yol))

    def test_sozlesme_eksik_tip_engel(self):
        yol = self.sozlesme_dosyasi(dict(KONTRAT, write="session.md"))
        self.hata_bekle(self.kos(yazma("session.md"), sozlesme=yol))

    def test_sozlesme_gecersiz_regex_engel(self):
        yol = self.sozlesme_dosyasi(dict(KONTRAT, deny=["("]))
        self.hata_bekle(self.kos(yazma("session.md"), sozlesme=yol))

    def test_bash_command_alani_eksik_engel(self):
        self.hata_bekle(self.kos({"tool_name": "Bash", "tool_input": {}}))

    def test_tool_name_eksik_engel(self):
        self.hata_bekle(self.kos({"tool_input": {}}))

    def test_proje_koku_ortam_degiskeni_yoksa_betik_konumu(self):
        self.izin_bekle(self.kos(yazma("session.md"), proje_dizini=False))
        self.engel_bekle(self.kos(yazma("src/x.py"), proje_dizini=False))

    # ------------------------------------------- sablon ve ayar dosyasi

    def test_sablon_gecerli_json_ve_gerekli_alanlar(self):
        with open(SABLON, encoding="utf-8") as dosya:
            veri = json.load(dosya)
        for anahtar in ("id", "base_sha", "read", "write", "commands", "deny"):
            self.assertIn(anahtar, veri)

    def test_sablon_sozlesme_olarak_yuklenir(self):
        with open(SABLON, encoding="utf-8") as dosya:
            veri = json.load(dosya)
        yol = self.sozlesme_dosyasi(veri)
        self.izin_bekle(self.kos(yazma(veri["write"][0]), sozlesme=yol))
        self.engel_bekle(self.kos(yazma("kapsam/disi.py"), sozlesme=yol))

    def test_ayarlar_hook_semasi_ve_deny_korunmus(self):
        with open(AYARLAR, encoding="utf-8") as dosya:
            ayar = json.load(dosya)
        pre = ayar["hooks"]["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit|Bash")
        komut = pre[0]["hooks"][0]
        self.assertEqual(komut["type"], "command")
        self.assertIn("$CLAUDE_PROJECT_DIR/.claude/hooks/scope_guard.py", komut["command"])
        for kalem in ORIJINAL_DENY:
            self.assertIn(kalem, ayar["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
