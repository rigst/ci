#!/usr/bin/env python3
"""Roda o axe-core sobre uma lista de páginas usando o Chromium do Playwright.

    python scripts/a11y.py --base-url http://127.0.0.1:8000 \
        --paths "/" --paths "/entrar/" \
        --axe node_modules/axe-core/axe.min.js

Serve tanto para aplicação Django quanto para site estático: o que muda é
apenas quem está servindo a `--base-url`. As duas usam o mesmo motor, então o
resultado é comparável entre os projetos.

Três decisões que não são óbvias:

1. O axe entra por `page.evaluate(fonte)`, e não por `add_script_tag`. Um
   `<script>` injetado é barrado pela Content-Security-Policy que os projetos
   servem em produção — e é justamente sob os settings de produção que a
   verificação tem valor. O `evaluate` roda pelo protocolo do navegador, fora
   do alcance da CSP.

2. A reprovação é por impacto (`--fail-on`), não por contagem. Uma página com
   quarenta avisos `minor` de contraste é dívida de design; um único `critical`
   é conteúdo inalcançável por leitor de tela. Tratar os dois pelo mesmo número
   faz a equipe silenciar a ferramenta inteira.

3. `--storage-state` aceita o estado de sessão gravado por um teste e2e, o que
   permite auditar página autenticada. Sem isso a verificação só alcança o que
   é público, que nos sistemas do rigst é a menor parte.
"""

import argparse
import json
import pathlib
import sys
from urllib.parse import urljoin

# Ordem de gravidade do axe-core, do mais leve ao mais grave. Violação sem
# impacto declarado é tratada como "minor": o axe deixa o campo nulo em algumas
# regras experimentais, e assumir o pior faria o build reprovar por regra que
# nem está estabilizada.
IMPACTOS = ["minor", "moderate", "serious", "critical"]


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True, help="Origem onde as páginas são servidas.")
    p.add_argument(
        "--paths",
        action="append",
        default=[],
        help="Caminho a auditar, relativo à base-url. Pode repetir.",
    )
    p.add_argument("--axe", required=True, help="Caminho para axe.min.js.")
    p.add_argument(
        "--tags",
        default="wcag2a,wcag2aa,wcag21a,wcag21aa",
        help="Tags de regra do axe-core, separadas por vírgula.",
    )
    p.add_argument(
        "--fail-on",
        default="serious",
        choices=IMPACTOS + ["none"],
        help="Impacto mínimo que reprova o build. 'none' só relata.",
    )
    p.add_argument("--out", default="a11y.json", help="Relatório completo em JSON.")
    p.add_argument("--storage-state", default="", help="Sessão gravada pelo Playwright.")
    p.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="Tempo máximo de carregamento por página, em milissegundos.",
    )
    return p.parse_args()


def auditar(page, url, fonte_axe, tags, timeout):
    """Carrega uma página e devolve as violações que o axe encontrar."""
    page.goto(url, wait_until="load", timeout=timeout)
    # `networkidle` em vez de só `load`: os projetos usam HTMX, e parte do
    # conteúdo (e dos problemas de acessibilidade) só existe depois da primeira
    # troca. Falhar aqui não é motivo para derrubar a auditoria — página sem
    # requisição pendente já está pronta.
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    page.evaluate(fonte_axe)
    return page.evaluate(
        """(tags) => axe.run(document, {
            runOnly: { type: 'tag', values: tags },
            resultTypes: ['violations'],
        }).then(r => r.violations)""",
        tags,
    )


def relatar(url, violacoes, limite):
    """Imprime as violações como anotações do Actions e conta as bloqueantes."""
    bloqueantes = 0
    for v in violacoes:
        impacto = v.get("impact") or "minor"
        indice = IMPACTOS.index(impacto) if impacto in IMPACTOS else 0
        bloqueia = limite is not None and indice >= limite
        bloqueantes += bloqueia

        nivel = "error" if bloqueia else "warning"
        alvos = [
            alvo
            for no in v.get("nodes", [])
            for alvo in (no.get("target") or [])
        ]
        # Anotação numa linha só: o Actions trunca a mensagem no primeiro \n, e
        # uma lista de seletores quebrada em linhas some do resumo do job.
        resumo = "; ".join(str(a) for a in alvos[:5])
        if len(alvos) > 5:
            resumo += f"; (+{len(alvos) - 5})"
        print(
            f"::{nivel}::[{impacto}] {url} — {v['id']}: {v['help']} "
            f"| elementos: {resumo} | {v['helpUrl']}"
        )
    return bloqueantes


def main():
    args = argumentos()

    caminhos = [c.strip() for c in args.paths if c.strip()]
    if not caminhos:
        print("Nenhum caminho informado — nada a auditar.", file=sys.stderr)
        return 1

    arquivo_axe = pathlib.Path(args.axe)
    if not arquivo_axe.is_file():
        print(f"axe-core não encontrado em {arquivo_axe}", file=sys.stderr)
        return 1
    fonte_axe = arquivo_axe.read_text(encoding="utf-8")

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    limite = None if args.fail_on == "none" else IMPACTOS.index(args.fail_on)

    # Import tardio para que --help funcione sem o Playwright instalado.
    from playwright.sync_api import sync_playwright

    relatorio = {}
    bloqueantes = 0
    falhas_de_carga = []

    with sync_playwright() as pw:
        navegador = pw.chromium.launch()
        contexto = navegador.new_context(
            storage_state=args.storage_state or None,
            # Viewport de desktop: as regras de contraste e de alvo de toque do
            # axe dependem do layout renderizado, e o padrão do Playwright
            # (1280x720) já é o caso comum destes sistemas.
            ignore_https_errors=True,
        )
        page = contexto.new_page()

        for caminho in caminhos:
            url = caminho if "://" in caminho else urljoin(args.base_url, caminho)
            print(f"\n--- {url} ---")
            try:
                violacoes = auditar(page, url, fonte_axe, tags, args.timeout)
            except Exception as e:
                # Página que não carrega é falha da auditoria, não resultado
                # limpo. Sem isto um erro de rota devolveria "zero violações".
                print(f"::error::{url} não pôde ser auditada: {e}")
                falhas_de_carga.append(url)
                continue

            relatorio[url] = violacoes
            bloqueantes += relatar(url, violacoes, limite)
            print(f"{len(violacoes)} violação(ões) de acessibilidade.")

        navegador.close()

    pathlib.Path(args.out).write_text(
        json.dumps(relatorio, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    total = sum(len(v) for v in relatorio.values())
    print(f"\n{total} violação(ões) em {len(relatorio)} página(s).")
    print(f"Relatório completo: {args.out}")

    if falhas_de_carga:
        print(f"::error::{len(falhas_de_carga)} página(s) não carregaram.")
        return 1
    if bloqueantes:
        print(f"::error::{bloqueantes} violação(ões) de impacto >= {args.fail_on}.")
        return 1
    if total:
        print(f"Nenhuma atinge o limite de reprovação ({args.fail_on}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
