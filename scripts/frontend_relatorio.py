#!/usr/bin/env python3
"""Junta Stylelint, ESLint e djlint num veredito e num resumo só.

    python scripts/frontend_relatorio.py --stylelint stylelint.json \
        --eslint eslint.json --djlint djlint.txt --fail-on erro

As três ferramentas relatam de formas diferentes — duas em JSON com esquemas
distintos, uma em texto — e nenhuma delas escreve anotação do GitHub Actions
ancorada em arquivo e linha. Sem esta camada, o resultado do frontend vira três
blocos de log que ninguém abre, e o veredito depende de somar códigos de saída
no shell.

Duas armadilhas medidas na prática, que este script contorna:

1. **O Stylelint escreve o relatório em stderr quando encontra algo.** Um
   `2>/dev/null` no passo do workflow apaga exatamente o caso que interessa e
   devolve silêncio, que parece sucesso. Por isso o workflow usa
   `--output-file` e este script lê o arquivo, não o pipe.

2. **A severidade de cada ferramenta significa coisas diferentes.** Tudo que o
   Stylelint chama de `error` aqui é dívida de CSS herdada, não defeito de
   runtime; já um `no-undef` do ESLint quebra a página. O mapeamento está
   explícito abaixo, e não implícito no código de saída de cada binário.
"""

import argparse
import json
import os
import pathlib
import re
import sys
from collections import defaultdict

SEVERIDADES = ["aviso", "erro"]

# Regras do djlint que descrevem defeito de comportamento ou de estrutura, e não
# dívida de marcação. `button` sem `type` submete o formulário sem ninguém pedir;
# tag órfã quebra a árvore do documento. O resto do conjunto habilitado (H020,
# par de tags vazio) é limpeza, e entra como aviso.
DJLINT_BLOQUEANTES = {"H025", "H043", "T001", "T003"}

LINHA_DJLINT = re.compile(r"^([HTDJ]\d+)\s+(\d+):(\d+)\s+(.*)$")


class Achado:
    def __init__(self, ferramenta, arquivo, linha, regra, severidade, mensagem):
        self.ferramenta = ferramenta
        self.arquivo = arquivo
        self.linha = linha
        self.regra = regra
        self.severidade = severidade
        self.mensagem = mensagem

    def como_dicionario(self):
        return {
            "ferramenta": self.ferramenta,
            "arquivo": self.arquivo,
            "linha": self.linha,
            "regra": self.regra,
            "severidade": self.severidade,
            "mensagem": self.mensagem,
        }


def relativo(caminho):
    """Caminho relativo à raiz do checkout: é o que a anotação do Actions precisa
    para ancorar o achado em 'Files changed'. Absoluto não ancora em nada."""
    try:
        return str(pathlib.Path(caminho).resolve().relative_to(pathlib.Path.cwd()))
    except ValueError:
        return caminho


def ler_stylelint(caminho):
    achados = []
    dados = json.loads(pathlib.Path(caminho).read_text(encoding="utf-8"))
    for arquivo in dados:
        for aviso in arquivo.get("warnings", []):
            achados.append(Achado(
                "stylelint",
                relativo(arquivo["source"]),
                aviso.get("line", 1),
                aviso.get("rule") or "stylelint",
                # Tudo que o Stylelint marca como error é, nestes projetos, CSS
                # herdado: seletor duplicado, atalho sobrescrevendo longhand. É
                # dívida a conter, não build a derrubar — o que bloqueia é o
                # `!important` NOVO, e isso quem cobra é o diff_quality.py.
                "aviso",
                aviso.get("text", "").strip(),
            ))
    return achados


def ler_eslint(caminho):
    achados = []
    dados = json.loads(pathlib.Path(caminho).read_text(encoding="utf-8"))
    for arquivo in dados:
        for msg in arquivo.get("messages", []):
            achados.append(Achado(
                "eslint",
                relativo(arquivo["filePath"]),
                msg.get("line", 1),
                msg.get("ruleId") or "eslint",
                # severity 2 do ESLint é erro de verdade: variável inexistente,
                # código inalcançável, typeof inválido. A frota inteira está em
                # zero, então isto pode bloquear desde o primeiro dia.
                "erro" if msg.get("severity") == 2 else "aviso",
                msg.get("message", "").strip(),
            ))
    return achados


def ler_djlint(caminho, raizes=()):
    """O djlint não tem saída JSON no modo lint; a saída é um cabeçalho com o
    nome do arquivo seguido das linhas `CODIGO linha:coluna mensagem`.

    O cabeçalho vem relativo ao diretório que o djlint recebeu, e não à raiz do
    repositório: rodando sobre `templates`, o arquivo
    `templates/trilhas/topico.html` aparece como `trilhas/topico.html`. Anotação
    com esse caminho não ancora em lugar nenhum do PR, então `raizes` traz os
    diretórios passados ao djlint e a resolução testa qual deles existe."""
    def resolver(relativo_ao_djlint):
        if pathlib.Path(relativo_ao_djlint).is_file():
            return relativo_ao_djlint
        for raiz in raizes:
            candidato = str(pathlib.Path(raiz) / relativo_ao_djlint)
            if pathlib.Path(candidato).is_file():
                return candidato
        return relativo_ao_djlint

    achados = []
    arquivo_atual = None
    for linha in pathlib.Path(caminho).read_text(encoding="utf-8", errors="replace").splitlines():
        texto = linha.strip()
        if not texto or set(texto) <= {"─", "-", "═"}:
            continue
        casado = LINHA_DJLINT.match(texto)
        if casado:
            if arquivo_atual:
                codigo, numero, _coluna, mensagem = casado.groups()
                achados.append(Achado(
                    "djlint", arquivo_atual, int(numero), codigo,
                    "erro" if codigo in DJLINT_BLOQUEANTES else "aviso",
                    mensagem.strip(),
                ))
            continue
        if texto.lower().startswith(("linted ", "djlint", "no linting")):
            continue
        # Sobra o cabeçalho com o caminho do arquivo.
        arquivo_atual = resolver(texto)
    return achados


def anotar(achados):
    for a in achados:
        nivel = "error" if a.severidade == "erro" else "warning"
        print(f"::{nivel} file={a.arquivo},line={a.linha},title={a.ferramenta} {a.regra}::{a.mensagem}")


def escrever_resumo(achados, fail_on):
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    linhas = ["## Qualidade de frontend", ""]
    if not achados:
        linhas.append("Nenhum achado em CSS, JavaScript ou templates.")
    else:
        por_ferramenta = defaultdict(lambda: defaultdict(int))
        for a in achados:
            por_ferramenta[a.ferramenta][a.regra] += 1
        erros = sum(1 for a in achados if a.severidade == "erro")
        linhas.append(f"{erros} erro(s) e {len(achados) - erros} aviso(s).")
        linhas.append("")
        linhas.append("| ferramenta | regra | ocorrências |")
        linhas.append("|---|---|---|")
        for ferramenta in sorted(por_ferramenta):
            for regra, quantas in sorted(
                por_ferramenta[ferramenta].items(), key=lambda par: -par[1]
            ):
                linhas.append(f"| {ferramenta} | `{regra}` | {quantas} |")
        # Os arquivos com mais achados são a pauta de refatoração: num CSS
        # remendado, a dívida se concentra em dois ou três arquivos.
        por_arquivo = defaultdict(int)
        for a in achados:
            por_arquivo[a.arquivo] += 1
        piores = sorted(por_arquivo.items(), key=lambda par: -par[1])[:5]
        if len(por_arquivo) > 1:
            linhas.append("")
            linhas.append("Arquivos com mais achados: " + ", ".join(
                f"`{arquivo}` ({quantas})" for arquivo, quantas in piores
            ))
    linhas.append("")
    linhas.append(f"Reprovação configurada em `{fail_on}`.")
    with open(destino, "a", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stylelint", default="", help="Relatório JSON do Stylelint.")
    p.add_argument("--eslint", default="", help="Relatório JSON do ESLint.")
    p.add_argument("--djlint", default="", help="Saída de texto do djlint.")
    p.add_argument(
        "--djlint-raiz",
        action="append",
        default=[],
        help="Diretório passado ao djlint, para recompor o caminho do arquivo. Pode repetir.",
    )
    p.add_argument(
        "--fail-on",
        default="erro",
        choices=SEVERIDADES + ["nenhum"],
        help="Severidade mínima que reprova. 'nenhum' só relata.",
    )
    p.add_argument("--out", default="frontend.json", help="Relatório unificado em JSON.")
    return p.parse_args()


def main():
    args = argumentos()
    achados = []
    leitores = (
        (args.stylelint, ler_stylelint, "stylelint"),
        (args.eslint, ler_eslint, "eslint"),
        (args.djlint, lambda c: ler_djlint(c, args.djlint_raiz), "djlint"),
    )
    for caminho, leitor, nome in leitores:
        if not caminho:
            continue
        if not pathlib.Path(caminho).is_file():
            # Relatório ausente significa que a ferramenta não chegou a rodar.
            # Tratar como "nada encontrado" transformaria uma falha de instalação
            # em aprovação silenciosa.
            print(f"::error::{nome} não gerou relatório em {caminho}.")
            return 1
        try:
            achados += leitor(caminho)
        except (json.JSONDecodeError, OSError) as e:
            print(f"::error::relatório de {nome} ilegível ({caminho}): {e}")
            return 1

    achados.sort(key=lambda a: (-SEVERIDADES.index(a.severidade), a.arquivo, a.linha))
    anotar(achados)
    pathlib.Path(args.out).write_text(
        json.dumps([a.como_dicionario() for a in achados], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    escrever_resumo(achados, args.fail_on)

    erros = sum(1 for a in achados if a.severidade == "erro")
    print(f"\n{len(achados)} achado(s) de frontend: {erros} erro(s), {len(achados) - erros} aviso(s).")
    print(f"Relatório completo: {args.out}")

    if args.fail_on == "nenhum":
        return 0
    limite = SEVERIDADES.index(args.fail_on)
    bloqueantes = sum(1 for a in achados if SEVERIDADES.index(a.severidade) >= limite)
    if bloqueantes:
        print(f"::error::{bloqueantes} achado(s) de frontend com severidade >= {args.fail_on}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
