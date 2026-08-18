"""Testes do verificador de licenças de ambiente instalado.

`scripts/conferir_licencas_instaladas.py` reproduz a normalização do liccheck
0.9.2 para poder aplicar a mesma política a um venv lido de fora. Essa
reprodução é a parte frágil: ela foi derivada lendo `command_line.py`, e se o
liccheck mudar as regras, os dois passam a discordar em silêncio — o CI
aprovando o que o servidor reprova, ou pior, o contrário.

Estes testes fixam as quatro regras observadas:

  1. remove **um** sufixo " license" do fim;
  2. divide em " OR " (maiúsculo), e **não** em " AND ";
  3. divide o campo do pip-licenses em "; ";
  4. compara por igualdade exata, em minúsculas.

Cada caso abaixo veio de um pacote real encontrado nos venvs de produção.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "conferir_licencas_instaladas.py"
POLITICA = RAIZ / "configs" / "liccheck.ini"


class ConferirLicencasTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _rodar(self, pacotes, politica=None):
        inv = self.dir / "inv.json"
        inv.write_text(json.dumps(pacotes), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT),
             "--politica", str(politica or POLITICA), "--inventario", str(inv)],
            capture_output=True, text=True,
        )

    def _pacote(self, nome, licenca, versao="1.0"):
        return {"Name": nome, "Version": versao, "License": licenca}

    def test_aprova_spdx_simples(self):
        r = self._rodar([self._pacote("gunicorn", "MIT")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aprova_classifier_com_sufixo_license(self):
        # "MIT License" precisa virar "mit". Se o sufixo não fosse removido,
        # todo pacote de classifier antigo seria reprovado.
        r = self._rodar([self._pacote("redis", "MIT License")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aprova_apache_software_license(self):
        # Vira "apache software", não "apache-2.0" — a entrada da política
        # precisa existir nessa forma. Foi o primeiro furo encontrado.
        r = self._rodar([self._pacote("zopfli", "Apache Software License")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aprova_expressao_or(self):
        r = self._rodar([self._pacote("packaging", "Apache-2.0 OR BSD-2-Clause")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aprova_expressao_and_listada_inteira(self):
        # O liccheck não divide em " AND ", então a string composta precisa
        # estar na política por inteiro. Casos reais: aiohttp e greenlet.
        r = self._rodar([self._pacote("aiohttp", "Apache-2.0 AND MIT")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aprova_multiplos_classifiers_separados_por_ponto_e_virgula(self):
        r = self._rodar([
            self._pacote("python-dateutil", "Apache Software License; BSD License")
        ])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_aprova_licenca_dupla_em_texto_livre(self):
        # pymupdf: escolhemos o ramo AGPL. A string termina em " License",
        # então chega à comparação já sem o sufixo.
        r = self._rodar([self._pacote(
            "pymupdf",
            "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License",
        )])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_reprova_licenca_nao_declarada(self):
        r = self._rodar([self._pacote("misterioso", "UNKNOWN")])
        self.assertEqual(r.returncode, 1)
        self.assertIn("misterioso", r.stderr)

    def test_reprova_campo_vazio(self):
        r = self._rodar([self._pacote("sem_metadado", "")])
        self.assertEqual(r.returncode, 1)
        self.assertIn("não declarada", r.stderr)

    def test_reprova_proprietaria(self):
        r = self._rodar([self._pacote("fechado", "Other/Proprietary License")])
        self.assertEqual(r.returncode, 1)

    def test_reprova_gpl2_only(self):
        # GPL-2.0 sem "or later" é incompatível com AGPL-3.0; a forma
        # "or later" é compatível e está autorizada.
        r = self._rodar([self._pacote("velho", "GPL-2.0-only")])
        self.assertEqual(r.returncode, 1)

    def test_proibida_vence_autorizada_na_mesma_linha(self):
        # Regra do nível CAUTIOUS: não basta ter uma autorizada, não pode
        # haver nenhuma proibida junto.
        r = self._rodar([self._pacote("misto", "MIT; GPL-2.0-only")])
        self.assertEqual(r.returncode, 1)

    def test_excecao_por_pacote_e_respeitada(self):
        politica = self.dir / "politica.ini"
        politica.write_text(
            "[Licenses]\nauthorized_licenses:\n    mit\n"
            "unauthorized_licenses:\n    proprietary\n"
            "[Authorized Packages]\nfechado:\n",
            encoding="utf-8",
        )
        r = self._rodar([self._pacote("fechado", "Proprietary")], politica=politica)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_politica_ilegivel_nao_aprova_por_omissao(self):
        # Falha de leitura precisa sair diferente de 0 e diferente de 1: uma
        # política vazia que devolvesse "tudo certo" seria o pior desfecho.
        politica = self.dir / "vazia.ini"
        politica.write_text("[Licenses]\n", encoding="utf-8")
        r = self._rodar([self._pacote("qualquer", "MIT")], politica=politica)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
