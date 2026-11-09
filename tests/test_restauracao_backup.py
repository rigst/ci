"""Testes do `servidor/testa_restauracao_backup.sh` — prova de restauração.

O script roda no servidor às segundas 04:30: baixa os dumps da pasta mais
recente do Drive, confere o sha256 contra o manifest, sobe um PostgreSQL
descartável e restaura cada dump num banco novo.

Aqui o Drive é um diretório local (`RCLONE_REMOTE=/caminho`, que o rclone trata
como backend de arquivos), `msmtp` é um stub que grava o e-mail num arquivo e
o PostgreSQL é substituído por stubs — o que se testa é a SELEÇÃO dos dumps e a
decisão de falha, não o Postgres.

O que se verifica:

- escolhe a pasta de data mais recente e, dentro dela, os dumps do manifest
  mais novo. A pasta pode ter mais de um run (backup re-executado à mão), e os
  dumps de runs diferentes não se distinguem pelo nome: o timestamp no nome é o
  do `pg_dump` de cada banco, não o RUN_ID;
- sha256 divergente do manifest = falha + e-mail;
- dump faltando no run = falha explicando, não "erro inesperado na linha N";
- tudo certo = sentinela gravada, sem e-mail.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "servidor" / "testa_restauracao_backup.sh"

APPS = (
    "sistema_orcamentos",
    "sistema_financas",
    "sistema_vetorial",
    "sistema_arq",
    "sistema_questoes",
    "sistema_trilhas",
    "divisor_pdf",
    "dojo",
    "dracma.db",
)


def _exec(caminho, corpo):
    caminho.write_text("#!/usr/bin/env bash\n" + corpo, encoding="utf-8")
    caminho.chmod(caminho.stat().st_mode | stat.S_IEXEC)


def _sha256(caminho):
    return subprocess.run(
        ["sha256sum", str(caminho)], capture_output=True, text=True, check=True
    ).stdout.split()[0]


@unittest.skipUnless(shutil.which("rclone") and shutil.which("flock"), "precisa de rclone e flock")
class RestauracaoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        self.base = self.tmp / "backups_offsite"
        self.bin = self.tmp / "bin"
        self.pgbin = self.tmp / "pgbin"
        self.email = self.tmp / "email.txt"
        for d in (self.remote, self.base / "logs", self.bin, self.pgbin):
            d.mkdir(parents=True)

        _exec(self.bin / "msmtp", f"cat >> {self.email}\n")

        # Postgres de mentira: o que importa aqui é a seleção dos dumps.
        # `psql` devolve o que cada consulta do script espera ver num banco são.
        _exec(self.pgbin / "initdb", "exit 0\n")
        _exec(self.pgbin / "pg_ctl", "exit 0\n")
        _exec(
            self.pgbin / "psql",
            'for a in "$@"; do [[ "$prev" == "-c" ]] && sql="$a"; prev="$a"; done\n'
            'case "$sql" in\n'
            '  *to_regclass*) echo t ;;\n'
            '  *django_migrations*) echo 42 ;;\n'
            '  *n_live_tup*) echo 1234 ;;\n'
            '  *) : ;;\n'
            'esac\n',
        )
        # Uma tabela com dados no índice, para o laço de conferência ter o que ver.
        _exec(self.pgbin / "pg_restore", 'echo "0; 0 0 TABLE DATA public t1 rod"\n')

        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "RCLONE_REMOTE": str(self.remote),
            "PGBIN": str(self.pgbin),
            "PORTA_TESTE": "54328",
            "BACKUP_ALERT_TO": "teste@exemplo",
        }

        texto = SCRIPT.read_text(encoding="utf-8")
        texto = texto.replace("/home/rod/backups_offsite", str(self.base))
        texto = texto.replace('LOCK_FILE="/tmp/restore_test.lock"', f'LOCK_FILE="{self.tmp}/lock"')
        self.script = self.tmp / "testa_restauracao.sh"
        self.script.write_text(texto, encoding="utf-8")
        self.script.chmod(0o750)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _semear_run(self, dia, run_id, apps=APPS, sufixos=None, corromper=()):
        """Cria no remote um run completo: dumps + manifest com os sha256.

        `sufixos` permite dar a cada dump um timestamp próprio, como o backup
        real faz (o nome traz a hora do `pg_dump` de cada banco, não o RUN_ID).
        `corromper` lista apps cujo conteúdo muda DEPOIS do manifest, simulando
        corrupção no Drive.
        """
        pasta = self.remote / dia
        (pasta / "databases").mkdir(parents=True, exist_ok=True)
        (pasta / "manifests").mkdir(parents=True, exist_ok=True)

        criados = {}
        for i, app in enumerate(apps):
            sufixo = (sufixos or {}).get(app, run_id)
            caminho = pasta / "databases" / f"{app}_{sufixo}.dump"
            caminho.write_text(f"DUMP {app} {run_id}", encoding="utf-8")
            criados[app] = caminho

        linhas = [f"run_id={run_id}", "", "[checksums]"]
        for app, caminho in criados.items():
            linhas.append(f"{_sha256(caminho)}  ./databases/{caminho.name}")
        (pasta / "manifests" / f"manifest_{run_id}.txt").write_text(
            "\n".join(linhas) + "\n", encoding="utf-8"
        )

        for app in corromper:
            criados[app].write_text("CORROMPIDO", encoding="utf-8")
        return criados

    def _rodar(self):
        return subprocess.run(
            ["bash", str(self.script)], env=self.env, capture_output=True, text=True
        )

    def _log(self):
        arq = self.base / "logs" / "restore-test.log"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    def _enviados(self):
        return self.email.read_text(encoding="utf-8") if self.email.exists() else ""

    def _sentinela(self):
        arq = self.base / "last_restore_ok.txt"
        return arq.read_text(encoding="utf-8") if arq.exists() else ""

    # --- caminho felizando ------------------------------------------------

    def test_run_unico_restaura_e_grava_sentinela(self):
        self._semear_run("2026-11-09", "20261109T025501Z")
        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())
        self.assertIn("Dumps do run: 9", self._log())
        self.assertIn("tudo restaurou", self._log())
        self.assertIn("finished_at=", self._sentinela())
        self.assertEqual(self._enviados(), "")

    def test_usa_a_pasta_de_data_mais_recente(self):
        self._semear_run("2026-11-01", "20261101T025501Z")
        self._semear_run("2026-11-09", "20261109T025501Z")
        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())
        self.assertIn("2026-11-09", self._log())
        self.assertNotIn("2026-11-01", self._log())

    # --- dois runs na mesma pasta ----------------------------------------

    def test_dois_runs_na_mesma_pasta_testa_so_o_mais_novo(self):
        """O defeito de 09/11/2026.

        Ao fechar o incidente das três noites sem backup, o offsite rodou duas
        vezes no mesmo dia. A pasta ficou com 18 dumps e 2 manifests. O script
        baixava TODOS os dumps e os conferia contra o manifest mais novo: os 9
        do run antigo não constavam nele, o `grep` saía 1 e — sob
        `set -Eeuo pipefail` — o trap ERR disparava com "erro inesperado na
        linha 96", sem dizer o que estava errado.

        Dentro de um mesmo run os nomes NÃO compartilham o RUN_ID: cada dump
        leva a hora do seu `pg_dump`. Por isso a lista vem do manifest.
        """
        self._semear_run("2026-11-09", "20261109T025501Z")
        sufixos = {
            "sistema_orcamentos": "20261109T124205Z",
            "sistema_financas": "20261109T124206Z",
            "sistema_vetorial": "20261109T124206Z",
            "sistema_arq": "20261109T124206Z",
            "sistema_questoes": "20261109T124207Z",
            "sistema_trilhas": "20261109T124207Z",
            "divisor_pdf": "20261109T124208Z",
            "dojo": "20261109T124208Z",
            "dracma.db": "20261109T124208Z",
        }
        self._semear_run("2026-11-09", "20261109T124205Z", sufixos=sufixos)

        # A pasta tem mesmo os dois runs completos.
        self.assertEqual(len(list((self.remote / "2026-11-09" / "databases").glob("*.dump"))), 18)

        r = self._rodar()
        self.assertEqual(r.returncode, 0, self._log())
        self.assertIn("run 20261109T124205Z", self._log())
        self.assertIn("Dumps do run: 9", self._log())
        self.assertNotIn("erro inesperado", self._log())
        self.assertIn("tudo restaurou", self._log())
        self.assertIn("finished_at=", self._sentinela())
        self.assertEqual(self._enviados(), "")

        # Só os dumps do run novo entraram: nenhum do run das 02:55.
        self.assertNotIn("20261109T025501Z", self._log())

    # --- falhas -----------------------------------------------------------

    def test_sha256_divergente_falha_e_avisa(self):
        self._semear_run("2026-11-09", "20261109T025501Z", corromper=("dojo",))
        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("sha256 do Drive não confere", self._log())
        self.assertIn("[RESTAURAÇÃO FALHOU]", self._enviados())
        self.assertEqual(self._sentinela(), "")

    def test_dump_faltando_falha_explicando(self):
        """Manifest lista 9, o Drive tem 8: mensagem clara, não trap opaco."""
        criados = self._semear_run("2026-11-09", "20261109T025501Z")
        criados["dojo"].unlink()
        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("dumps do run", self._log())
        self.assertNotIn("erro inesperado", self._log())
        self.assertIn("[RESTAURAÇÃO FALHOU]", self._enviados())
        self.assertEqual(self._sentinela(), "")

    def test_menos_de_nove_apps_falha(self):
        """Um app que sai do offsite sem ninguém notar tem de acusar."""
        self._semear_run("2026-11-09", "20261109T025501Z", apps=APPS[:8])
        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("esperava 9 dumps", self._log())
        self.assertIn("[RESTAURAÇÃO FALHOU]", self._enviados())
        self.assertEqual(self._sentinela(), "")

    def test_sem_pasta_datada_falha(self):
        r = self._rodar()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nenhuma pasta datada", self._log())
        self.assertEqual(self._sentinela(), "")

    def test_lock_impede_duas_execucoes(self):
        """Saída antecipada não pode gravar a sentinela de sucesso.

        O lock tomado faz o script sair 0 sem restaurar nada. Antes, esse 0
        caía no ramo de sucesso do `finalizar` e a sentinela era gravada — o
        `verifica_backup_recente.sh` passaria a ver uma prova de restauração
        que nunca aconteceu, e ficaria calado por mais 8 dias.
        """
        self._semear_run("2026-11-09", "20261109T025501Z")
        _exec(self.bin / "flock", "exit 1\n")
        r = self._rodar()
        self.assertEqual(r.returncode, 0)
        self.assertIn("já em execução", self._log())
        self.assertEqual(self._sentinela(), "")


if __name__ == "__main__":
    unittest.main()
