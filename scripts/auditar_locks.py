#!/usr/bin/env python3
"""Audita o lock da main de cada app e mantém uma issue por app no rigst/ci.

    python scripts/auditar_locks.py --app vetorial=relatorios/vetorial.json ...
    python scripts/auditar_locks.py --app vetorial=v.json --fail-on high --issues rigst/ci

Por que existe: o pip-audit do pipeline só roda em pull request. Uma CVE
publicada depois do merge fica invisível até alguém abrir um PR naquele app —
e o que está em produção é justamente o lock da main. Este script roda num
workflow agendado, sobre o `requirements.lock` da main de cada app (que é o
que o CD instala com `--require-hashes`), e:

1. junta os achados do pip-audit (um JSON por app, gerado pelo workflow);
2. descobre a severidade pelo advisory do GitHub (GHSA), que é a mesma régua
   do Dependabot — o pip-audit não informa severidade;
3. abre, atualiza ou fecha **uma** issue por app no repositório indicado, com
   rótulo fixo, para a pendência ter dono e histórico em vez de virar e-mail
   perdido;
4. sai com código 1 se algum app tiver achado na severidade de `--fail-on` ou
   acima — o workflow agendado fica vermelho e o GitHub avisa por e-mail.

O pip-audit às vezes devolve o mesmo advisory duas vezes para um pacote (uma
por id PYSEC, outra pelo alias). Os achados são agrupados pelo conjunto de
identificadores, então cada vulnerabilidade conta uma vez.
"""

import argparse
import json
import pathlib
import subprocess
import sys

ROTULO = "auditoria-dependencias"
ORDEM = {"low": 1, "medium": 2, "moderate": 2, "high": 3, "critical": 4}
# Sem GHSA não há severidade publicada. Conta como alta: melhor uma issue a
# mais do que uma CVE sem nota passar em silêncio.
SEM_NOTA = "high"


def argumentos(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--app", action="append", default=[], metavar="NOME=arquivo.json",
        help="Saída JSON do pip-audit de um app. Pode repetir.",
    )
    p.add_argument(
        "--fail-on", default="high", choices=["low", "medium", "high", "critical", "nunca"],
        help="Severidade mínima que faz o script sair com 1 (padrão: high).",
    )
    p.add_argument("--issues", default="", metavar="OWNER/REPO", help="Repositório onde manter as issues. Vazio: só relatório.")
    p.add_argument("--resumo", default="", help="Arquivo Markdown para o resumo (ex.: $GITHUB_STEP_SUMMARY).")
    return p.parse_args(argv)


def achados(dados):
    """Lista de achados únicos de uma saída do pip-audit."""
    deps = dados.get("dependencies", dados if isinstance(dados, list) else [])
    vistos = {}
    for dep in deps:
        for v in dep.get("vulns") or []:
            ids = frozenset([v["id"], *(v.get("aliases") or [])])
            chave = (dep["name"].lower(), dep.get("version"))
            repetido = any(c == chave and ids & outros for (c, outros), _ in vistos.items())
            if repetido:
                continue
            vistos[(chave, ids)] = {
                "pacote": dep["name"],
                "versao": dep.get("version", "?"),
                "id": v["id"],
                "ghsa": sorted(i for i in ids if i.startswith("GHSA-")),
                "cve": sorted(i for i in ids if i.startswith("CVE-")),
                "correcao": v.get("fix_versions") or [],
            }
    return list(vistos.values())


def severidade_github(ghsa):
    """Severidade do advisory global do GitHub; '' se não achar."""
    r = subprocess.run(["gh", "api", f"/advisories/{ghsa}", "--jq", ".severity"], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def classificar(lista, consultar=severidade_github):
    for a in lista:
        notas = [consultar(g) for g in a["ghsa"]]
        notas = [n for n in notas if n in ORDEM]
        a["severidade"] = max(notas, key=ORDEM.get) if notas else SEM_NOTA
        a["sem_nota"] = not notas
    return lista


def corpo_issue(app, lista):
    linhas = [
        f"Auditoria agendada do `requirements.lock` da main de **{app}** (o que o CD instala em produção).",
        "",
        "| severidade | pacote | versão | advisory | corrigido em |",
        "|---|---|---|---|---|",
    ]
    for a in sorted(lista, key=lambda x: (-ORDEM[x["severidade"]], x["pacote"], x["id"])):
        ref = ", ".join(a["ghsa"] or a["cve"] or [a["id"]])
        nota = a["severidade"] + (" (sem nota publicada)" if a["sem_nota"] else "")
        linhas.append(f"| {nota} | {a['pacote']} | {a['versao']} | {ref} | {', '.join(a['correcao']) or 'sem correção'} |")
    minimos = {}
    for a in lista:
        if a["correcao"]:
            alvo = max(a["correcao"], key=_chave_versao)
            atual = minimos.get(a["pacote"])
            minimos[a["pacote"]] = alvo if atual is None else max(atual, alvo, key=_chave_versao)
    if minimos:
        linhas += ["", "**Para zerar:** " + ", ".join(f"`{p}>={v}`" for p, v in sorted(minimos.items()))]
        linhas += ["", "Pacote transitivo também se fixa no `requirements.txt` e o lock é regenerado com `gerar_lock.py`."]
    linhas += ["", "_Atualizada sozinha pelo workflow `auditoria-dependencias` do rigst/ci; fecha quando o lock fica limpo._"]
    return "\n".join(linhas)


def _chave_versao(v):
    partes = []
    for p in v.replace("-", ".").split("."):
        partes.append((0, int(p), "") if p.isdigit() else (1, 0, p))
    return partes


def pior(lista):
    return max((a["severidade"] for a in lista), key=ORDEM.get, default="")


def titulo(app, lista):
    return f"[auditoria] {app}: {len(lista)} vulnerabilidade(s) no lock da main (pior: {pior(lista)})"


def _gh(*args, entrada=None):
    return subprocess.run(["gh", *args], input=entrada, capture_output=True, text=True, check=True).stdout


def issue_aberta(repo, app):
    saida = _gh("issue", "list", "-R", repo, "--label", ROTULO, "--state", "open", "--json", "number,title", "--limit", "100")
    for i in json.loads(saida):
        if i["title"].startswith(f"[auditoria] {app}:"):
            return i["number"]
    return None


def sincronizar_issue(repo, app, lista):
    numero = issue_aberta(repo, app)
    if lista:
        corpo = corpo_issue(app, lista)
        if numero:
            _gh("issue", "edit", str(numero), "-R", repo, "--title", titulo(app, lista), "--body-file", "-", entrada=corpo)
            return f"#{numero} atualizada"
        url = _gh("issue", "create", "-R", repo, "--label", ROTULO, "--title", titulo(app, lista), "--body-file", "-", entrada=corpo)
        return f"aberta {url.strip()}"
    if numero:
        _gh("issue", "close", str(numero), "-R", repo, "--comment", "Lock da main sem vulnerabilidade conhecida. Fechando.")
        return f"#{numero} fechada"
    return "nada a fazer"


def _dentro_da_pasta_atual(caminho):
    """Caminho resolvido se estiver dentro da pasta atual; None se não.

    O script só lê os JSON que o próprio workflow gravou em `auditoria/`.
    Recusar caminho fora da pasta de trabalho evita que um argumento mal
    formado faça ele ler (e publicar numa issue) um arquivo qualquer.
    """
    base = pathlib.Path.cwd().resolve()
    alvo = (base / caminho).resolve()
    return alvo if alvo.is_relative_to(base) else None


def main(argv=None):
    args = argumentos(argv)
    if not args.app:
        print("nenhum --app informado", file=sys.stderr)
        return 2
    limite = ORDEM.get(args.fail_on, 99)
    resumo = ["## Auditoria do lock da main", "", "| app | achados | pior | issue |", "|---|---|---|---|"]
    reprovados = []
    for item in args.app:
        app, _, caminho = item.partition("=")
        arquivo = _dentro_da_pasta_atual(caminho)
        if arquivo is None or not arquivo.is_file():
            # O workflow não conseguiu auditar (lock ausente, PyPI fora). Isso
            # é falha da auditoria, não "app limpo": não fecha issue nenhuma.
            resumo.append(f"| {app} | auditoria não rodou | ? | — |")
            reprovados.append(app)
            continue
        lista = classificar(achados(json.loads(arquivo.read_text(encoding="utf-8"))))
        estado = sincronizar_issue(args.issues, app, lista) if args.issues else "—"
        resumo.append(f"| {app} | {len(lista)} | {pior(lista) or 'limpo'} | {estado} |")
        print(f"{app}: {len(lista)} achado(s), pior {pior(lista) or 'nenhum'}; issue: {estado}")
        if lista and ORDEM[pior(lista)] >= limite:
            reprovados.append(app)
    texto = "\n".join(resumo) + "\n"
    if args.resumo:
        with open(args.resumo, "a", encoding="utf-8") as f:
            f.write(texto)
    if reprovados:
        print(f"::error::severidade >= {args.fail_on} ou auditoria falha em: {', '.join(reprovados)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
