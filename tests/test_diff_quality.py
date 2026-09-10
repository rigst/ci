"""Testes do cobrador de qualidade do diff.

`scripts/diff_quality.py` decide o que é dívida nova e o que é dívida herdada.
Errar para o lado permissivo deixa o remendo entrar; errar para o lado severo
transforma o pipeline em ruído e a equipe desliga a etapa inteira — que é como
se perde uma ferramenta de qualidade.

O teste que mais importa é `test_linha_deslocada_nao_e_novidade`: ele é a razão
de este script existir em vez de um arquivo de baseline com `arquivo+linha`.
Inserir uma linha no topo do módulo desloca todas as outras, e um registro por
posição acusaria o arquivo inteiro como problema novo no primeiro PR.

Os testes montam um repositório git de verdade num diretório temporário e
invocam o script como subprocesso: é a interface que o workflow usa, com o
`git diff` real que ela consome.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "diff_quality.py"


class DiffQualityTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "ci@exemplo.invalido")
        self._git("config", "user.name", "CI")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.dir, capture_output=True, text=True, check=True
        )

    def _escrever(self, nome, conteudo):
        alvo = self.dir / nome
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text(conteudo, encoding="utf-8")

    def _commitar(self, mensagem):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", mensagem)

    def _base(self, arquivos, mensagem="base"):
        for nome, conteudo in arquivos.items():
            self._escrever(nome, conteudo)
        self._commitar(mensagem)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _rodar(self, base, *extra):
        saida = self.dir / "relatorio.json"
        ambiente = dict(os.environ)
        ambiente.pop("GITHUB_STEP_SUMMARY", None)
        processo = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", base, "--out", str(saida), *extra],
            cwd=self.dir, capture_output=True, text=True, check=False, env=ambiente,
        )
        relatorio = json.loads(saida.read_text(encoding="utf-8")) if saida.exists() else None
        return processo, relatorio

    def _regras(self, relatorio):
        return sorted(a["regra"] for a in relatorio["achados"])

    # -- o caso que justifica a arquitetura -------------------------------

    def test_linha_deslocada_nao_e_novidade(self):
        """Dívida antiga empurrada para baixo continua sendo dívida antiga."""
        antigo = (
            "def antiga():\n"
            "    try:\n"
            "        risco()\n"
            "    except Exception:\n"
            "        pass\n"
        )
        base = self._base({"app.py": antigo})
        self._escrever("app.py", '"""Docstring nova no topo."""\n\n' + antigo)
        self._commitar("insere linha no topo")

        _, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [],
                         "o except herdado apenas mudou de linha; não é problema novo")

    def test_mesmo_problema_escrito_agora_bloqueia(self):
        base = self._base({"app.py": "def antiga():\n    return 1\n"})
        self._escrever("app.py", (
            "def antiga():\n"
            "    return 1\n"
            "\n"
            "def nova():\n"
            "    try:\n"
            "        risco()\n"
            "    except Exception:\n"
            "        pass\n"
        ))
        self._commitar("adiciona except engolido")

        processo, relatorio = self._rodar(base)
        self.assertIn("excecao-engolida", self._regras(relatorio))
        self.assertEqual(processo.returncode, 1)

    # -- exceções ---------------------------------------------------------

    def test_except_generico_com_log_de_contexto_passa(self):
        base = self._base({"app.py": "import logging\nlogger = logging.getLogger(__name__)\n"})
        self._escrever("app.py", (
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "def enviar(doc):\n"
            "    try:\n"
            "        entregar(doc)\n"
            "    except Exception:\n"
            "        logger.exception('falha ao entregar %s', doc.pk)\n"
        ))
        self._commitar("except com log")

        processo, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [])
        self.assertEqual(processo.returncode, 0)

    def test_except_generico_sem_tratamento_bloqueia(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", (
            "x = 1\n"
            "\n"
            "def enviar():\n"
            "    try:\n"
            "        entregar()\n"
            "    except Exception:\n"
            "        return None\n"
        ))
        self._commitar("except silencioso")

        _, relatorio = self._rodar(base)
        self.assertIn("excecao-generica", self._regras(relatorio))

    def test_except_especifico_nao_e_acusado(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", (
            "x = 1\n"
            "\n"
            "def ler(caminho):\n"
            "    try:\n"
            "        return open(caminho).read()\n"
            "    except FileNotFoundError:\n"
            "        return ''\n"
        ))
        self._commitar("except específico")

        _, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [])

    def test_except_em_string_nao_conta(self):
        """O AST existe justamente para não confundir prosa com código."""
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", "x = 1\nAJUDA = 'evite except Exception: pass no código'\n")
        self._commitar("menção em string")

        _, relatorio = self._rodar(base)
        self.assertEqual(self._regras(relatorio), [])

    # -- migrações --------------------------------------------------------

    def test_migracao_destrutiva_bloqueia(self):
        base = self._base({"app/migrations/0001_inicial.py": "operations = []\n"})
        self._escrever("app/migrations/0002_limpa.py", (
            "from django.db import migrations\n"
            "\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [migrations.RemoveField('Contrato', 'anexo')]\n"
        ))
        self._commitar("remove campo")

        processo, relatorio = self._rodar(base)
        self.assertIn("migracao-destrutiva", self._regras(relatorio))
        self.assertEqual(processo.returncode, 1)

    def test_alterfield_obrigatorio_sem_default_bloqueia(self):
        base = self._base({"app/migrations/0001_inicial.py": "operations = []\n"})
        self._escrever("app/migrations/0002_aperta.py", (
            "from django.db import migrations, models\n"
            "\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [\n"
            "        migrations.AlterField('Contrato', 'valor', models.DecimalField(null=False)),\n"
            "    ]\n"
        ))
        self._commitar("aperta campo")

        _, relatorio = self._rodar(base)
        self.assertIn("migracao-destrutiva", self._regras(relatorio))

    def test_alterfield_com_default_passa(self):
        base = self._base({"app/migrations/0001_inicial.py": "operations = []\n"})
        self._escrever("app/migrations/0002_aperta.py", (
            "from django.db import migrations, models\n"
            "\n"
            "class Migration(migrations.Migration):\n"
            "    operations = [\n"
            "        migrations.AlterField('Contrato', 'valor',\n"
            "                              models.DecimalField(null=False, default=0)),\n"
            "    ]\n"
        ))
        self._commitar("aperta campo com default")

        _, relatorio = self._rodar(base)
        self.assertEqual(self._regras(relatorio), [])

    # -- frontend e marcadores -------------------------------------------

    def test_important_novo_e_aviso_e_nao_bloqueia(self):
        base = self._base({"static/css/app.css": ".a { color: red; }\n"})
        self._escrever("static/css/app.css", ".a { color: red; }\n.b { color: blue !important; }\n")
        self._commitar("novo important")

        processo, relatorio = self._rodar(base)
        self.assertIn("important-novo", self._regras(relatorio))
        self.assertEqual(relatorio["achados"][0]["severidade"], "aviso")
        self.assertEqual(processo.returncode, 0, "aviso não pode derrubar o build")

    def test_safe_novo_em_template_bloqueia(self):
        base = self._base({"templates/pagina.html": "<p>{{ texto }}</p>\n"})
        self._escrever("templates/pagina.html", "<p>{{ texto }}</p>\n<div>{{ html|safe }}</div>\n")
        self._commitar("novo safe")

        processo, relatorio = self._rodar(base)
        self.assertIn("escape-desligado", self._regras(relatorio))
        self.assertEqual(processo.returncode, 1)

    def test_marcador_provisorio_novo_vira_aviso(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", "x = 1\n# TODO: arrumar depois\n")
        self._commitar("marcador")

        _, relatorio = self._rodar(base)
        self.assertIn("marcador-provisorio", self._regras(relatorio))

    # -- tamanho e crescimento -------------------------------------------

    def test_funcao_nova_grande_vira_aviso(self):
        base = self._base({"app.py": "x = 1\n"})
        corpo = "\n".join(f"    passo_{i} = {i}" for i in range(60))
        self._escrever("app.py", f"x = 1\n\ndef enorme():\n{corpo}\n")
        self._commitar("função grande")

        _, relatorio = self._rodar(base)
        self.assertIn("funcao-grande", self._regras(relatorio))

    def test_funcao_nova_dentro_do_limite_passa(self):
        base = self._base({"app.py": "x = 1\n"})
        corpo = "\n".join(f"    passo_{i} = {i}" for i in range(10))
        self._escrever("app.py", f"x = 1\n\ndef pequena():\n{corpo}\n")
        self._commitar("função pequena")

        _, relatorio = self._rodar(base)
        self.assertEqual(self._regras(relatorio), [])

    def test_arquivo_que_engorda_muito_vira_aviso(self):
        base = self._base({"app.py": "\n".join(f"linha_{i} = {i}" for i in range(200)) + "\n"})
        antigo = (self.dir / "app.py").read_text(encoding="utf-8")
        self._escrever("app.py", antigo + "\n".join(f"nova_{i} = {i}" for i in range(60)) + "\n")
        self._commitar("engorda")

        _, relatorio = self._rodar(base)
        self.assertIn("arquivo-cresceu", self._regras(relatorio))

    def test_arquivo_pequeno_nao_dispara_crescimento(self):
        """Em arquivo curto qualquer adição estoura o percentual; seria só ruído."""
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", "x = 1\ny = 2\nz = 3\n")
        self._commitar("adição trivial")

        _, relatorio = self._rodar(base)
        self.assertNotIn("arquivo-cresceu", self._regras(relatorio))

    # -- rede -------------------------------------------------------------

    def test_chamada_externa_sem_timeout_vira_aviso(self):
        base = self._base({"app.py": "import requests\n"})
        self._escrever("app.py", "import requests\n\ndef buscar():\n    return requests.get(URL)\n")
        self._commitar("sem timeout")

        _, relatorio = self._rodar(base)
        self.assertIn("sem-timeout", self._regras(relatorio))

    def test_chamada_externa_com_timeout_passa(self):
        base = self._base({"app.py": "import requests\n"})
        self._escrever("app.py",
                       "import requests\n\ndef buscar():\n    return requests.get(URL, timeout=10)\n")
        self._commitar("com timeout")

        _, relatorio = self._rodar(base)
        self.assertEqual(self._regras(relatorio), [])

    # -- portão e exclusões ----------------------------------------------

    def test_fail_on_nenhum_relata_sem_reprovar(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", "x = 1\n\ndef f():\n    try:\n        g()\n    except:\n        pass\n")
        self._commitar("except nu")

        processo, relatorio = self._rodar(base, "--fail-on", "nenhum")
        self.assertTrue(relatorio["achados"])
        self.assertEqual(processo.returncode, 0)

    def test_caminho_ignorado_nao_e_analisado(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("static/js/vendor/lib.min.js", "var a=1 //!important\n")
        self._escrever("node_modules/pacote/index.js", "// TODO deixado pelo autor\n")
        self._commitar("dependência vendorizada")

        _, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [])

    def test_arquivo_apagado_nao_gera_achado(self):
        base = self._base({"app.py": "x = 1\n", "velho.py": "# TODO antigo\n"})
        (self.dir / "velho.py").unlink()
        self._commitar("apaga arquivo")

        processo, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [])
        self.assertEqual(processo.returncode, 0)

    def test_resumo_do_job_e_escrito_quando_a_variavel_existe(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", "x = 1\n# TODO: depois\n")
        self._commitar("marcador")

        resumo = self.dir / "resumo.md"
        ambiente = dict(os.environ, GITHUB_STEP_SUMMARY=str(resumo))
        subprocess.run(
            [sys.executable, str(SCRIPT), "--base", base, "--out", str(self.dir / "r.json")],
            cwd=self.dir, capture_output=True, text=True, check=False, env=ambiente,
        )
        self.assertIn("Qualidade do diff", resumo.read_text(encoding="utf-8"))

    def test_estilo_inline_novo_vira_aviso(self):
        """Contraparte do H021, que sai do djlint por volume (231 na frota) e
        entra aqui, onde só as ocorrências novas são cobradas."""
        base = self._base({"templates/p.html": "<p>oi</p>\n"})
        self._escrever("templates/p.html", '<p>oi</p>\n<div style="margin-top:8px">a</div>\n')
        self._commitar("estilo inline")

        processo, relatorio = self._rodar(base)
        self.assertIn("estilo-inline", self._regras(relatorio))
        self.assertEqual(processo.returncode, 0, "aviso não bloqueia")

    # -- supressão justificada -------------------------------------------

    def test_supressao_com_motivo_libera_o_achado(self):
        """O caso legítimo existe: inicialização de monitoramento opcional é o
        exemplo real que apareceu no settings de produção do sistema_trilhas."""
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", (
            "x = 1\n"
            "\n"
            "def iniciar():\n"
            "    try:\n"
            "        import sentry_sdk\n"
            "    # qualidade: ignorar excecao-engolida - monitoramento e opcional,\n"
            "    # o app precisa subir sem ele\n"
            "    except Exception:\n"
            "        pass\n"
        ))
        self._commitar("except suprimido com motivo")

        processo, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [])
        self.assertEqual(processo.returncode, 0)

    def test_supressao_sem_motivo_e_ela_mesma_um_erro(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", (
            "x = 1\n"
            "\n"
            "def iniciar():\n"
            "    try:\n"
            "        risco()\n"
            "    except Exception:  # qualidade: ignorar excecao-engolida\n"
            "        pass\n"
        ))
        self._commitar("supressão pelada")

        processo, relatorio = self._rodar(base)
        self.assertEqual(self._regras(relatorio), ["supressao-sem-motivo"])
        self.assertEqual(processo.returncode, 1)

    def test_supressao_de_outra_regra_nao_vale(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", (
            "x = 1\n"
            "\n"
            "def iniciar():\n"
            "    try:\n"
            "        risco()\n"
            "    # qualidade: ignorar sem-timeout - regra diferente desta aqui\n"
            "    except Exception:\n"
            "        pass\n"
        ))
        self._commitar("supressão de outra regra")

        _, relatorio = self._rodar(base)
        self.assertIn("excecao-engolida", self._regras(relatorio))

    def test_supressao_funciona_em_css(self):
        base = self._base({"static/css/app.css": ".a { color: red; }\n"})
        self._escrever("static/css/app.css", (
            ".a { color: red; }\n"
            "/* qualidade: ignorar important-novo - sobrepoe estilo do widget de terceiro */\n"
            ".b { color: blue !important; }\n"
        ))
        self._commitar("important justificado")

        _, relatorio = self._rodar(base)
        self.assertEqual(relatorio["achados"], [])


    # -- guardas de entrada ----------------------------------------------

    def test_base_que_parece_opcao_do_git_e_recusada(self):
        """Uma ref começando com `-` é lida pelo git como opção, não revisão:
        `--output=...` onde se espera um commit escreve arquivo."""
        self._base({"app.py": "x = 1\n"})
        processo = subprocess.run(
            [sys.executable, str(SCRIPT), "--base=--output=/tmp/nao-deve-existir",
             "--out", str(self.dir / "r.json")],
            cwd=self.dir, capture_output=True, text=True, check=False,
        )
        self.assertEqual(processo.returncode, 1)
        self.assertIn("não parece uma revisão", processo.stderr)
        self.assertFalse(Path("/tmp/nao-deve-existir").exists())

    def test_out_fora_do_repositorio_e_recusado(self):
        base = self._base({"app.py": "x = 1\n"})
        self._escrever("app.py", "x = 1\ny = 2\n")
        self._commitar("mudança")
        fora = self.dir.parent / "relatorio-fora.json"
        processo = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", base, "--out", str(fora)],
            cwd=self.dir, capture_output=True, text=True, check=False,
        )
        self.assertEqual(processo.returncode, 1)
        self.assertIn("fora do diretório analisado", processo.stdout)
        self.assertFalse(fora.exists())


if __name__ == "__main__":
    unittest.main()
