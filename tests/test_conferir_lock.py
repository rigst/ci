"""Testes do verificador de lock.

`scripts/conferir_lock.py` é o portão que impede `requirements.txt` e o lock
de divergirem depois de um bump do Dependabot — que não reconhece o lock como
arquivo de dependências e portanto nunca o atualiza.

Portão que quebra em silêncio é pior do que portão nenhum: se o script passar
a devolver 0 sempre, ninguém percebe até produção instalar a versão errada.
Como ele agora vale para todos os projetos, e não só para aquele onde nasceu,
o silêncio custaria sete repositórios em vez de um.

Os testes invocam o script como subprocesso, e não importando o módulo: é a
interface de linha de comando que o workflow usa, então é ela que precisa
continuar funcionando.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "conferir_lock.py"


class ConferirLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.req = self.dir / "requirements.txt"
        self.lock = self.dir / "requirements.lock"

    def _rodar(self, requirements, lock, lock_existe=True):
        self.req.write_text(requirements, encoding="utf-8")
        if lock_existe:
            self.lock.write_text(lock, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT),
             "--requirements", str(self.req), "--lock", str(self.lock)],
            capture_output=True, text=True,
        )

    def test_aprova_quando_as_versoes_conferem(self):
        r = self._rodar(
            "Django==6.1\npsycopg[binary]==3.2.12\n",
            "django==6.1 \\\n    --hash=sha256:abc\n"
            "psycopg==3.2.12 \\\n    --hash=sha256:def\n",
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_reprova_quando_a_versao_diverge(self):
        r = self._rodar("Django==6.2\n", "django==6.1 \\\n    --hash=sha256:abc\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("DIVERGE", r.stderr)

    def test_reprova_quando_falta_no_lock(self):
        r = self._rodar(
            "Django==6.1\nredis==5.2.1\n",
            "django==6.1 \\\n    --hash=sha256:abc\n",
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("FALTA no lock", r.stderr)

    def test_ignora_comentario_e_linha_vazia(self):
        r = self._rodar(
            "# comentário\n\nDjango==6.1\n",
            "django==6.1 \\\n    --hash=sha256:abc\n",
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_normaliza_underscore_e_caixa(self):
        # O PyPI trata "-" e "_" como o mesmo nome, e o gerador emite o lock na
        # forma normalizada da PEP 503. Sem esta normalização, `prompt_toolkit`
        # no requirements e `prompt-toolkit` no lock pareceriam pacotes
        # diferentes — e o portão reprovaria um lock correto.
        r = self._rodar(
            "dj_database_url==3.1.2\n",
            "dj-database-url==3.1.2 \\\n    --hash=sha256:abc\n",
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_reprova_quando_o_lock_nao_existe(self):
        r = self._rodar("Django==6.1\n", "", lock_existe=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("não existe", r.stderr)

    def test_reprova_faixa_de_versao_no_requirements(self):
        # Faixa e lock não convivem: o lock congelaria a resolução de hoje
        # enquanto o requirements seguiria dizendo que qualquer versão serve.
        r = self._rodar(
            "Django>=6.0,<7.0\n",
            "django==6.1 \\\n    --hash=sha256:abc\n",
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("sem pin exato", r.stderr)

    def test_reprova_pino_sem_hash(self):
        # Um pin sem hash passa pelo --require-hashes do pip apenas enquanto
        # nenhum outro pacote tiver hash. É o buraco que o lock deveria fechar.
        r = self._rodar("Django==6.1\n", "django==6.1\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SEM HASH", r.stderr)

    def test_hash_na_linha_seguinte_conta(self):
        # O formato do pip aceita o hash em continuação de linha; ler só a
        # primeira linha acusaria "sem hash" num lock perfeitamente válido.
        r = self._rodar(
            "Django==6.1\n",
            "django==6.1 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def\n",
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_ignora_marcador_de_ambiente(self):
        r = self._rodar(
            'tzdata==2026.3; sys_platform == "win32"\n',
            "tzdata==2026.3 \\\n    --hash=sha256:abc\n",
        )
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
