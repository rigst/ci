"""Testes do medidor de layout.

Cobre a checagem de status HTTP, que é a guarda contra o modo de falha mais
caro que este pipeline já teve: um projeto com `ALLOWED_HOSTS` sem `127.0.0.1`
devolve `DisallowedHost` em toda rota, e a página de erro do Django **tem**
layout — tabelas largas de `META` que estouram qualquer viewport de celular.

Sem a guarda, o job media a tela de erro e relatava cinco "quebras" por rota,
idênticas entre três projetos diferentes. Idênticas porque eram a mesma página
de erro. O relatório vinha cheio, o job não reclamava de nada, e os seletores
acusados (`table.meta`, `table.req`) não existem em nenhum dos projetos.

O resto do `layout.py` roda dentro do navegador, e é exercitado por páginas
sintéticas — aqui fica o que dá para testar sem subir Chromium.
"""

import importlib.util
import pathlib
import unittest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("layout", RAIZ / "scripts" / "layout.py")
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


class Resposta:
    def __init__(self, status):
        self.status = status


class ConferirStatusTests(unittest.TestCase):
    def test_200_passa(self):
        layout.conferir_status(Resposta(200), "http://127.0.0.1/")

    def test_302_passa(self):
        """Redirecionamento é resposta legítima: o Playwright segue e o status
        final é o que chega aqui. Um 3xx que sobrar não é erro de página."""
        layout.conferir_status(Resposta(302), "http://127.0.0.1/")

    def test_400_reprova_e_cita_allowed_hosts(self):
        with self.assertRaises(layout.RespostaDeErro) as ctx:
            layout.conferir_status(Resposta(400), "http://127.0.0.1/login/")
        self.assertIn("ALLOWED_HOSTS", str(ctx.exception))
        self.assertIn("400", str(ctx.exception))

    def test_404_reprova(self):
        with self.assertRaises(layout.RespostaDeErro):
            layout.conferir_status(Resposta(404), "http://127.0.0.1/termos/")

    def test_500_reprova(self):
        with self.assertRaises(layout.RespostaDeErro):
            layout.conferir_status(Resposta(500), "http://127.0.0.1/")

    def test_sem_resposta_nao_reprova(self):
        """Navegação que não produz resposta HTTP (about:blank, file://) não é
        erro de página — só não há status a conferir."""
        layout.conferir_status(None, "about:blank")


class CaminhoNoRepositorioTests(unittest.TestCase):
    def test_recusa_caminho_fora_da_arvore(self):
        self.assertIsNone(layout.caminho_no_repositorio("../../etc/passwd"))

    def test_aceita_caminho_interno(self):
        self.assertIsNotNone(layout.caminho_no_repositorio("relatorio.json"))


if __name__ == "__main__":
    unittest.main()
