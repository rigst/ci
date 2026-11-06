#!/usr/bin/env python3
"""Merge automático dos PRs do Dependabot que só corrigem CVE.

Roda no workflow `automerge-dependabot.yml` (reutilizável), disparado quando o
CI de um PR do Dependabot termina VERDE. Faz o merge sozinho apenas se TODAS as
condições valerem; senão comenta no PR o motivo e deixa para revisão humana.

1. O PR é do Dependabot, em `dependabot/pip/...`, e o CI verde é do commit
   atual do PR (um run velho não serve).
2. Só mudam `requirements.txt` e `requirements.lock`: nenhuma linha de código,
   template, migração ou workflow.
3. É correção de segurança: o pip-audit do lock da main acha vulnerabilidade
   que o lock do PR não tem mais, e o lock do PR não traz nenhuma nova.
4. Toda dependência DIRETA que muda tinha vulnerabilidade na main. Isso separa
   o PR de segurança do PR semanal agrupado, que traz outras atualizações
   junto e continua indo para revisão.
5. Nenhum pacote (direto ou transitivo) muda de versão "maior": X.* -> Y.*,
   ou 0.X -> 0.Y, porque em 0.x a minor já quebra API. Nenhuma dependência
   direta entra ou sai.

O CI inteiro já passou (testes, e2e, mypy, Sonar etc.), e a proteção da main
continua valendo no merge, que exige o mesmo SHA auditado
(`--match-head-commit`).

Depois do merge, dispara o CI da main (workflow_dispatch): merge feito com o
GITHUB_TOKEN não gera evento de push, e sem CI na main o CD não roda. É a mesma
causa do arq #65. Se o PR estiver atrás da main (proteção "strict"), atualiza o
branch e dispara o CI nele; quando esse CI termina, este script roda de novo.

Variáveis: GH_TOKEN, GITHUB_REPOSITORY, HEAD_BRANCH, HEAD_SHA. `--seco` decide e
mostra tudo sem comentar, mergear nem disparar nada.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

REQUIREMENTS = "requirements.txt"
LOCK = "requirements.lock"
ARQUIVOS_PERMITIDOS = {REQUIREMENTS, LOCK}
MARCA = "<!-- automerge-cve -->"
AUTOR = "dependabot[bot]"
USO = (
    "uso: automerge_dependabot.py [--seco]  (lê GITHUB_REPOSITORY, HEAD_BRANCH, HEAD_SHA, GH_TOKEN)"
)


# ---------------------------------------------------------------- decisão pura


def nome_canonico(nome: str) -> str:
    return re.sub(r"[-_.]+", "-", nome).lower()


def pins(texto: str) -> dict[str, str]:
    """`pacote==versão` de um requirements/lock (ignora hashes, comentários, extras)."""
    achados = {}
    for linha in texto.splitlines():
        linha = linha.split("#", 1)[0].strip().rstrip("\\").strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;]+)", linha)
        if m:
            achados[nome_canonico(m.group(1))] = m.group(2)
    return achados


def faixa_maior(versao: str) -> tuple[str, str]:
    """O que não pode mudar: o major, ou major+minor quando o major é 0."""
    partes = re.findall(r"\d+", versao) or [versao]
    if partes[0] == "0":
        return ("0", partes[1] if len(partes) > 1 else "0")
    return (partes[0], "*")


def vulnerabilidades(auditoria: dict) -> set[tuple[str, frozenset[str]]]:
    """(pacote, ids) de cada vulnerabilidade do JSON do pip-audit, sem duplicar alias."""
    achados = set()
    for dep in auditoria.get("dependencies", []):
        for v in dep.get("vulns", []):
            ids = frozenset([v["id"], *v.get("aliases", [])])
            achados.add((nome_canonico(dep["name"]), ids))
    return achados


def _mesma(v: tuple[str, frozenset[str]], outras: set[tuple[str, frozenset[str]]]) -> bool:
    return any(v[0] == o[0] and v[1] & o[1] for o in outras)


def _checar_versoes(base: dict[str, str], head: dict[str, str], qual: str) -> list[str]:
    motivos = []
    for nome in sorted(base.keys() & head.keys()):
        if base[nome] != head[nome] and faixa_maior(base[nome]) != faixa_maior(head[nome]):
            motivos.append(f"{qual} `{nome}` muda de versão maior ({base[nome]} → {head[nome]})")
    return motivos


def decidir(
    arquivos: list[str],
    req_base: str,
    req_head: str,
    lock_base: str,
    lock_head: str,
    auditoria_base: dict,
    auditoria_head: dict,
) -> tuple[bool, list[str], list[str]]:
    """(pode_mergear, motivos_para_nao, resumo_do_que_corrige)."""
    motivos = []
    fora = sorted(set(arquivos) - ARQUIVOS_PERMITIDOS)
    if fora:
        motivos.append("muda arquivos além das dependências: " + ", ".join(f"`{a}`" for a in fora))

    diretas_base, diretas_head = pins(req_base), pins(req_head)
    if diretas_base.keys() != diretas_head.keys():
        entra = sorted(diretas_head.keys() - diretas_base.keys())
        sai = sorted(diretas_base.keys() - diretas_head.keys())
        motivos.append(f"dependência direta entra/sai (entra: {entra or '-'}; sai: {sai or '-'})")
    mudadas = sorted(
        n for n in diretas_base.keys() & diretas_head.keys() if diretas_base[n] != diretas_head[n]
    )

    motivos += _checar_versoes(diretas_base, diretas_head, "dependência direta")
    motivos += _checar_versoes(pins(lock_base), pins(lock_head), "pacote do lock")

    vul_base, vul_head = vulnerabilidades(auditoria_base), vulnerabilidades(auditoria_head)
    corrigidas = {v for v in vul_base if not _mesma(v, vul_head)}
    novas = {v for v in vul_head if not _mesma(v, vul_base)}
    if not corrigidas:
        motivos.append(
            "não corrige nenhuma vulnerabilidade conhecida do lock da main (não é PR de CVE)"
        )
    if novas:
        motivos.append(
            "o lock do PR traz vulnerabilidade nova: "
            + ", ".join(sorted(f"{p} {min(i)}" for p, i in novas))
        )
    pacotes_vulneraveis = {p for p, _ in vul_base}
    sem_cve = [n for n in mudadas if n not in pacotes_vulneraveis]
    if sem_cve:
        motivos.append(
            "atualiza dependência direta sem CVE na main (não é só correção): " + ", ".join(sem_cve)
        )

    resumo = [
        f"`{p}` {diretas_base.get(p, '')} → {diretas_head.get(p, '')}: " + ", ".join(sorted(i))
        for p, i in sorted(corrigidas, key=lambda v: (v[0], min(v[1])))
    ]
    return (not motivos, motivos, resumo)


# --------------------------------------------------------------- GitHub e git


def sh(*args: str, entrada: str | None = None, checar: bool = True) -> str:
    r = subprocess.run(args, capture_output=True, text=True, input=entrada)
    if checar and r.returncode:
        raise RuntimeError(f"{' '.join(args[:4])}…: {r.stderr.strip()[-400:]}")
    return r.stdout


def gh_json(*args: str):
    return json.loads(sh("gh", *args) or "null")


def auditar(lock: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".lock", delete=False) as f:
        f.write(lock)
    saida = f.name + ".json"
    subprocess.run(
        [
            "pip-audit",
            "-r",
            f.name,
            "--require-hashes",
            "--disable-pip",
            "--progress-spinner",
            "off",
            "--format",
            "json",
            "--output",
            saida,
        ],
        capture_output=True,
        text=True,
    )
    with open(saida, encoding="utf-8") as f:
        return json.load(f)


def comentar(repo: str, pr: int, texto: str, seco: bool) -> None:
    corpo = f"{MARCA}\n{texto}"
    print("--- comentário no PR ---\n" + texto)
    if seco:
        return
    existentes = gh_json("api", f"repos/{repo}/issues/{pr}/comments", "--paginate")
    meu = next((c for c in existentes if MARCA in c["body"]), None)
    if meu:
        sh(
            "gh",
            "api",
            "-X",
            "PATCH",
            f"repos/{repo}/issues/comments/{meu['id']}",
            "-f",
            f"body={corpo}",
        )
    else:
        sh("gh", "api", f"repos/{repo}/issues/{pr}/comments", "-f", f"body={corpo}")


def disparar_ci(repo: str, ref: str) -> None:
    sh("gh", "workflow", "run", "ci.yml", "--repo", repo, "--ref", ref)


def ci_verde(repo: str, sha: str) -> bool:
    """A execução mais recente do CI neste commit terminou com sucesso."""
    runs = gh_json("api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=20")["workflow_runs"]
    ci = next((r for r in runs if r["name"] == "CI"), None)  # mais nova primeiro
    return bool(ci) and ci["status"] == "completed" and ci["conclusion"] == "success"


def ci_da_main_rodando(repo: str, sha: str) -> bool:
    runs = gh_json("api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=20")["workflow_runs"]
    return any(r["name"] == "CI" for r in runs)


def processar(argv: list[str]) -> str:
    """Faz o que couber e devolve o desfecho em uma linha."""
    seco = "--seco" in argv
    repo, branch = os.environ["GITHUB_REPOSITORY"], os.environ["HEAD_BRANCH"]
    sha = os.environ.get("HEAD_SHA", "")
    dono = repo.split("/")[0]

    prs = gh_json("api", f"repos/{repo}/pulls?state=open&head={dono}:{branch}")
    if len(prs) != 1:
        return f"nenhum PR aberto (ou mais de um) para {branch}; nada a fazer"
    pr = prs[0]
    n = pr["number"]
    if pr["user"]["login"] != AUTOR:
        return f"PR #{n} não é do Dependabot; nada a fazer"
    if not sha:  # disparo à mão: vale o commit atual do PR, se o CI dele passou
        sha = pr["head"]["sha"]
    if pr["head"]["sha"] != sha:
        return f"CI verde é de {sha[:7]}, mas o PR #{n} já está em {pr['head']['sha'][:7]}; espero o próximo"
    if not ci_verde(repo, sha):
        return f"o CI de {sha[:7]} não terminou verde; nada a fazer"

    arquivos = [
        f["filename"] for f in gh_json("api", f"repos/{repo}/pulls/{n}/files", "--paginate")
    ]
    sh("git", "fetch", "-q", "origin", sha, "main")

    def ler(ref: str, caminho: str) -> str:
        return sh("git", "show", f"{ref}:{caminho}", checar=False)

    lock_base, lock_head = ler("origin/main", LOCK), ler(sha, LOCK)
    ok, motivos, resumo = decidir(
        arquivos,
        ler("origin/main", REQUIREMENTS),
        ler(sha, REQUIREMENTS),
        lock_base,
        lock_head,
        auditar(lock_base),
        auditar(lock_head),
    )
    print(f"PR #{n} ({sha[:7]}): {'MERGE' if ok else 'revisão humana'}")
    for linha in motivos + resumo:
        print("  -", linha)

    if not ok:
        comentar(
            repo,
            n,
            "**Auto-merge de CVE: não.** Fica para revisão humana, porque:\n\n"
            + "\n".join(f"- {m}" for m in motivos),
            seco,
        )
        return f"PR #{n}: revisão humana ({len(motivos)} motivo(s))"

    estado = gh_json("pr", "view", str(n), "--repo", repo, "--json", "mergeStateStatus")[
        "mergeStateStatus"
    ]
    if estado == "BEHIND":
        comentar(
            repo,
            n,
            "**Auto-merge de CVE:** o PR está atrás da main. Atualizei o branch e "
            "disparei o CI nele; o merge acontece quando ele passar.",
            seco,
        )
        if not seco:
            sh(
                "gh",
                "api",
                "-X",
                "PUT",
                f"repos/{repo}/pulls/{n}/update-branch",
                "-f",
                f"expected_head_sha={sha}",
            )
            time.sleep(5)
            disparar_ci(repo, branch)
        return f"PR #{n}: atrás da main; branch atualizado, decide de novo após o CI"

    if seco:
        return (
            f"[seco] PR #{n}: faria gh pr merge --merge --match-head-commit {sha} (estado {estado})"
        )
    sh(
        "gh",
        "pr",
        "merge",
        str(n),
        "--repo",
        repo,
        "--merge",
        "--delete-branch",
        "--match-head-commit",
        sha,
    )
    comentar(
        repo,
        n,
        "**Auto-merge de CVE: sim.** Só dependências mudaram, o CI inteiro passou e o "
        "PR corrige:\n\n" + "\n".join(f"- {r}" for r in resumo),
        seco,
    )

    merge_sha = gh_json("api", f"repos/{repo}/pulls/{n}")["merge_commit_sha"]
    time.sleep(30)
    if not ci_da_main_rodando(repo, merge_sha):
        disparar_ci(repo, "main")
        return f"PR #{n}: mergeado em {merge_sha[:7]}; CI da main disparado (merge pelo GITHUB_TOKEN não gera push)"
    return f"PR #{n}: mergeado em {merge_sha[:7]}; CI da main já rodando"


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print(USO)
        print((__doc__ or "").strip())
        return 0
    print(processar(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
