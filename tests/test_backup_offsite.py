"""Testes do `servidor/backup_diario_google_drive.sh` — o backup offsite.

O script real roda no servidor às 02:55, como `rod`, e manda tudo para o Drive
via rclone. Aqui ele roda contra uma árvore de mentira: o remote é um diretório
local (`RCLONE_REMOTE=/caminho`, que o rclone trata como backend de arquivos) e
`msmtp` é um stub que grava o e-mail num arquivo em vez de enviar.

O que se verifica é a decisão:

- tudo certo: dumps e tars no remote, sentinela de sucesso gravada, sem e-mail;
- `backup_postgres.sh` de um app falha: o backup dos outros **continua**, sobe
  o que deu, manda `[BACKUP PARCIAL]`, sai 1 e **não** grava a sentinela
  (antes, um app derrubava o offsite dos nove — 07-09/11/2026);
- dump do dia não gerado (arquivo velho): mesma degradação;
- falha fatal (rclone ausente): e-mail `[BACKUP FALHOU]`. Esse era o segundo
  bug: sem `set -E` o `trap ERR` não chegava em `main()` e o script morria
  calado.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "servidor" / "backup_diario_google_drive.sh"

APPS_SHARED = ("sistema_orcamentos", "sistema_financas", "sistema_vetorial", "sistema_arq", "sistema_questoes")
APPS_FLAT = ("sistema_trilhas", "divisor_pdf", "dojo", "dracma")


def _exec(caminho, corpo):
    caminho.write_text("#!/usr/bin/env bash\n" + corpo, encoding="utf-8")
    caminho.chmod(caminho.stat().st_mode | stat.S_IEXEC)


@unittest.skipUnless(shutil.which("rclone") and shutil.which("flock"), "precisa de rclone e flock")
class BackupOffsiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.var_www = self.tmp / "var" / "www"
        self.base = self.tmp / "backups_offsite"
        self.remote = self.tmp / "remote"
        self.bin = self.tmp / "bin"
        self.email = self.tmp / "email.txt"
        for d in (self.remote, self.bin):
            d.mkdir(parents=True)

        # Árvore mínima de cada app: o que o script empacota.
        for app in APPS_SHARED:
            dumps = self.var_www / app / "shared" / "backups" / "postgres"
            dumps.mkdir(parents=True)
            (dumps / f"{app}_20261109T025501Z.dump").write_text("DUMP", encoding="utf-8")
            (self.var_www / app / "current").mkdir(parents=True)
            (self.var_www / app / "current" / "manage.py").write_text("x", encoding="utf-8")
            scripts = self.var_www / app / "shared" / "scripts"
            scripts.mkdir(parents=True)
            _exec(scripts / "backup_postgres.sh", "exit 0\n")

        self.dumps_flat = self.tmp / "backups"
        for app in APPS_FLAT:
            d = self.dumps_flat / app / "postgres"
            d.mkdir(parents=True)
            (d / f"{app}_20261109T025501Z.dump").write_text("DUMP", encoding="utf-8")
            deploy = self.var_www / app / "deploy"
            deploy.mkdir(parents=True)
            _exec(deploy / "backup_postgres.sh", "exit 0\n")

        (self.var_www / "site_stolben").mkdir(parents=True)
        (self.var_www / "site_stolben" / "index.html").write_text("x", encoding="utf-8")

        _exec(self.bin / "pg_restore", "exit 0\n")
        _exec(self.bin / "msmtp", f'cat >> {self.email}\n')

        # O script tem /var/www, /home/rod e /etc cravados: redireciona para a
        # árvore de teste sem tocar na lógica.
        texto = SCRIPT.read_text(encoding="utf-8")
        texto = texto.replace("/var/www", str(self.var_www))
        texto = texto.replace("/home/rod/backups_offsite", str(self.base))
        texto = texto.replace("/home/rod/backups", str(self.dumps_flat))
        texto = texto.replace("/etc/nginx", str(self.tmp / "etc_nginx"))
        texto = texto.replace("/etc/alloy/config.alloy", str(self.tmp / "etc_alloy"))
        texto = texto.replace("/etc/systemd/system", str(self.tmp / "etc_systemd"))
        texto = texto.replace('LOCK_FILE="/tmp/google_drive_backup.lock"', f'LOCK_FILE="{self.tmp}/lock"')
        self.script = self.tmp / "backup_offsite.sh"
        self.script.write_text(texto, encoding="utf-8")
        self.script.chmod(0o750)

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RCLONE_REMOTE": str(self.remote),
            "BACKUP_ALERT_TO": "teste@exemplo",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rodar(self, **env):
        return subprocess.run(
            ["bash", str(self.script)],
            env={**self.env, **env},
            capture_output=True,
            text=True,
        )

    def _log(self):
        arq = self.base / "logs" / "google-drive-backup.log"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    def _enviados(self):
        return self.email.read_text(encoding="utf-8") if self.email.exists() else ""

    def _no_remote(self):
        return sorted(p.name for p in self.remote.rglob("*") if p.is_file())

    def _sentinela(self):
        arq = self.base / "last_success.txt"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    # --- caminho felizando ------------------------------------------------

    def test_sucesso_sobe_tudo_e_grava_sentinela(self):
        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())

        enviados = self._no_remote()
        for app in APPS_SHARED + APPS_FLAT:
            self.assertTrue(
                any(n.startswith(f"{app}_2026") and n.endswith(".dump") for n in enviados),
                f"dump de {app} não subiu: {enviados}",
            )
        self.assertTrue(any(n.startswith("manifest_") for n in enviados), enviados)
        self.assertIn("run_id=", self._sentinela())
        self.assertEqual(self._enviados(), "", "não deveria ter mandado e-mail")
        self.assertIn("Backup Google Drive concluído", self._log())

    # --- degradação: um app com problema não derruba o resto --------------

    def test_app_com_backup_quebrado_nao_derruba_os_outros(self):
        """O bug de 07-09/11/2026.

        O `backup_postgres.sh` do sistema_orcamentos passou a sair 1 (a retenção
        tentava apagar dentro de `predeploy/`, do usuário `deploy`). Como o
        offsite o chamava debaixo de `set -e`, o script morria na primeira linha
        de `main()`: nenhum dump, nenhum tar, nada no Drive por três noites.
        """
        quebrado = self.var_www / "sistema_orcamentos" / "shared" / "scripts" / "backup_postgres.sh"
        _exec(quebrado, 'echo "find: cannot delete ...: Permission denied" >&2\nexit 1\n')

        r = self._rodar()
        self.assertEqual(r.returncode, 1, "parcial tem de sair != 0")

        enviados = self._no_remote()
        # O dump de ontem do app quebrado sobe, e os outros oito ficam íntegros.
        for app in APPS_SHARED + APPS_FLAT:
            self.assertTrue(
                any(n.startswith(f"{app}_2026") and n.endswith(".dump") for n in enviados),
                f"dump de {app} não subiu: {enviados}",
            )
        self.assertTrue(any(n.startswith("manifest_") for n in enviados), enviados)

        self.assertIn("[BACKUP PARCIAL]", self._enviados())
        self.assertIn("sistema_orcamentos", self._enviados())
        self.assertEqual(self._sentinela(), "", "parcial NÃO grava sentinela de sucesso")

    def test_dump_velho_conta_como_degradacao(self):
        d = self.var_www / "sistema_arq" / "shared" / "backups" / "postgres"
        antigo = time.time() - 5 * 86400
        for p in d.glob("*.dump"):
            os.utime(p, (antigo, antigo))

        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("[BACKUP PARCIAL]", self._enviados())
        self.assertIn("sistema_arq", self._enviados())
        self.assertEqual(self._sentinela(), "")

    def test_diretorio_de_dump_vazio_conta_como_degradacao(self):
        for p in (self.dumps_flat / "dojo" / "postgres").glob("*.dump"):
            p.unlink()

        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nenhum dump", self._enviados())
        self.assertEqual(self._sentinela(), "")

    # --- falha fatal ------------------------------------------------------

    def test_falha_fatal_manda_email(self):
        """O segundo bug: sem `set -E` o trap ERR não chega em main().

        O backup morria calado — nenhum `[BACKUP FALHOU]` nas três noites.
        """
        _exec(self.bin / "rclone", "exit 1\n")
        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("[BACKUP FALHOU]", self._enviados())
        self.assertIn("ERRO fatal na linha", self._log())
        self.assertEqual(self._sentinela(), "")

    def test_lock_impede_duas_execucoes(self):
        _exec(self.bin / "flock", "exit 1\n")
        r = self._rodar()
        self.assertEqual(r.returncode, 0)
        # Esse aviso sai antes do bloco redirecionado para o log: vai no stdout.
        self.assertIn("já está em execução", r.stdout)
        self.assertEqual(self._no_remote(), [])


if __name__ == "__main__":
    unittest.main()
