"""Testes da decisão de auto-merge (scripts/automerge_dependabot.py)."""

import importlib.util
import pathlib
import unittest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("automerge", RAIZ / "scripts/automerge_dependabot.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ARQS = ["requirements.txt", "requirements.lock"]


def auditoria(*vulns):
    """vulns: (pacote, versão, id, [aliases])."""
    deps = {}
    for pacote, versao, vid, aliases in vulns:
        deps.setdefault(pacote, {"name": pacote, "version": versao, "vulns": []})
        deps[pacote]["vulns"].append({"id": vid, "aliases": aliases, "fix_versions": []})
    return {"dependencies": list(deps.values())}


VUL_REQUESTS = ("requests", "2.31.0", "PYSEC-2024-1", ["GHSA-9wx4-h78v-vm56", "CVE-2024-35195"])
REQ_BASE = "django==6.1.2\nrequests==2.31.0\n"
LOCK_BASE = "django==6.1.2 \\\n    --hash=sha256:aa\nrequests==2.31.0 \\\n    --hash=sha256:bb\nurllib3==2.2.1 \\\n"


class PinsTests(unittest.TestCase):
    def test_lock_com_hash_e_comentario(self):
        texto = "# gerado\nDjango==6.1.2 \\\n    --hash=sha256:aa\nzope.interface==7.0 ; python_version>'3'\n"
        self.assertEqual(mod.pins(texto), {"django": "6.1.2", "zope-interface": "7.0"})

    def test_extras(self):
        self.assertEqual(mod.pins("celery[redis]==5.4.0\n"), {"celery": "5.4.0"})


class FaixaTests(unittest.TestCase):
    def test_major(self):
        self.assertEqual(mod.faixa_maior("2.31.0"), mod.faixa_maior("2.32.3"))
        self.assertNotEqual(mod.faixa_maior("2.31.0"), mod.faixa_maior("3.0.0"))

    def test_zero_x_compara_a_minor(self):
        self.assertEqual(mod.faixa_maior("0.21.1"), mod.faixa_maior("0.21.4"))
        self.assertNotEqual(mod.faixa_maior("0.21.1"), mod.faixa_maior("0.22.0"))

    def test_sempre_tupla_de_dois(self):
        for v in ("2.31.0", "0.21.1", "7", "abc"):
            with self.subTest(v=v):
                self.assertEqual(len(mod.faixa_maior(v)), 2)

    def test_calver(self):
        self.assertEqual(mod.faixa_maior("2026.10.19"), mod.faixa_maior("2026.10.23"))


class DecidirTests(unittest.TestCase):
    def _decidir(self, req_head=None, lock_head=None, arquivos=ARQS, aud_base=None, aud_head=None):
        return mod.decidir(
            arquivos,
            REQ_BASE,
            req_head if req_head is not None else REQ_BASE.replace("2.31.0", "2.32.3"),
            LOCK_BASE,
            lock_head if lock_head is not None else LOCK_BASE.replace("2.31.0", "2.32.3"),
            aud_base if aud_base is not None else auditoria(VUL_REQUESTS),
            aud_head if aud_head is not None else auditoria(),
        )

    def test_correcao_de_cve_simples_mergeia(self):
        ok, motivos, resumo = self._decidir()
        self.assertTrue(ok, motivos)
        self.assertEqual(len(resumo), 1)
        self.assertIn("CVE-2024-35195", resumo[0])

    def test_transitiva_mudando_na_mesma_faixa_tudo_bem(self):
        lock = LOCK_BASE.replace("2.31.0", "2.32.3").replace("urllib3==2.2.1", "urllib3==2.2.3")
        ok, motivos, _ = self._decidir(lock_head=lock)
        self.assertTrue(ok, motivos)

    def test_arquivo_de_codigo_barra(self):
        ok, motivos, _ = self._decidir(arquivos=[*ARQS, "core/views.py"])
        self.assertFalse(ok)
        self.assertIn("core/views.py", motivos[0])

    def test_workflow_barra(self):
        ok, _, _ = self._decidir(arquivos=[".github/workflows/ci.yml"])
        self.assertFalse(ok)

    def test_sem_cve_na_main_e_atualizacao_comum(self):
        ok, motivos, _ = self._decidir(aud_base=auditoria())
        self.assertFalse(ok)
        self.assertTrue(any("não corrige nenhuma" in m for m in motivos))

    def test_pr_agrupado_com_outra_atualizacao_barra(self):
        req = REQ_BASE.replace("2.31.0", "2.32.3").replace("django==6.1.2", "django==6.1.3")
        lock = LOCK_BASE.replace("2.31.0", "2.32.3").replace("django==6.1.2", "django==6.1.3")
        ok, motivos, _ = self._decidir(req_head=req, lock_head=lock)
        self.assertFalse(ok)
        self.assertTrue(any("sem CVE na main" in m and "django" in m for m in motivos))

    def test_major_na_direta_barra(self):
        ok, motivos, _ = self._decidir(
            req_head=REQ_BASE.replace("2.31.0", "3.0.0"),
            lock_head=LOCK_BASE.replace("2.31.0", "3.0.0"),
        )
        self.assertFalse(ok)
        self.assertTrue(any("versão maior" in m for m in motivos))

    def test_major_em_transitiva_barra(self):
        lock = LOCK_BASE.replace("2.31.0", "2.32.3").replace("urllib3==2.2.1", "urllib3==3.0.0")
        ok, motivos, _ = self._decidir(lock_head=lock)
        self.assertFalse(ok)
        self.assertTrue(any("urllib3" in m for m in motivos))

    def test_vulnerabilidade_nova_barra(self):
        nova = ("urllib3", "2.2.1", "GHSA-nova", [])
        ok, motivos, _ = self._decidir(aud_head=auditoria(nova))
        self.assertFalse(ok)
        self.assertTrue(any("vulnerabilidade nova" in m for m in motivos))

    def test_cve_que_continua_nao_conta_como_corrigida(self):
        ok, motivos, _ = self._decidir(aud_head=auditoria(VUL_REQUESTS))
        self.assertFalse(ok)
        self.assertTrue(any("não corrige nenhuma" in m for m in motivos), motivos)

    def test_mesmo_cve_com_id_diferente_nao_e_novo(self):
        """O pip-audit às vezes troca o id principal (PYSEC x GHSA): alias conta."""
        outro = ("requests", "2.32.3", "GHSA-9wx4-h78v-vm56", ["CVE-2024-35195"])
        segundo = ("requests", "2.31.0", "PYSEC-2024-2", ["CVE-2024-9999"])
        ok, motivos, resumo = self._decidir(
            aud_base=auditoria(VUL_REQUESTS, segundo), aud_head=auditoria(outro)
        )
        # Corrige a segunda; a primeira continua (mesmo CVE, id trocado) e não é "nova".
        self.assertTrue(ok, motivos)
        self.assertEqual(len(resumo), 1)
        self.assertIn("CVE-2024-9999", resumo[0])

    def test_dependencia_direta_nova_barra(self):
        req = REQ_BASE.replace("2.31.0", "2.32.3") + "httpx==0.28.1\n"
        ok, motivos, _ = self._decidir(req_head=req)
        self.assertFalse(ok)
        self.assertTrue(any("entra" in m for m in motivos))


class HelpTests(unittest.TestCase):
    def test_help(self):
        self.assertEqual(mod.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
