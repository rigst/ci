#!/usr/bin/env python3
"""Aplica a política do liccheck.ini a um inventário do pip-licenses.

    pip-licenses --python /var/www/PROJETO/venv/bin/python --format=json > inv.json
    python scripts/conferir_licencas_instaladas.py --politica configs/liccheck.ini --inventario inv.json

Existe por um limite do liccheck: ele lê os metadados pelo `pkg_resources` do
**próprio** interpretador, e não tem opção equivalente ao `--python` do
pip-licenses. Auditar um venv de produção com ele exigiria instalar o liccheck
lá dentro — mexer no ambiente que está no ar só para medi-lo, que é
exatamente o que não se faz.

O caminho aqui é outro: o pip-licenses lê o venv de fora, sem tocá-lo, e este
script aplica a mesma política ao resultado. A normalização abaixo reproduz a
do liccheck 0.9.2 (`command_line.py`), incluindo a divisão do inventário em
"; ", que é como o pip-licenses junta múltiplos classifiers.

Não substitui o job `licencas` do CI: aquele é o veredito sobre o que o
repositório declara, e este é sobre o que a máquina tem instalado. Os dois
divergem — é justamente a divergência que interessa.
"""

import argparse
import configparser
import json
import pathlib
import sys


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--politica", required=True, help="Arquivo liccheck.ini.")
    p.add_argument("--inventario", required=True, help="Saída do pip-licenses --format=json.")
    p.add_argument("--rotulo", default="", help="Nome do projeto, só para o relatório.")
    return p.parse_args()


def ler_politica(caminho):
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg.read(caminho, encoding="utf-8")

    def lista(chave):
        bruto = cfg.get("Licenses", chave, fallback="")
        return {linha.strip().lower() for linha in bruto.split("\n") if linha.strip()}

    autorizadas = lista("authorized_licenses")
    proibidas = lista("unauthorized_licenses")
    excecoes = {
        nome.strip().lower()
        for nome in (cfg.options("Authorized Packages") if cfg.has_section("Authorized Packages") else [])
    }
    return autorizadas, proibidas, excecoes


def normalizar(campo_licenca):
    """Reproduz a normalização do liccheck sobre o campo do pip-licenses.

    O pip-licenses junta múltiplos classifiers com "; "; o liccheck já os
    recebe separados. Depois disso a regra é a mesma: remover **um** sufixo
    " license", dividir em " OR " e passar a minúsculas.
    """
    nomes = []
    for parte in campo_licenca.split(";"):
        parte = parte.strip()
        if not parte:
            continue
        if parte.lower().endswith(" license"):
            parte = parte[: -len(" license")]
        nomes.extend(opcao.strip().lower() for opcao in parte.split(" OR "))
    return [n for n in nomes if n]


def main():
    args = argumentos()
    autorizadas, proibidas, excecoes = ler_politica(args.politica)
    if not autorizadas:
        print(f"política vazia ou ilegível: {args.politica}", file=sys.stderr)
        return 2

    inventario = json.loads(pathlib.Path(args.inventario).read_text(encoding="utf-8"))

    reprovados = []
    for pacote in inventario:
        nome = pacote.get("Name", "?")
        if nome.lower() in excecoes:
            continue
        nomes = normalizar(pacote.get("License", ""))
        if not nomes:
            reprovados.append((nome, pacote.get("Version", "?"), "não declarada"))
            continue
        # Mesma regra do nível CAUTIOUS: precisa de pelo menos uma autorizada e
        # nenhuma proibida junto.
        if any(n in proibidas for n in nomes) or not any(n in autorizadas for n in nomes):
            reprovados.append((nome, pacote.get("Version", "?"), "; ".join(nomes)))

    rotulo = f"[{args.rotulo}] " if args.rotulo else ""
    if not reprovados:
        print(f"{rotulo}{len(inventario)} pacotes instalados, todas as licenças dentro da política.")
        return 0

    print(f"{rotulo}{len(reprovados)} de {len(inventario)} pacotes fora da política:", file=sys.stderr)
    for nome, versao, licenca in sorted(reprovados):
        print(f"  {nome}=={versao}: {licenca}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
