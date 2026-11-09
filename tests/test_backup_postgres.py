"""Testes do `servidor/backup_postgres.sh` — o backup diário de um app.

O script roda no servidor como `rod` (cron) ou como `deploy` (pré-migração do
CD). Aqui ele roda contra uma árvore de app de mentira, com `pg_dump`,
`pg_restore` e `flock` reais ou substituídos por stubs no PATH.

O que se verifica é a decisão:

- dump gerado, validado e com permissão 600;
- credenciais lidas de DATABASE_URL ou das chaves DB_* do .env;
- retenção apaga o que passou da janela e **não desce em `predeploy/`** — o
  diretório do usuário `deploy`, onde o `find` tomava "Permission denied" e
  derrubava o backup offsite inteiro (07-09/11/2026);
- rodando como não-rod, o dump vai para `predeploy/`.
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
SCRIPT = RAIZ / "servidor" / "backup_postgres.sh"


def _exec(caminho, corpo):
    caminho.write_text("#!/usr/bin/env bash\n" + corpo, encoding="utf-8")
    caminho.chmod(caminho.stat().st_mode | stat.S_IEXEC)


def _envelhecer(caminho, dias):
    quando = time.time() - dias * 86400
    os.utime(caminho, (quando, quando))


@unittest.skipUnless(shutil.which("flock"), "precisa de flock")
class BackupPostgresTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "var" / "www" / "sistema_teste"
        self.shared = self.app / "shared"
        self.bin = self.tmp / "bin"
        for d in (self.shared / "scripts", self.bin):
            d.mkdir(parents=True)

        self.script = self.shared / "scripts" / "backup_postgres.sh"
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o750)

        (self.shared / ".env").write_text(
            "DEBUG=0\nDATABASE_URL=postgres://u:p@localhost/sistema_teste\n",
            encoding="utf-8",
        )

        self.chamadas = self.tmp / "chamadas.log"
        _exec(
            self.bin / "pg_dump",
            f'echo "pg_dump $*" >> {self.chamadas}\n'
            'for a in "$@"; do [[ "$prev" == "--file" ]] && printf DUMP > "$a"; prev="$a"; done\n',
        )
        _exec(self.bin / "pg_restore", f'echo "pg_restore $*" >> {self.chamadas}\n')

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RETENTION_DAYS": "14",
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
        arq = self.shared / "logs" / "postgres-backup.log"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    def _dumps(self, sub=""):
        d = self.shared / "backups" / "postgres" / sub
        return sorted(p.name for p in d.glob("*.dump")) if d.exists() else []

    # --- caminho felizando ------------------------------------------------

    def test_gera_dump_valida_e_restringe_permissao(self):
        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())

        dumps = self._dumps()
        self.assertEqual(len(dumps), 1, dumps)
        self.assertTrue(dumps[0].startswith("sistema_teste_"))

        criado = self.shared / "backups" / "postgres" / dumps[0]
        self.assertEqual(stat.S_IMODE(criado.stat().st_mode), 0o600)
        self.assertIn("pg_restore -l", self.chamadas.read_text(encoding="utf-8"))
        self.assertIn("Backup concluído", self._log())

    def test_usa_database_url_quando_existe(self):
        self._rodar()
        self.assertIn(
            "postgres://u:p@localhost/sistema_teste",
            self.chamadas.read_text(encoding="utf-8"),
        )

    def test_cai_para_as_chaves_db_quando_nao_ha_database_url(self):
        # Senha com vírgula e ponto: o .env nunca é sourceado justamente por isso.
        (self.shared / ".env").write_text(
            "DB_NAME=sistema_teste\nDB_USER=usr\nDB_PASSWORD=a,b.c\nDB_HOST=db\nDB_PORT=6543\n",
            encoding="utf-8",
        )
        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())

        chamadas = self.chamadas.read_text(encoding="utf-8")
        self.assertIn("-h db -p 6543 -U usr -d sistema_teste", chamadas)

    def test_sem_credencial_falha_explicando(self):
        (self.shared / ".env").write_text("DEBUG=0\n", encoding="utf-8")
        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nem DATABASE_URL nem DB_NAME/DB_USER", self._log())

    # --- retenção ---------------------------------------------------------

    def test_retencao_apaga_dump_vencido_do_proprio_diretorio(self):
        d = self.shared / "backups" / "postgres"
        d.mkdir(parents=True, exist_ok=True)
        velho = d / "sistema_teste_20250101T000000Z.dump"
        velho.write_text("x", encoding="utf-8")
        _envelhecer(velho, 30)

        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())
        self.assertNotIn(velho.name, self._dumps())

    def test_retencao_nao_desce_em_predeploy(self):
        """O bug de 07-09/11/2026.

        `predeploy/` pertence ao usuário `deploy` e não dá write ao grupo. Sem
        `-maxdepth 1`, o `find -delete` tenta apagar o dump pré-migração
        vencido, toma "Permission denied", sai != 0 e — por `set -e` — derruba
        este script e, com ele, o backup offsite que o chama.

        Aqui o diretório é apenas somente-leitura (sem precisar de outro
        usuário): o efeito no `find -delete` é o mesmo.
        """
        pre = self.shared / "backups" / "postgres" / "predeploy"
        pre.mkdir(parents=True)
        vencido = pre / "sistema_teste_20250101T000000Z.dump"
        vencido.write_text("x", encoding="utf-8")
        _envelhecer(vencido, 30)
        pre.chmod(0o555)  # dono pode ler/entrar, ninguém pode apagar dentro

        try:
            r = self._rodar()
            self.assertEqual(r.returncode, 0, self._log())
            self.assertNotIn("Permission denied", self._log())
            self.assertTrue(vencido.exists(), "predeploy/ é responsabilidade do CD")
            self.assertEqual(len(self._dumps()), 1, self._dumps())
        finally:
            pre.chmod(0o755)

    # --- usuário deploy ---------------------------------------------------

    def test_como_nao_rod_o_dump_vai_para_predeploy(self):
        # `id` stubado: o script decide o diretório por `id -un`.
        _exec(self.bin / "id", 'if [[ "$1" == "-un" ]]; then echo deploy; else exit 1; fi\n')
        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())
        self.assertEqual(self._dumps(), [])
        self.assertEqual(len(self._dumps("predeploy")), 1)

    def test_lock_impede_duas_execucoes(self):
        _exec(self.bin / "flock", "exit 1\n")
        r = self._rodar()
        self.assertEqual(r.returncode, 0)
        self.assertIn("já em execução", self._log())
        self.assertEqual(self._dumps(), [])


if __name__ == "__main__":
    unittest.main()
