#!/usr/bin/env python3
"""Relatório periódico da frota: churn histórico e duplicação entre repositórios.

    python scripts/frota.py --repo trilhas=/caminho/trilhas --repo dojo=/caminho/dojo

Este script existe fora do caminho do pull request, e é uma decisão, não uma
limitação. Nenhuma das duas medidas decide alguma coisa num PR:

- **Churn não é defeito.** Um arquivo alterado quarenta vezes pode ser o mais
  vivo do sistema ou o mais remendado; a diferença está no motivo das
  alterações, e nenhum número decide isso. Bloquear por churn puniria trabalho
  legítimo. O que o número serve é para ordenar a fila de refatoração.

- **Duplicação entre repositórios não é visível de dentro de um.** O job de um
  PR tem um repositório em mãos; os outros oito não estão lá. Rodar aqui, com
  todos clonados, é a única forma de responder "esta função também existe em
  mais três projetos".

Rodar isto em cada PR custaria `fetch-depth: 0` mais oito clones por execução,
para produzir um número que não muda de um PR para o outro.

O sinal que interessa é o cruzamento: **arquivo grande + muito alterado + com
alta proporção de commits de conserto**. Um arquivo assim é onde o próximo
defeito vai aparecer, e é o candidato a ser dividido antes disso.
"""

import argparse
import ast
import hashlib
import os
import pathlib
import re
import subprocess
import sys
from collections import defaultdict

# Assunto de commit que declara conserto. A proporção destes sobre o total é o
# que separa "arquivo ativo" de "arquivo remendado".
CONSERTO = re.compile(
    r"\b(fix|fixes|hotfix|patch|bug|corrige|corrigir|conserta|ajusta|ajuste|"
    r"revert|workaround|contorna)\b",
    re.IGNORECASE,
)

IGNORADOS = re.compile(
    r"(^|/)(\.git|venv|\.venv|node_modules|staticfiles|__pycache__|migrations)(/|$)"
)

# Função menor que isto não diz nada sobre duplicação: `def __str__(self):
# return self.nome` é igual em toda a frota e não é código repetido, é Django.
LINHAS_MINIMAS_PARA_DUPLICATA = 12

# Arquivos que o próprio framework gera iguais em todo projeto. O `main` do
# manage.py apareceu em sete repositórios na primeira execução — é verdade, e é
# inútil: ninguém vai extrair o boilerplate do django-admin para uma biblioteca.
# Achado que não gera ação nenhuma treina quem lê o relatório a ignorá-lo.
BOILERPLATE = re.compile(r"(^|/)(manage\.py|wsgi\.py|asgi\.py|conftest\.py)$")


def rodar(args, cwd):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, errors="replace", check=False
    ).stdout


# --------------------------------------------------------------------------
# churn
# --------------------------------------------------------------------------

def medir_churn(caminho):
    """Uma passada só no log. `--numstat` com `--format` devolve o assunto do
    commit e as linhas por arquivo no mesmo fluxo, o que evita um `git log` por
    arquivo — que numa frota de nove repositórios seria lento sem necessidade."""
    saida = rodar(
        ["git", "log", "--no-merges", "--numstat", "--format=%x01%s"], caminho
    )
    arquivos = defaultdict(lambda: {"commits": 0, "add": 0, "rem": 0, "consertos": 0})
    eh_conserto = False
    for linha in saida.splitlines():
        if linha.startswith("\x01"):
            eh_conserto = bool(CONSERTO.search(linha[1:]))
            continue
        partes = linha.split("\t")
        if len(partes) != 3:
            continue
        add, rem, arquivo = partes
        if IGNORADOS.search(arquivo) or not arquivo.endswith((".py", ".css", ".js", ".html")):
            continue
        dados = arquivos[arquivo]
        dados["commits"] += 1
        dados["consertos"] += eh_conserto
        # Arquivo binário vem com "-" no lugar do número.
        dados["add"] += int(add) if add.isdigit() else 0
        dados["rem"] += int(rem) if rem.isdigit() else 0
    return arquivos


def tamanho_atual(caminho, arquivo):
    alvo = pathlib.Path(caminho) / arquivo
    if not alvo.is_file():
        return 0  # apagado ou renomeado desde então
    try:
        return len(alvo.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def ranking_de_risco(projetos):
    """tamanho x churn x proporção de consertos, cada fator normalizado.

    Nenhum dos três sozinho identifica dívida: arquivo grande pode ser estável,
    arquivo muito alterado pode ser pequeno e saudável, e proporção alta de
    consertos num arquivo de dez linhas é ruído estatístico. O produto só fica
    alto quando os três estão altos ao mesmo tempo."""
    linhas = []
    for projeto, caminho in projetos.items():
        churn = medir_churn(caminho)
        for arquivo, dados in churn.items():
            tamanho = tamanho_atual(caminho, arquivo)
            if tamanho < 100 or dados["commits"] < 5:
                continue
            proporcao = dados["consertos"] / dados["commits"]
            risco = tamanho * dados["commits"] * (0.25 + proporcao)
            linhas.append({
                "projeto": projeto,
                "arquivo": arquivo,
                "linhas": tamanho,
                "commits": dados["commits"],
                "consertos": dados["consertos"],
                "proporcao": proporcao,
                "risco": risco,
            })
    linhas.sort(key=lambda item: -item["risco"])
    return linhas


# --------------------------------------------------------------------------
# duplicação entre repositórios
# --------------------------------------------------------------------------

LITERAL = re.compile(r"(\"\"\".*?\"\"\"|'''.*?'''|\"[^\"]*\"|'[^']*')", re.S)
COMENTARIO = re.compile(r"#.*$", re.M)
ESPACO = re.compile(r"\s+")


def impressao_digital(fonte):
    """Normaliza antes de comparar: literais de texto e comentários viram
    marcadores, e o espaço em branco some.

    Sem isso, duas cópias da mesma função que divergiram só numa mensagem de erro
    contariam como código diferente — e é exatamente assim que a duplicação
    sobrevive à revisão, divergindo aos poucos até ninguém mais reconhecer que
    era a mesma coisa."""
    texto = COMENTARIO.sub("", fonte)
    texto = LITERAL.sub("«t»", texto)
    texto = ESPACO.sub(" ", texto).strip()
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


def funcoes_do_projeto(projeto, caminho):
    achadas = []
    raiz = pathlib.Path(caminho)
    for arquivo in raiz.rglob("*.py"):
        relativo = str(arquivo.relative_to(raiz))
        if IGNORADOS.search(relativo) or BOILERPLATE.search(relativo):
            continue
        try:
            fonte = arquivo.read_text(encoding="utf-8")
            arvore = ast.parse(fonte)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        linhas = fonte.splitlines()
        for no in ast.walk(arvore):
            if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fim = getattr(no, "end_lineno", no.lineno)
            if fim - no.lineno + 1 < LINHAS_MINIMAS_PARA_DUPLICATA:
                continue
            corpo = "\n".join(linhas[no.lineno - 1:fim])
            achadas.append({
                "projeto": projeto,
                "arquivo": relativo,
                "nome": no.name,
                "linhas": fim - no.lineno + 1,
                "digital": impressao_digital(corpo),
            })
    return achadas


def duplicatas_entre_repos(projetos):
    por_digital = defaultdict(list)
    for projeto, caminho in projetos.items():
        for funcao in funcoes_do_projeto(projeto, caminho):
            por_digital[funcao["digital"]].append(funcao)

    grupos = []
    for ocorrencias in por_digital.values():
        projetos_envolvidos = {o["projeto"] for o in ocorrencias}
        if len(projetos_envolvidos) < 2:
            continue  # duplicação dentro de um projeto é trabalho do Sonar
        grupos.append({
            "nome": ocorrencias[0]["nome"],
            "linhas": ocorrencias[0]["linhas"],
            "projetos": sorted(projetos_envolvidos),
            "ocorrencias": sorted(
                (f"{o['projeto']}:{o['arquivo']}" for o in ocorrencias)
            ),
        })
    grupos.sort(key=lambda g: (-len(g["projetos"]), -g["linhas"]))
    return grupos


# --------------------------------------------------------------------------

def escrever(linhas):
    texto = "\n".join(linhas)
    print(texto)
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if destino:
        with open(destino, "a", encoding="utf-8") as f:
            f.write(texto + "\n")


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="NOME=CAMINHO",
        help="Projeto a analisar. Pode repetir.",
    )
    p.add_argument("--top", type=int, default=20, help="Quantos arquivos no ranking.")
    return p.parse_args()


def main():
    args = argumentos()
    projetos = {}
    for entrada in args.repo:
        if "=" not in entrada:
            print(f"--repo espera NOME=CAMINHO, recebi {entrada!r}", file=sys.stderr)
            return 1
        nome, caminho = entrada.split("=", 1)
        if not (pathlib.Path(caminho) / ".git").exists():
            print(f"::warning::{nome} não parece um clone git ({caminho}); pulando.")
            continue
        projetos[nome] = caminho

    if not projetos:
        print("::error::nenhum repositório utilizável foi informado.")
        return 1

    linhas = [f"# Frota — {len(projetos)} projeto(s)", ""]

    risco = ranking_de_risco(projetos)
    linhas += [
        "## Candidatos a refatoração",
        "",
        "Cruzamento de tamanho, número de alterações e proporção de commits de",
        "conserto. Churn não é defeito: é a fila de prioridade, não um veredito.",
        "",
        "| projeto | arquivo | linhas | commits | consertos | % conserto |",
        "|---|---|---|---|---|---|",
    ]
    for item in risco[:args.top]:
        linhas.append(
            f"| {item['projeto']} | `{item['arquivo']}` | {item['linhas']} "
            f"| {item['commits']} | {item['consertos']} | {item['proporcao']:.0%} |"
        )
    if not risco:
        linhas.append("| — | nenhum arquivo cruzou os limites mínimos | | | | |")

    grupos = duplicatas_entre_repos(projetos)
    linhas += [
        "",
        "## Duplicação entre repositórios",
        "",
        f"{len(grupos)} função(ões) idêntica(s) em mais de um projeto, comparadas",
        "com literais de texto e comentários normalizados.",
        "",
        "A remoção está fora do escopo do CI — não há biblioteca compartilhada",
        "entre os projetos, e criá-la é decisão de arquitetura. O que este",
        "relatório garante é que o número seja conhecido e não cresça sem que",
        "ninguém perceba.",
        "",
    ]
    if grupos:
        linhas += ["| função | linhas | projetos | onde |", "|---|---|---|---|"]
        for g in grupos[:args.top]:
            onde = ", ".join(f"`{o}`" for o in g["ocorrencias"][:4])
            if len(g["ocorrencias"]) > 4:
                onde += f" (+{len(g['ocorrencias']) - 4})"
            linhas.append(
                f"| `{g['nome']}` | {g['linhas']} | {len(g['projetos'])} | {onde} |"
            )
    else:
        linhas.append("Nenhuma função duplicada entre projetos.")

    escrever(linhas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
