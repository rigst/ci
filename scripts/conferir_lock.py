#!/usr/bin/env python3
"""Confere se o lock está de acordo com o requirements.txt que o originou.

    python scripts/conferir_lock.py
    python scripts/conferir_lock.py --requirements requirements.txt --lock requirements.lock

Roda em segundos e sem rede: só compara o que os dois arquivos dizem.

Existe porque o Dependabot atualiza `requirements.txt` mas não reconhece
`requirements.lock` como arquivo de dependências — o nome não casa com os que a
ferramenta procura. Sem esta checagem os dois divergem em silêncio: o CI testa
uma versão e a imagem de produção instala outra, que é o tipo de diferença que
só aparece depois do deploy.

Confere três coisas:

1. toda dependência direta declarada existe no lock;
2. a versão é a mesma nos dois arquivos;
3. toda linha do lock traz pelo menos um hash — um pin sem hash passa pelo
   `--require-hashes` do pip apenas enquanto nenhum outro pacote tiver hash, e
   é exatamente o buraco que o lock deveria fechar.
"""

import argparse
import pathlib
import re
import sys

# Nome do pacote, extra opcional, e a versão fixada. Marcador de ambiente
# (`; python_version < "3.13"`) é cortado: não faz parte da identidade.
RE_REQUISITO = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^\s;]+)")
RE_LOCK = re.compile(r"^([A-Za-z0-9._-]+)==([^\s\\]+)")


def normalizar(nome):
    return nome.lower().replace("_", "-")


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--requirements", default="requirements.txt")
    p.add_argument("--lock", default="requirements.lock")
    return p.parse_args()


def pinos_do_requirements(caminho):
    pinos = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        m = RE_REQUISITO.match(linha)
        if not m:
            print(f"sem pin exato (==) em {caminho.name}: {linha!r}", file=sys.stderr)
            return None
        pinos[normalizar(m.group(1))] = m.group(2)
    return pinos


def pinos_do_lock(caminho):
    """Devolve {pacote: (versao, tem_hash)} lendo o lock linha a linha."""
    pinos = {}
    atual = None
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        m = RE_LOCK.match(linha)
        if m:
            atual = normalizar(m.group(1))
            # O hash pode estar na mesma linha ou nas continuações seguintes.
            pinos[atual] = (m.group(2), "--hash=" in linha)
        elif atual and "--hash=" in linha:
            versao, _ = pinos[atual]
            pinos[atual] = (versao, True)
    return pinos


def main():
    args = argumentos()
    req_path = pathlib.Path(args.requirements)
    lock_path = pathlib.Path(args.lock)

    if not lock_path.exists():
        print(
            f"{lock_path} não existe. Gere com: python scripts/gerar_lock.py",
            file=sys.stderr,
        )
        return 1

    declarados = pinos_do_requirements(req_path)
    if declarados is None:
        print(
            "\nO lock exige pin exato em todas as linhas. O procedimento de "
            "conversão está no RUNBOOK do rigst/ci.",
            file=sys.stderr,
        )
        return 1
    travados = pinos_do_lock(lock_path)

    faltando = sorted(n for n in declarados if n not in travados)
    divergentes = sorted(
        (n, declarados[n], travados[n][0])
        for n in declarados
        if n in travados and declarados[n] != travados[n][0]
    )
    sem_hash = sorted(n for n, (_, tem) in travados.items() if not tem)

    if not faltando and not divergentes and not sem_hash:
        print(
            f"lock em dia: {len(declarados)} dependências diretas conferem, "
            f"{len(travados)} pacotes travados com hash."
        )
        return 0

    for nome in faltando:
        print(f"FALTA no lock: {nome}=={declarados[nome]}", file=sys.stderr)
    for nome, esperado, achado in divergentes:
        print(f"DIVERGE: {nome} — {req_path.name} {esperado}, lock {achado}", file=sys.stderr)
    for nome in sem_hash:
        print(f"SEM HASH no lock: {nome}", file=sys.stderr)
    print("\nRegenere com: python scripts/gerar_lock.py", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
