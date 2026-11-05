"""Testes da auditoria agendada dos locks (`scripts/auditar_locks.py`).

Sem rede: a severidade é injetada e as issues não são tocadas (sem --issues).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCRIPT = RAIZ / "scripts" / "auditar_locks.py"
sys.path.insert(0, str(RAIZ / "scripts"))

import auditar_locks as al  # noqa: E402


def saida(*deps):
    return {"dependencies": list(deps), "fixes": []}


def dep(nome, versao, *vulns):
    return {"name": nome, "version": versao, "vulns": list(vulns)}


def vuln(id_, aliases=(), fix=("9.9",)):
    return {"id": id_, "aliases": list(aliases), "fix_versions": list(fix), "description": ""}


# Formato real do pip-audit 2.10 sobre o lock do sistema_arq: o mesmo advisory
# do brotli vem duas vezes, uma por id, com os aliases cruzados.
BROTLI_DUPLICADO = saida(
    dep("brotli", "1.2.0",
        vuln("PYSEC-2026-4322", ["GHSA-9v8h-qqgm-4m35", "PYSEC-2026-4323", "CVE-2026-111497"], ["1.2.1"]),
        vuln("PYSEC-2026-4322", ["GHSA-9v8h-qqgm-4m35", "CVE-2026-111497", "PYSEC-2026-4323"], ["1.2.1"])),
    dep("django", "6.1.1"),
)


class AchadosTests(unittest.TestCase):
    def test_advisory_repetido_conta_uma_vez(self):
        lista = al.achados(BROTLI_DUPLICADO)
        self.assertEqual(len(lista), 1)
        self.assertEqual(lista[0]["ghsa"], ["GHSA-9v8h-qqgm-4m35"])
        self.assertEqual(lista[0]["cve"], ["CVE-2026-111497"])

    def test_advisories_diferentes_do_mesmo_pacote_contam_separado(self):
        dados = saida(dep("pypdf", "6.20.1", vuln("CVE-1", ["GHSA-a"]), vuln("CVE-2", ["GHSA-b"])))
        self.assertEqual(len(al.achados(dados)), 2)

    def test_lock_limpo(self):
        self.assertEqual(al.achados(saida(dep("django", "6.1.1"))), [])


class ClassificarTests(unittest.TestCase):
    def test_pega_a_pior_nota_entre_os_ghsa(self):
        lista = al.achados(saida(dep("x", "1", vuln("CVE-1", ["GHSA-a", "GHSA-b"]))))
        notas = {"GHSA-a": "medium", "GHSA-b": "critical"}
        al.classificar(lista, consultar=notas.get)
        self.assertEqual(lista[0]["severidade"], "critical")

    def test_sem_ghsa_conta_como_alta(self):
        lista = al.achados(saida(dep("x", "1", vuln("PYSEC-1"))))
        al.classificar(lista, consultar=lambda g: "")
        self.assertEqual(lista[0]["severidade"], "high")
        self.assertTrue(lista[0]["sem_nota"])


class CorpoTests(unittest.TestCase):
    def test_versao_minima_para_zerar_e_a_maior_correcao(self):
        dados = saida(dep("pypdf", "6.20.1",
                          vuln("CVE-1", ["GHSA-a"], ["6.21.0"]),
                          vuln("CVE-2", ["GHSA-b"], ["6.21.3"]),
                          vuln("CVE-3", ["GHSA-c"], ["6.9.0"])))
        lista = al.classificar(al.achados(dados), consultar=lambda g: "high")
        corpo = al.corpo_issue("divisor", lista)
        self.assertIn("`pypdf>=6.21.3`", corpo)
        self.assertEqual(al.titulo("divisor", lista), "[auditoria] divisor: 3 vulnerabilidade(s) no lock da main (pior: high)")


class LinhaDeComandoTests(unittest.TestCase):
    def _rodar(self, *apps, fail_on="high"):
        pasta = Path(tempfile.mkdtemp())
        args = []
        for nome, dados in apps:
            arquivo = pasta / f"{nome}.json"
            if dados is not None:
                arquivo.write_text(json.dumps(dados), encoding="utf-8")
            args += ["--app", f"{nome}={arquivo.name}"]
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--fail-on", fail_on],
                              capture_output=True, text=True, check=False, cwd=pasta)

    def test_tudo_limpo_sai_zero(self):
        r = self._rodar(("a", saida(dep("django", "6.1.1"))), ("b", saida()))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_auditoria_que_nao_rodou_reprova_em_vez_de_passar_como_limpa(self):
        r = self._rodar(("a", saida()), ("b", None))
        self.assertEqual(r.returncode, 1)
        self.assertIn("b", r.stdout)

    def test_fail_on_nunca_so_relata(self):
        # Sem GHSA não há consulta de rede: a nota cai em "high" sozinha.
        r = self._rodar(("a", saida(dep("x", "1", vuln("PYSEC-1")))), fail_on="nunca")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_caminho_fora_da_pasta_de_trabalho_nao_e_lido(self):
        pasta = Path(tempfile.mkdtemp())
        fora = Path(tempfile.mkdtemp()) / "limpo.json"
        fora.write_text(json.dumps(saida()), encoding="utf-8")
        r = subprocess.run([sys.executable, str(SCRIPT), "--app", f"a={fora}"],
                           capture_output=True, text=True, check=False, cwd=pasta)
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::", r.stdout)

    def test_achado_alto_reprova(self):
        r = self._rodar(("a", saida(dep("x", "1", vuln("PYSEC-1")))))
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::", r.stdout)


if __name__ == "__main__":
    unittest.main()
