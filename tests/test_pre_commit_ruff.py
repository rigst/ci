"""Testes do hook de pre-commit do ruff (scripts/pre_commit_ruff.py)."""

import importlib.util
import pathlib
import re
import tempfile
import unittest
from unittest import mock

RAIZ = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "pre_commit_ruff", RAIZ / "scripts/pre_commit_ruff.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class VersaoTests(unittest.TestCase):
    def test_versao_igual_a_do_ci(self):
        """Hook e CI na mesma versão: senão o pre-commit aprova e o CI reprova."""
        wf = (RAIZ / ".github/workflows/python-django.yml").read_text(encoding="utf-8")
        versao_ci = re.search(r'RUFF_VERSION:\s*"([0-9.]+)"', wf).group(1)
        self.assertEqual(mod.RUFF_VERSION, versao_ci)

    def test_config_ci_existe(self):
        self.assertTrue(mod.CONFIG_CI.is_file())

    def test_hooks_apontam_para_o_script(self):
        hooks = (RAIZ / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
        self.assertEqual(hooks.count("entry: scripts/pre_commit_ruff.py"), 2)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _cmd(self, sub="check"):
        with mock.patch.object(mod, "comando_ruff", return_value=["ruff"]):
            return mod.montar(sub, ["a.py"], self.raiz)

    def test_sem_config_propria_usa_a_do_ci(self):
        self.assertEqual(
            self._cmd(),
            ["ruff", "check", "--fix", "--force-exclude", "--config", str(mod.CONFIG_CI), "a.py"],
        )

    def test_format_usa_a_do_ci(self):
        self.assertEqual(
            self._cmd("format"),
            ["ruff", "format", "--force-exclude", "--config", str(mod.CONFIG_CI), "a.py"],
        )

    def test_force_exclude_mesmo_com_config_propria(self):
        """O exclude da config tem de valer para arquivo passado por nome."""
        (self.raiz / "ruff.toml").write_text("")
        for sub in ("check", "format"):
            with self.subTest(sub=sub):
                self.assertIn("--force-exclude", self._cmd(sub))

    def test_pyproject_com_tool_ruff_vale(self):
        (self.raiz / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
        self.assertNotIn("--config", self._cmd())

    def test_pyproject_sem_tool_ruff_nao_conta(self):
        (self.raiz / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        self.assertIn("--config", self._cmd())

    def test_ruff_toml_vale(self):
        for nome in ("ruff.toml", ".ruff.toml"):
            with self.subTest(nome=nome):
                f = self.raiz / nome
                f.write_text("")
                self.assertNotIn("--config", self._cmd())
                f.unlink()


class ComandoTests(unittest.TestCase):
    def test_prefere_uvx_com_versao_fixa(self):
        with mock.patch.object(
            mod.shutil, "which", side_effect=lambda n: "/x/uvx" if n == "uvx" else None
        ):
            self.assertEqual(mod.comando_ruff(), ["/x/uvx", f"ruff@{mod.RUFF_VERSION}"])

    def test_ruff_do_path_com_versao_errada_reprova(self):
        saida = mock.Mock(stdout="ruff 0.1.0\n")
        with (
            mock.patch.object(
                mod.shutil, "which", side_effect=lambda n: "/x/ruff" if n == "ruff" else None
            ),
            mock.patch.object(mod.subprocess, "run", return_value=saida),
        ):
            with self.assertRaises(SystemExit):
                mod.comando_ruff()

    def test_ruff_do_path_com_versao_certa_serve(self):
        saida = mock.Mock(stdout=f"ruff {mod.RUFF_VERSION}\n")
        with (
            mock.patch.object(
                mod.shutil, "which", side_effect=lambda n: "/x/ruff" if n == "ruff" else None
            ),
            mock.patch.object(mod.subprocess, "run", return_value=saida),
        ):
            self.assertEqual(mod.comando_ruff(), ["/x/ruff"])

    def test_help_responde_sem_rodar_o_ruff(self):
        with mock.patch.object(mod.subprocess, "run") as run:
            self.assertEqual(mod.main(["--help"]), 0)
        run.assert_not_called()

    def test_subcomando_invalido_sai_2(self):
        self.assertEqual(mod.main(["lint"]), 2)

    def test_sem_arquivos_nao_roda_nada(self):
        with mock.patch.object(mod.subprocess, "run") as run:
            self.assertEqual(mod.main(["check"]), 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
