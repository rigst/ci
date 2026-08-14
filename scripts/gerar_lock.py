#!/usr/bin/env python3
"""Gera um lock com hashes a partir de um requirements.txt de pins exatos.

    python scripts/gerar_lock.py
    python scripts/gerar_lock.py --python-version 3.14 --sdist-only ofxparse=beautifulsoup4,lxml,six

Escrito para rodar na máquina do desenvolvedor, não no CI: ele acessa o PyPI
para colher os hashes. O CI apenas confere o resultado (`conferir_lock.py`) e
instala com `--require-hashes`.

Por que o lock existe: sem ele as dependências transitivas ficam soltas e o
conteúdo instalado muda sozinho entre dois builds do mesmo commit. O
`requirements.txt` prende o que o projeto declara; o lock prende o que o pip
realmente resolve, com o hash de cada artefato.

Duas decisões que não são óbvias:

1. A resolução é feita para `--python-version`, e não para o interpretador que
   roda este script. Resolver no 3.12 e instalar no 3.14 pode produzir
   conjuntos diferentes, e sob `--require-hashes` a diferença vira falha de
   build em vez de aviso.

2. Os hashes colhidos cobrem **todos** os artefatos publicados de cada versão,
   não só o wheel escolhido nesta máquina. É o que mantém o lock válido em
   outra arquitetura — caso contrário o build no runner x86 e o build no
   servidor ARM discordariam.

Pacote publicado apenas como sdist precisa de `--sdist-only`: o pip exige
`--only-binary :all:` junto de `--python-version`, então esses ficam fora da
resolução e entram fixados na versão declarada, com as dependências deles
passadas ao resolvedor para que as transitivas não sumam.
"""

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import urllib.request


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--requirements", default="requirements.txt", help="Arquivo de entrada.")
    p.add_argument("--lock", default="requirements.lock", help="Arquivo de saída.")
    p.add_argument(
        "--python-version",
        default="3.12",
        help="Versão do Python para a qual resolver — a de produção, não a desta máquina.",
    )
    p.add_argument(
        "--sdist-only",
        action="append",
        default=[],
        metavar="PACOTE=dep1,dep2",
        help="Pacote sem wheel publicado, com as dependências dele. Pode repetir.",
    )
    return p.parse_args()


def normalizar(nome):
    return nome.lower().replace("_", "-")


def resolver(requisitos, python_version, destino_relatorio):
    """Roda o resolvedor do pip sem instalar nada e devolve o relatório JSON."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(requisitos) + "\n")
        caminho = f.name
    with tempfile.TemporaryDirectory() as alvo:
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--dry-run", "--quiet", "--ignore-installed",
                "--python-version", python_version,
                "--only-binary", ":all:",
                "--target", alvo,
                "--report", str(destino_relatorio),
                "-r", caminho,
            ],
            check=True,
        )


def hashes_de(nome, versao):
    """Colhe do PyPI o sha256 de todos os artefatos publicados de uma versão."""
    url = f"https://pypi.org/pypi/{nome}/{versao}/json"
    with urllib.request.urlopen(url) as r:  # noqa: S310 — host fixo, https
        info = json.load(r)
    digests = sorted({a["digests"]["sha256"] for a in info.get("urls") or []})
    if not digests:
        raise SystemExit(f"sem artefatos publicados para {nome}=={versao}")
    return digests


def main():
    args = argumentos()
    entrada_path = pathlib.Path(args.requirements)
    saida_path = pathlib.Path(args.lock)

    sdist_only = {}
    for item in args.sdist_only:
        nome, _, deps = item.partition("=")
        sdist_only[normalizar(nome)] = [d for d in deps.split(",") if d]

    declarados = [
        linha.strip()
        for linha in entrada_path.read_text(encoding="utf-8").splitlines()
        if linha.strip() and not linha.lstrip().startswith("#")
    ]

    soltos = [r for r in declarados if "==" not in r]
    if soltos:
        # Faixa e lock não convivem: o lock congelaria uma resolução de hoje
        # enquanto o requirements.txt continuaria dizendo que qualquer versão
        # da faixa serve. Os dois passariam a discordar em silêncio.
        print("Estas linhas não têm pin exato (==):", file=sys.stderr)
        for r in soltos:
            print(f"  {r}", file=sys.stderr)
        print(
            "\nConverta para == antes de gerar o lock. O procedimento está no "
            "RUNBOOK do rigst/ci, seção 'Converter faixa em pin exato'.",
            file=sys.stderr,
        )
        return 1

    fixados_a_mao = {}
    entrada = []
    for req in declarados:
        nome = normalizar(req.split("==")[0].split("[")[0].strip())
        if nome in sdist_only:
            fixados_a_mao[nome] = req.split("==", 1)[1].split(";")[0].strip()
            entrada.extend(sdist_only[nome])
        else:
            entrada.append(req)

    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as f:
        relatorio = pathlib.Path(f.name)
    resolver(entrada, args.python_version, relatorio)

    pacotes = {
        normalizar(item["metadata"]["name"]): item["metadata"]["version"]
        for item in json.loads(relatorio.read_text())["install"]
    }
    pacotes.update(fixados_a_mao)

    linhas = [
        f"# Gerado por scripts/gerar_lock.py a partir de {entrada_path.name} — não edite à mão.",
        "#",
        f"# Resolvido para Python {args.python_version}, incluindo as transitivas.",
        "# Os hashes cobrem todos os artefatos de cada versão, então o arquivo",
        "# vale em qualquer arquitetura.",
        "#",
        f"# Depois de mexer em {entrada_path.name}:  python scripts/gerar_lock.py",
        "",
    ]
    for nome in sorted(pacotes):
        versao = pacotes[nome]
        digests = hashes_de(nome, versao)
        print(f"  {nome}=={versao} ({len(digests)} artefatos)", file=sys.stderr)
        corpo = f"{nome}=={versao}"
        for d in digests:
            corpo += f" \\\n    --hash=sha256:{d}"
        linhas.append(corpo)

    saida_path.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"\n{len(pacotes)} pacotes em {saida_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
