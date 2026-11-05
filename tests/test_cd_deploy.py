"""Testes do esqueleto `examples/cd-deploy.sh`: smoke-test e rollback.

O script real roda no servidor, como o usuário `deploy`. Aqui ele roda contra
um repositório git de mentira, com `sudo`, `curl`, `pip` e `python`
substituídos por stubs no PATH/venv. O que se verifica é a decisão:

- healthz 200 → deploy fica, sai 0;
- healthz quebrado, sem migração → volta o código ao commit anterior, sai 1;
- healthz quebrado, com migração → NÃO volta (schema novo), sai 1 e explica;
- healthz que redireciona (302) conta como quebrado.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ESQUELETO = RAIZ / "examples" / "cd-deploy.sh"


def _exec(caminho, corpo):
    caminho.write_text("#!/usr/bin/env bash\n" + corpo, encoding="utf-8")
    caminho.chmod(caminho.stat().st_mode | stat.S_IEXEC)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git") and shutil.which("flock"), "precisa de git e flock")
class CdDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origem = self.tmp / "origem"
        self.app = self.tmp / "app"
        self.bin = self.tmp / "bin"
        self.venv = self.tmp / "venv"
        self.log = self.tmp / "chamadas.log"
        for d in (self.origem, self.bin, self.venv / "bin"):
            d.mkdir(parents=True)

        _git(self.origem, "init", "-q", "-b", "main")
        _git(self.origem, "config", "user.email", "t@t")
        _git(self.origem, "config", "user.name", "t")
        (self.origem / "requirements.lock").write_text("", encoding="utf-8")
        (self.origem / "app.txt").write_text("v1\n", encoding="utf-8")
        _git(self.origem, "add", ".")
        _git(self.origem, "commit", "-q", "-m", "v1")
        self.v1 = _git(self.origem, "rev-parse", "HEAD")
        subprocess.run(["git", "clone", "-q", str(self.origem), str(self.app)], check=True)

        # curl devolve o código gravado para o commit que está no ar.
        self.codigos = self.tmp / "codigos"
        self.codigos.mkdir()
        _exec(self.bin / "curl", f"""
sha="$(git -C {self.app} rev-parse HEAD)"
echo "curl $sha" >> {self.log}
cat "{self.codigos}/$sha" 2>/dev/null || printf 200
""")
        _exec(self.bin / "sudo", f'echo "sudo $*" >> {self.log}\n')
        _exec(self.bin / "systemctl", "echo ''\n")
        _exec(self.bin / "sleep", "true\n")
        _exec(self.venv / "bin" / "pip", f'echo "pip $*" >> {self.log}\n')
        _exec(self.venv / "bin" / "python", f'echo "python $*" >> {self.log}\nexit 0\n')

        script = ESQUELETO.read_text(encoding="utf-8")
        trocas = {
            "APP_DIR=/var/www/PROJETO/current": f"APP_DIR={self.app}",
            "FETCH_URL=https://github.com/rigst/PROJETO.git": f"FETCH_URL={self.origem}",
            "VENV=/var/www/PROJETO/venv": f"VENV={self.venv}",
            "ENV_FILE=/var/www/PROJETO/shared/.env": "ENV_FILE=",
            'HEALTH_URL="https://PROJETO.stolben.com/healthz/"': 'HEALTH_URL="https://teste/healthz/"',
            "LOCK_FILE=/tmp/PROJETO_cd_deploy.lock": f"LOCK_FILE={self.tmp}/deploy.lock",
        }
        for antigo, novo in trocas.items():
            self.assertIn(antigo, script)
            script = script.replace(antigo, novo)
        self.script = self.tmp / "cd-deploy.sh"
        _exec(self.script, script.split("\n", 1)[1])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, nome, arquivo="app.txt"):
        alvo = self.origem / arquivo
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(f"{nome}\n", encoding="utf-8")
        _git(self.origem, "add", ".")
        _git(self.origem, "commit", "-q", "-m", nome)
        return _git(self.origem, "rev-parse", "HEAD")

    def _deploy(self, sha):
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "SSH_ORIGINAL_COMMAND": f"deploy {sha}"}
        return subprocess.run(["bash", str(self.script)], env=env, capture_output=True, text=True, check=False)

    def test_deploy_saudavel_fica_no_ar(self):
        v2 = self._commit("v2")
        r = self._deploy(v2)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_git(self.app, "rev-parse", "HEAD"), v2)
        self.assertIn("sudo systemctl reload", self.log.read_text())

    def test_healthz_quebrado_sem_migracao_volta_o_codigo(self):
        v2 = self._commit("v2")
        (self.codigos / v2).write_text("503")
        r = self._deploy(v2)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(_git(self.app, "rev-parse", "HEAD"), self.v1)
        self.assertIn("Rollback concluído", r.stdout)
        # restart (não reload) no rollback: o pip pode ter trocado pacotes.
        self.assertIn("sudo systemctl restart", self.log.read_text())

    def test_healthz_quebrado_com_migracao_nao_volta(self):
        v2 = self._commit("v2", arquivo="core/migrations/0002_x.py")
        (self.codigos / v2).write_text("500")
        r = self._deploy(v2)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(_git(self.app, "rev-parse", "HEAD"), v2)
        self.assertIn("SEM rollback automático", r.stdout)

    def test_redirecionamento_nao_conta_como_saude(self):
        v2 = self._commit("v2")
        (self.codigos / v2).write_text("302")
        r = self._deploy(v2)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(_git(self.app, "rev-parse", "HEAD"), self.v1)

    def test_rollback_que_nao_recupera_pede_intervencao(self):
        v2 = self._commit("v2")
        (self.codigos / v2).write_text("503")
        (self.codigos / self.v1).write_text("503")
        r = self._deploy(v2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("intervenção manual", r.stdout)

    def test_sha_que_nao_esta_na_main_e_recusado(self):
        r = self._deploy("deadbeef")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(_git(self.app, "rev-parse", "HEAD"), self.v1)


if __name__ == "__main__":
    unittest.main()
