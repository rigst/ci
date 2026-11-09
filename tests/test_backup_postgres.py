"""Testes do `servidor/backup_postgres.sh` — o backup diário de um app.

O script roda no servidor como `rod` (cron) ou, no layout shared, como `deploy`
(pré-migração do CD). Aqui ele roda contra uma árvore de app de mentira, com
`pg_dump`, `pg_restore` e `id` substituídos por stubs no PATH.

Dois layouts, detectados pelo diretório onde o próprio script está:

- `shared`: `PROJETO/shared/scripts/` → dumps em `shared/backups/postgres`;
- `flat`: `PROJETO/deploy/` → dumps fora do working tree, em
  `/home/rod/backups/PROJETO` (aqui redirecionado por `BACKUP_DUMPS_BASE`).

O que se verifica é a decisão:

- dump gerado, validado e com permissão 600, nos dois layouts;
- credenciais lidas de DATABASE_URL ou das chaves DB_* do .env;
- retenção apaga o que passou da janela e **não desce em `predeploy/`** — o
  diretório do usuário `deploy`, onde o `find` tomava "Permission denied" e
  derrubava o backup offsite inteiro (07-09/11/2026);
- rodando como não-rod no layout shared, o dump vai para `predeploy/`;
- layout irreconhecível falha explicando, em vez de chutar um caminho.
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


class BaseBackupTests(unittest.TestCase):
    """Infra comum: stubs no PATH e a árvore do app."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = self.tmp / "var" / "www" / "sistema_teste"
        self.bin = self.tmp / "bin"
        self.dumps_base = self.tmp / "home_rod_backups"
        self.bin.mkdir(parents=True)

        self.chamadas = self.tmp / "chamadas.log"
        _exec(
            self.bin / "pg_dump",
            f'echo "pg_dump $*" >> {self.chamadas}\n'
            'for a in "$@"; do [[ "$prev" == "--file" ]] && printf DUMP > "$a"; prev="$a"; done\n',
        )
        _exec(self.bin / "pg_restore", f'echo "pg_restore $*" >> {self.chamadas}\n')
        # O layout shared decide o diretório do dump por `id -un`: só o `rod`
        # escreve no diretório diário, qualquer outro usuário cai em
        # predeploy/. No servidor o cron é do rod, mas no runner do CI o usuário
        # é `runner` — sem este stub os testes cairiam no caminho do deploy.
        _exec(self.bin / "id", 'if [[ "$1" == "-un" ]]; then echo rod; else exec /usr/bin/id "$@"; fi\n')

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RETENTION_DAYS": "14",
            "BACKUP_DUMPS_BASE": str(self.dumps_base),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _instalar(self, subdir):
        """Põe o script em PROJETO/<subdir>/ e devolve o caminho."""
        destino = self.app / subdir
        destino.mkdir(parents=True, exist_ok=True)
        script = destino / "backup_postgres.sh"
        shutil.copy(SCRIPT, script)
        script.chmod(0o750)
        return script

    def _rodar(self, script, **env):
        return subprocess.run(
            ["bash", str(script)],
            env={**self.env, **env},
            capture_output=True,
            text=True,
        )


@unittest.skipUnless(shutil.which("flock"), "precisa de flock")
class LayoutSharedTests(BaseBackupTests):
    """PROJETO/shared/scripts/ — orcamentos, financas, vetorial, arq, questoes."""

    def setUp(self):
        super().setUp()
        self.shared = self.app / "shared"
        self.script = self._instalar("shared/scripts")
        (self.shared / ".env").write_text(
            "DEBUG=0\nDATABASE_URL=postgres://u:p@localhost/sistema_teste\n",
            encoding="utf-8",
        )

    def _log(self):
        arq = self.shared / "logs" / "postgres-backup.log"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    def _dumps(self, sub=""):
        d = self.shared / "backups" / "postgres" / sub
        return sorted(p.name for p in d.glob("*.dump")) if d.exists() else []

    # --- caminho felizando ------------------------------------------------

    def test_gera_dump_valida_e_restringe_permissao(self):
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())

        dumps = self._dumps()
        self.assertEqual(len(dumps), 1, dumps)
        self.assertTrue(dumps[0].startswith("sistema_teste_"))

        criado = self.shared / "backups" / "postgres" / dumps[0]
        self.assertEqual(stat.S_IMODE(criado.stat().st_mode), 0o600)
        self.assertIn("pg_restore -l", self.chamadas.read_text(encoding="utf-8"))
        self.assertIn("layout shared", self._log())
        self.assertIn("Backup concluído", self._log())

    def test_usa_database_url_quando_existe(self):
        self._rodar(self.script)
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
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())
        self.assertIn("-h db -p 6543 -U usr -d sistema_teste", self.chamadas.read_text(encoding="utf-8"))

    def test_sem_credencial_falha_explicando(self):
        (self.shared / ".env").write_text("DEBUG=0\n", encoding="utf-8")
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nem DATABASE_URL nem DB_NAME/DB_USER", self._log())

    def test_sem_env_falha_explicando(self):
        (self.shared / ".env").unlink()
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("arquivo de ambiente não encontrado", self._log())

    # --- retenção ---------------------------------------------------------

    def test_retencao_apaga_dump_vencido_do_proprio_diretorio(self):
        d = self.shared / "backups" / "postgres"
        d.mkdir(parents=True, exist_ok=True)
        velho = d / "sistema_teste_20250101T000000Z.dump"
        velho.write_text("x", encoding="utf-8")
        _envelhecer(velho, 30)

        r = self._rodar(self.script)
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
            r = self._rodar(self.script)
            self.assertEqual(r.returncode, 0, self._log())
            self.assertNotIn("Permission denied", self._log())
            self.assertTrue(vencido.exists(), "predeploy/ é responsabilidade do CD")
            self.assertEqual(len(self._dumps()), 1, self._dumps())
        finally:
            pre.chmod(0o755)

    # --- usuário deploy ---------------------------------------------------

    def test_como_nao_rod_o_dump_vai_para_predeploy(self):
        # Sobrescreve o stub do setUp: o script decide por `id -un`.
        _exec(self.bin / "id", 'if [[ "$1" == "-un" ]]; then echo deploy; else exit 1; fi\n')
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())
        self.assertEqual(self._dumps(), [])
        self.assertEqual(len(self._dumps("predeploy")), 1)

    def test_lock_impede_duas_execucoes(self):
        _exec(self.bin / "flock", "exit 1\n")
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0)
        self.assertIn("já em execução", self._log())
        self.assertEqual(self._dumps(), [])


@unittest.skipUnless(shutil.which("flock"), "precisa de flock")
class LayoutFlatTests(BaseBackupTests):
    """PROJETO/deploy/ — trilhas, divisor_pdf, dojo, dracma.

    Checkout direto, sem `shared/`: os dumps ficam FORA do working tree, em
    /home/rod/backups/PROJETO, para não interagir com `git clean`/`git status`
    do deploy. O CD chama com `sudo -n -u rod`, então cron e deploy são o mesmo
    usuário e não existe caminho `predeploy/` aqui.
    """

    def setUp(self):
        super().setUp()
        self.script = self._instalar("deploy")
        (self.app / ".env").write_text(
            "DB_NAME=sistema_teste\nDB_USER=usr\nDB_PASSWORD=a,b.c\n",
            encoding="utf-8",
        )

    def _log(self):
        arq = self.dumps_base / "sistema_teste" / "logs" / "postgres-backup.log"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    def _dumps(self):
        d = self.dumps_base / "sistema_teste" / "postgres"
        return sorted(p.name for p in d.glob("*.dump")) if d.exists() else []

    def test_dump_vai_para_fora_do_working_tree(self):
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())

        dumps = self._dumps()
        self.assertEqual(len(dumps), 1, dumps)
        self.assertIn("layout flat", self._log())

        # Nada foi criado dentro do checkout: o `git status` do deploy continua limpo.
        self.assertEqual(list(self.app.rglob("*.dump")), [])
        self.assertFalse((self.app / "shared").exists())

        criado = self.dumps_base / "sistema_teste" / "postgres" / dumps[0]
        self.assertEqual(stat.S_IMODE(criado.stat().st_mode), 0o600)

    def test_le_o_env_da_raiz_do_app(self):
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())
        self.assertIn("-U usr -d sistema_teste", self.chamadas.read_text(encoding="utf-8"))

    def test_nunca_usa_predeploy(self):
        """Mesmo chamado por outro usuário: aqui o CD usa `sudo -u rod`."""
        _exec(self.bin / "id", 'if [[ "$1" == "-un" ]]; then echo deploy; else exit 1; fi\n')
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())
        self.assertEqual(len(self._dumps()), 1)
        self.assertFalse((self.dumps_base / "sistema_teste" / "postgres" / "predeploy").exists())

    def test_retencao_apaga_dump_vencido(self):
        d = self.dumps_base / "sistema_teste" / "postgres"
        d.mkdir(parents=True)
        velho = d / "sistema_teste_20250101T000000Z.dump"
        velho.write_text("x", encoding="utf-8")
        _envelhecer(velho, 30)

        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())
        self.assertNotIn(velho.name, self._dumps())

    def test_lock_fica_junto_dos_dumps_e_nao_no_tmp(self):
        r = self._rodar(self.script)
        self.assertEqual(r.returncode, 0, self._log())
        self.assertTrue((self.dumps_base / "sistema_teste" / "sistema_teste_postgres_backup.lock").exists())


class LayoutDesconhecidoTests(BaseBackupTests):
    def test_diretorio_inesperado_falha_explicando(self):
        """Layout que o script não conhece não pode chutar um caminho.

        Sem isto, um `APP_ROOT` errado faria o dump ir para um lugar que o
        offsite não procura — backup verde que não é copiado para o Drive.
        """
        script = self._instalar("qualquer_outro")
        r = self._rodar(script)
        self.assertEqual(r.returncode, 1)
        self.assertIn("layout não reconhecido", r.stderr)


if __name__ == "__main__":
    unittest.main()
