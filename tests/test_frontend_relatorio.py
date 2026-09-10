"""Testes do consolidador de frontend.

`scripts/frontend_relatorio.py` junta três ferramentas que relatam de formas
diferentes — duas em JSON com esquemas distintos, uma em texto — e decide o
veredito. Errar a leitura de qualquer uma delas produz "zero achados", que é
indistinguível de "está limpo": a etapa fica verde e ninguém percebe que uma
das três parou de contar.

Foi exatamente o que aconteceu no primeiro PR de teste: o djlint troca do bloco
legível para anotações do GitHub quando detecta o Actions, o parser não
reconheceu a linha, e o único achado de template sumiu da contagem sem nenhum
erro no log. `test_djlint_no_formato_de_anotacao` existe por causa disso.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "frontend_relatorio.py"


class FrontendRelatorioTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "templates").mkdir()
        (self.dir / "templates" / "pagina.html").write_text("<p>oi</p>\n", encoding="utf-8")
        (self.dir / "app.css").write_text(".a{color:red}\n", encoding="utf-8")
        (self.dir / "app.js").write_text("var a = 1;\n", encoding="utf-8")

    def _rodar(self, *args, fail_on="erro"):
        saida = self.dir / "frontend.json"
        ambiente = dict(os.environ)
        ambiente.pop("GITHUB_STEP_SUMMARY", None)
        processo = subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--fail-on", fail_on, "--out", "frontend.json"],
            cwd=self.dir, capture_output=True, text=True, check=False, env=ambiente,
        )
        dados = json.loads(saida.read_text(encoding="utf-8")) if saida.exists() else None
        return processo, dados

    def _escrever(self, nome, conteudo):
        (self.dir / nome).write_text(conteudo, encoding="utf-8")
        return nome

    # -- djlint: os dois formatos --------------------------------------

    def test_djlint_no_formato_de_anotacao(self):
        """Formato que o djlint usa quando roda dentro do GitHub Actions."""
        self._escrever("djlint.txt",
                       "::warning file=templates/pagina.html,line=191,col=29::"
                       "H020 Empty tag pair found. Consider removing.\n")
        _, dados = self._rodar("--djlint", "djlint.txt")
        self.assertEqual(len(dados), 1)
        self.assertEqual(dados[0]["regra"], "H020")
        self.assertEqual(dados[0]["arquivo"], "templates/pagina.html")
        self.assertEqual(dados[0]["linha"], 191)
        self.assertEqual(dados[0]["severidade"], "aviso")

    def test_djlint_no_formato_legivel(self):
        """Formato que ele usa no terminal, com cabeçalho de arquivo."""
        self._escrever("djlint.txt",
                       "Linting 1/1 files\n\n"
                       "pagina.html\n"
                       "────────\n"
                       "H043 77:6 Button tag should have a type attribute.\n"
                       "\nLinted 1 files, found 1 errors.\n")
        _, dados = self._rodar("--djlint", "djlint.txt", "--djlint-raiz", "templates")
        self.assertEqual(len(dados), 1)
        self.assertEqual(dados[0]["regra"], "H043")
        # o cabeçalho vem relativo ao diretório passado ao djlint
        self.assertEqual(dados[0]["arquivo"], "templates/pagina.html")

    def test_h043_bloqueia_e_h020_nao(self):
        """`button` sem type submete o formulário sozinho; par de tags vazio é
        limpeza. A diferença precisa sobreviver a qualquer refatoração."""
        self._escrever("djlint.txt",
                       "::error file=templates/pagina.html,line=1::H043 Button tag should have a type.\n"
                       "::warning file=templates/pagina.html,line=2::H020 Empty tag pair found.\n")
        processo, dados = self._rodar("--djlint", "djlint.txt")
        por_regra = {a["regra"]: a["severidade"] for a in dados}
        self.assertEqual(por_regra, {"H043": "erro", "H020": "aviso"})
        self.assertEqual(processo.returncode, 1)

    # -- severidade das outras duas -------------------------------------

    def test_stylelint_sempre_entra_como_aviso(self):
        """O passivo herdado de CSS é dívida a conter, não build a derrubar —
        o que bloqueia é `!important` NOVO, e disso cuida o diff_quality."""
        self._escrever("stylelint.json", json.dumps([{
            "source": str(self.dir / "app.css"),
            "warnings": [{"line": 3, "rule": "no-duplicate-selectors",
                          "severity": "error", "text": "Unexpected duplicate selector"}],
        }]))
        processo, dados = self._rodar("--stylelint", "stylelint.json")
        self.assertEqual(dados[0]["severidade"], "aviso")
        self.assertEqual(dados[0]["arquivo"], "app.css")
        self.assertEqual(processo.returncode, 0)

    def test_eslint_severidade_2_bloqueia(self):
        self._escrever("eslint.json", json.dumps([{
            "filePath": str(self.dir / "app.js"),
            "messages": [
                {"line": 1, "ruleId": "no-undef", "severity": 2, "message": "'x' is not defined."},
                {"line": 2, "ruleId": "no-unused-vars", "severity": 1, "message": "'a' unused."},
            ],
        }]))
        processo, dados = self._rodar("--eslint", "eslint.json")
        por_regra = {a["regra"]: a["severidade"] for a in dados}
        self.assertEqual(por_regra, {"no-undef": "erro", "no-unused-vars": "aviso"})
        self.assertEqual(processo.returncode, 1)

    # -- o silêncio que não pode passar por sucesso ---------------------

    def test_relatorio_ausente_reprova_em_vez_de_dar_zero(self):
        """Ferramenta que não chegou a rodar não é 'nada encontrado'."""
        processo, _ = self._rodar("--eslint", "nao-existe.json")
        self.assertEqual(processo.returncode, 1)
        self.assertIn("não gerou relatório", processo.stdout)

    def test_relatorio_ilegivel_reprova(self):
        self._escrever("eslint.json", "{isto não é json}")
        processo, _ = self._rodar("--eslint", "eslint.json")
        self.assertEqual(processo.returncode, 1)
        self.assertIn("ilegível", processo.stdout)

    def test_fail_on_nenhum_relata_sem_reprovar(self):
        self._escrever("eslint.json", json.dumps([{
            "filePath": str(self.dir / "app.js"),
            "messages": [{"line": 1, "ruleId": "no-undef", "severity": 2, "message": "x"}],
        }]))
        processo, dados = self._rodar("--eslint", "eslint.json", fail_on="nenhum")
        self.assertEqual(len(dados), 1)
        self.assertEqual(processo.returncode, 0)


if __name__ == "__main__":
    unittest.main()
