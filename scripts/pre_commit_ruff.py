#!/usr/bin/env python3
"""Hook de pre-commit: ruff com a MESMA versão e a MESMA config do CI.

Usado pelo `.pre-commit-hooks.yaml` deste repo. Nos apps:

    repos:
      - repo: https://github.com/rigst/ci
        rev: <sha>
        hooks:
          - id: ruff-ci
          - id: ruff-format-ci

Por que não o `astral-sh/ruff-pre-commit` direto: sem a config compartilhada
o ruff roda no padrão e acusa centenas de violações legadas que o CI não
cobra; copiar o `configs/ruff.toml` para cada app faria o CI passar a ler a
cópia (ele prefere a config do projeto) e as duas divergiriam. Aqui a config
vem do próprio checkout deste repo que o pre-commit clona, no mesmo `rev`.

Mesma regra do job `ruff` do python-django.yml: se o app tiver config própria
(`[tool.ruff]` no pyproject, `ruff.toml` ou `.ruff.toml`), ela vale.

O ruff roda via `uvx ruff@VERSAO` (sem instalar nada no projeto). Sem `uvx`,
usa um `ruff` do PATH, desde que seja exatamente a versão do CI.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

# Tem de ser igual ao RUFF_VERSION do .github/workflows/python-django.yml;
# tests/test_pre_commit_ruff.py reprova se divergir.
RUFF_VERSION = "0.16.7"
CONFIG_CI = pathlib.Path(__file__).resolve().parent.parent / "configs" / "ruff.toml"


def config_do_projeto(raiz: pathlib.Path) -> bool:
    pyproject = raiz / "pyproject.toml"
    if pyproject.is_file() and "[tool.ruff" in pyproject.read_text(encoding="utf-8"):
        return True
    return (raiz / "ruff.toml").is_file() or (raiz / ".ruff.toml").is_file()


def comando_ruff() -> list[str]:
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, f"ruff@{RUFF_VERSION}"]
    ruff = shutil.which("ruff")
    if ruff:
        saida = subprocess.run([ruff, "--version"], capture_output=True, text=True).stdout
        if saida.split()[-1:] == [RUFF_VERSION]:
            return [ruff]
        raise SystemExit(
            f"ruff do PATH é '{saida.strip()}', o CI usa {RUFF_VERSION}. "
            "Instale o uv (https://docs.astral.sh/uv/) ou a versão certa do ruff."
        )
    raise SystemExit(
        f"Nem uvx nem ruff no PATH. Instale o uv (https://docs.astral.sh/uv/) "
        f"ou `pip install ruff=={RUFF_VERSION}`."
    )


def montar(subcomando: str, arquivos: list[str], raiz: pathlib.Path) -> list[str]:
    # --force-exclude: o pre-commit passa os arquivos por nome, e o ruff só
    # aplica o `exclude` da config (migrations/, venv/...) a caminho explícito
    # com essa flag. Sem ela o hook reprova migração que o CI nunca olha.
    cmd = comando_ruff() + (["check", "--fix"] if subcomando == "check" else ["format"])
    cmd.append("--force-exclude")
    if not config_do_projeto(raiz):
        cmd += ["--config", str(CONFIG_CI)]
    return cmd + arquivos


USO = "uso: pre_commit_ruff.py {check|format} [arquivos...]"


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print(USO)
        print((__doc__ or "").strip())
        return 0
    if not argv or argv[0] not in ("check", "format"):
        print(USO, file=sys.stderr)
        return 2
    arquivos = argv[1:]
    if not arquivos:
        return 0
    return subprocess.run(montar(argv[0], arquivos, pathlib.Path(os.getcwd()))).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
