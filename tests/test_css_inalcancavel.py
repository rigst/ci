"""Testes do detector de CSS inalcançável.

O script apaga regras de folhas de estilo de produção, então o custo de um
falso positivo é uma tela quebrada em silêncio. Estes casos existem porque a
primeira versão da análise era um scanner artesanal e eles pegaram dois
defeitos nela: perdia regra dentro de `@media` e confundia `;` dentro de string
(`content: "; }"`). A versão atual usa `tinycss2`.

Os casos vivos importam tanto quanto os mortos — cada um é uma razão pela qual
a regra ainda pode vencer alguém.
"""

import importlib.util
import pathlib
import tempfile
import unittest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("cssin", RAIZ / "scripts" / "css_inalcancavel.py")
cssin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cssin)

try:
    import tinycss2  # noqa: F401
    TEM_PARSER = True
except ImportError:
    TEM_PARSER = False


@unittest.skipUnless(TEM_PARSER, "tinycss2 não está instalado")
class InalcancavelTests(unittest.TestCase):
    def mortas(self, css):
        rs = cssin.regras(css)
        return sorted(rs[i]["sel"] for i in cssin.mortas(rs))

    # -- o que deve morrer ----------------------------------------------

    def test_todas_as_declaracoes_sobrescritas(self):
        self.assertEqual(
            self.mortas(".a { color: red; background: blue; }\n.a { color: green; background: yellow; }"),
            [".a"])

    def test_important_coberto_por_important(self):
        self.assertEqual(
            self.mortas(".d { color: red !important; }\n.d { color: green !important; }"),
            [".d"])

    def test_mesmo_contexto_de_media(self):
        css = "@media screen { .f { color: red; } }\n@media screen { .f { color: green; } }"
        self.assertEqual(self.mortas(css), [".f"])

    def test_ponto_e_virgula_dentro_de_string_nao_confunde(self):
        """`content: "; }"` quebrava o scanner artesanal."""
        css = '.j { content: "; }"; color: red; }\n.j { content: "outro"; color: green; }'
        self.assertEqual(self.mortas(css), [".j"])

    def test_comentario_antes_da_regra_nao_muda_o_seletor(self):
        """`/* nota */ .a` e `.a` são o mesmo seletor — não vê-lo assim fazia a
        análise inteira não achar nada."""
        css = "/* nota */\n.a { color: red; }\n.a { color: green; }"
        self.assertEqual(self.mortas(css), [".a"])

    # -- o que deve sobreviver ------------------------------------------

    def test_sobrescrita_parcial_sobrevive(self):
        self.assertEqual(
            self.mortas(".b { color: red; margin: 4px; }\n.b { color: green; }"), [])

    def test_important_anterior_vence_posterior_sem_important(self):
        self.assertEqual(
            self.mortas(".c { color: red !important; }\n.c { color: green; }"), [])

    def test_contexto_de_media_diferente_nao_sobrescreve(self):
        css = ".e { color: red; }\n@media print { .e { color: green; } }"
        self.assertEqual(self.mortas(css), [])

    def test_seletor_com_virgula_fica_de_fora(self):
        css = ".g, .h { color: red; }\n.g { color: green; }\n.h { color: green; }"
        self.assertEqual(self.mortas(css), [])

    def test_pseudo_classe_e_outro_seletor(self):
        self.assertEqual(
            self.mortas(".i:hover { color: red; }\n.i { color: green; }"), [])

    def test_atalho_depois_de_longhand_fica_de_fora(self):
        """`margin` depois de `margin-top` sobrescreve de fato, mas comparar só
        nomes iguais mantém a análise conservadora — e conservador é o lado
        certo de errar aqui."""
        self.assertEqual(
            self.mortas(".k { margin-top: 4px; }\n.k { margin: 8px; }"), [])

    # -- a remoção preserva a cascata -----------------------------------

    def test_remocao_preserva_a_cascata(self):
        css = (".a { color: red; }\n.a { color: green; }\n"
               "@media screen { .f { color: red; } }\n@media screen { .f { color: green; } }\n")
        arq = pathlib.Path(tempfile.mkdtemp()) / "e.css"
        arq.write_text(css, encoding="utf-8")
        antes = cssin.cascata(cssin.regras(css))
        cssin.sys.argv = ["css_inalcancavel.py", str(arq), "--aplicar"]
        self.assertEqual(cssin.main(), 0)
        depois = cssin.cascata(cssin.regras(arq.read_text(encoding="utf-8")))
        self.assertEqual(antes, depois)
        self.assertNotIn("color: red", arq.read_text(encoding="utf-8"))

    def test_at_rule_vazia_nao_fica_para_tras(self):
        """`@media screen {}` seria acusado pelo `block-no-empty` do Stylelint:
        a limpeza não pode criar achado novo."""
        css = "@media screen { .f { color: red; } }\n@media screen { .f { color: green; } }\n"
        arq = pathlib.Path(tempfile.mkdtemp()) / "e.css"
        arq.write_text(css, encoding="utf-8")
        cssin.sys.argv = ["css_inalcancavel.py", str(arq), "--aplicar"]
        cssin.main()
        self.assertNotRegex(arq.read_text(encoding="utf-8"), r"@media[^{]*\{\s*\}")


if __name__ == "__main__":
    unittest.main()
