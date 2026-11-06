#!/usr/bin/env python3
"""Confere, para cada app, se produção está no último commit da main.

Por que existe: em 06/11/2026 um merge na main do sistema_arq (#65) não gerou
evento de push. Sem CI na main não há CD (o CD pende do CI concluído), e o
servidor ficou para trás sem nenhum alerta. O mesmo já tinha acontecido no
sistema_vetorial em 02/09. Este script pega esse caso e os parecidos.

Para cada app em /var/www com remote rigst/<repo>:
  - main == produção                    -> ok, silêncio;
  - commit da main com menos de CARENCIA -> espera (CI/CD ainda rodando);
  - main sem NENHUMA execução de CI      -> dispara o CI (workflow_dispatch,
    o botão que o ci.yml dos apps tem exatamente para isso) e avisa;
  - CI/CD falhou ou terminou sem deploy  -> avisa, sem mexer em nada.

Só lê produção (git rev-parse) e a API do GitHub; a única ação é disparar o CI
da main, que passa pelos mesmos portões de sempre antes de qualquer deploy.

Cron (diário, 07:00 UTC):
  0 7 * * * /home/rod/scripts/ativos/confere_main_producao.py >/dev/null 2>&1 # confere_main_producao

Para testar sem e-mail nem disparo:  confere_main_producao.py --seco
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys

WWW = pathlib.Path("/var/www")
GH = os.environ.get("GH_BIN", "/home/rod/.local/bin/gh")
ALERT_TO = os.environ.get("ALERT_TO", "alertas@stolben.com")
CARENCIA_H = float(os.environ.get("CARENCIA_HORAS", "2"))
LOG = pathlib.Path("/home/rod/scripts/ativos/logs/confere_main_producao.log")
SECO = "--seco" in sys.argv


def agora() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def log(msg: str) -> None:
    linha = f"[{agora():%Y-%m-%dT%H:%M:%SZ}] {msg}"
    print(linha)
    if not SECO:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(linha + "\n")


def gh_api(caminho: str):
    r = subprocess.run([GH, "api", caminho], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"gh api {caminho}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def apps() -> list[tuple[str, str, pathlib.Path]]:
    """(nome, repo, diretório git) de cada app em produção."""
    achados = []
    for d in sorted(WWW.iterdir()):
        for g in (d / "current", d):
            if (g / ".git").exists():
                url = subprocess.run(
                    ["git", "-C", str(g), "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                m = re.search(r"github\.com[:/]rigst/([\w.-]+?)(?:\.git)?$", url)
                if m:
                    achados.append((d.name, m.group(1), g))
                break
    return achados


def conferir(nome: str, repo: str, git_dir: pathlib.Path) -> str | None:
    """Devolve a mensagem de problema, ou None se está tudo certo."""
    prod = subprocess.run(
        ["git", "-C", str(git_dir), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    commit = gh_api(f"repos/rigst/{repo}/commits/main")
    main = commit["sha"]
    if prod == main:
        log(f"ok {nome}: produção = main ({main[:7]})")
        return None

    quando = dt.datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
    idade_h = (agora() - quando).total_seconds() / 3600
    if idade_h < CARENCIA_H:
        log(f"espera {nome}: main {main[:7]} tem {idade_h:.1f}h (carência {CARENCIA_H}h)")
        return None

    runs = gh_api(f"repos/rigst/{repo}/actions/runs?head_sha={main}&per_page=50")["workflow_runs"]
    por_nome: dict[str, dict] = {}
    for r in runs:  # a API devolve do mais novo para o mais velho
        por_nome.setdefault(r["name"], r)
    ci, cd = por_nome.get("CI"), por_nome.get("CD")
    base = f"{nome}: produção em {prod[:7]}, main em {main[:7]} (há {idade_h:.0f}h)"

    if ci is None:
        if not SECO:
            subprocess.run(
                [GH, "workflow", "run", "ci.yml", "--repo", f"rigst/{repo}", "--ref", "main"],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        return (
            f"{base}. A main não tem NENHUMA execução de CI (merge sem evento de push). "
            f"{'CI disparado à mão (workflow_dispatch); o CD segue se ele passar.' if not SECO else '[seco: não disparei]'}"
        )
    if ci["status"] != "completed":
        return None if idade_h < 6 else f"{base}. CI parado em '{ci['status']}' há horas."
    if ci["conclusion"] != "success":
        return f"{base}. CI da main terminou em '{ci['conclusion']}': {ci['html_url']}"
    if cd is None:
        return f"{base}. CI passou, mas nenhum CD rodou para este commit."
    if cd["status"] != "completed":
        return None if idade_h < 6 else f"{base}. CD parado em '{cd['status']}' há horas."
    return (
        f"{base}. CD terminou em '{cd['conclusion']}' sem levar o commit ao servidor: "
        f"{cd['html_url']}"
    )


def alertar(problemas: list[str]) -> None:
    corpo = (
        "Produção divergente da main (confere_main_producao.py).\n\n"
        + "\n".join(f"- {p}" for p in problemas)
        + f"\n\nLog: {LOG}\n"
    )
    msg = (
        f"To: {ALERT_TO}\nFrom: rodrigo@stolben.com\n"
        f"Subject: [PRODUÇÃO ≠ MAIN] srv1553321 {agora():%Y-%m-%d}\n\n{corpo}"
    )
    if SECO:
        print("--- e-mail (seco, não enviado) ---\n" + msg)
        return
    subprocess.run(["msmtp", ALERT_TO], input=msg, text=True, timeout=60)


def main() -> int:
    problemas = []
    for nome, repo, g in apps():
        try:
            p = conferir(nome, repo, g)
        except Exception as e:  # um app com erro de API não pode calar os outros
            p = f"{nome}: não consegui conferir ({type(e).__name__}: {e})"
        if p:
            log("PROBLEMA " + p)
            problemas.append(p)
    if problemas:
        alertar(problemas)
        return 1
    log("tudo em dia")
    return 0


if __name__ == "__main__":
    sys.exit(main())
