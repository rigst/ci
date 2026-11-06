"""Testes de `scripts/conferir_headers.py` (sem rede: avalia dicionários de cabeçalhos)."""

import subprocess
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

import conferir_headers as ch  # noqa: E402

COMPLETO = {
    "strict-transport-security": "max-age=31536000; includeSubDomains",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'self'; script-src 'self' 'nonce-abc'; object-src 'none'",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=()",
}


def severidades(cabecalhos):
    return sorted(s for s, _ in ch.avaliar(cabecalhos))


class AvaliarTests(unittest.TestCase):
    def test_conjunto_completo_nao_tem_achado(self):
        self.assertEqual(ch.avaliar(COMPLETO), [])

    def test_site_sem_nenhum_cabecalho(self):
        # O caso do stolben.com em 05/11/2026.
        achados = ch.avaliar({})
        self.assertEqual([s for s, _ in achados].count("erro"), 3)
        self.assertEqual([s for s, _ in achados].count("aviso"), 3)

    def test_hsts_curto_e_erro(self):
        c = {**COMPLETO, "strict-transport-security": "max-age=300"}
        self.assertEqual(severidades(c), ["erro"])

    def test_frame_ancestors_substitui_x_frame_options(self):
        c = {k: v for k, v in COMPLETO.items() if k != "x-frame-options"}
        c["content-security-policy"] += "; frame-ancestors 'none'"
        self.assertEqual(ch.avaliar(c), [])

    def test_sem_frame_nenhum_e_erro(self):
        c = {k: v for k, v in COMPLETO.items() if k != "x-frame-options"}
        self.assertEqual(severidades(c), ["erro"])

    def test_unsafe_eval_e_aviso(self):
        c = {**COMPLETO, "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-eval'"}
        self.assertEqual(severidades(c), ["aviso"])

    def test_sem_permissions_policy_e_aviso(self):
        c = {k: v for k, v in COMPLETO.items() if k != "permissions-policy"}
        self.assertEqual(severidades(c), ["aviso"])

    def test_nosniff_com_maiuscula_vale(self):
        c = {**COMPLETO, "x-content-type-options": "NoSniff"}
        self.assertEqual(ch.avaliar(c), [])


class LinhaDeComandoTests(unittest.TestCase):
    def test_recusa_http(self):
        r = subprocess.run([sys.executable, str(RAIZ / "scripts" / "conferir_headers.py"), "http://exemplo"],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("só https", r.stdout)


if __name__ == "__main__":
    unittest.main()
