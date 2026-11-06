#!/usr/bin/env python3
"""Confere os cabeçalhos de segurança das URLs públicas da frota.

    python scripts/conferir_headers.py https://app.stolben.com/healthz/ ...
    python scripts/conferir_headers.py --fail-on erro https://app.stolben.com/

Por que existe: os cabeçalhos vêm de dois lugares (middleware do Django e
`add_header` do nginx), e nenhum teste de app olha a resposta que sai do
nginx de verdade. Em 05/11/2026 o stolben.com não mandava nenhum, três apps
não tinham CSP e nenhum mandava Permissions-Policy — tudo com o CI verde.

Cada URL é pedida com GET, sem seguir redirecionamento (o 302 do login também
precisa dos cabeçalhos; é a resposta que o navegador recebe primeiro).

Severidade:
- **erro**: falta de HSTS, X-Content-Type-Options ou proteção contra frame
  (X-Frame-Options ou `frame-ancestors` na CSP); HSTS com max-age < 6 meses.
- **aviso**: falta de CSP, Referrer-Policy, Permissions-Policy; CSP com
  'unsafe-eval' em script-src; CSP sem `object-src` nem `default-src`.

Sai com 1 se houver achado na severidade de `--fail-on` ou acima, ou se uma
URL não responder.
"""

import argparse
import re
import sys
import urllib.error
import urllib.request

ORDEM = {"aviso": 1, "erro": 2}
SEIS_MESES = 15552000


class SemRedirecionar(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - API do urllib
        return None


def buscar(url, timeout=15):
    """Devolve (status, cabeçalhos em minúsculas) sem seguir redirecionamento."""
    opener = urllib.request.build_opener(SemRedirecionar)
    pedido = urllib.request.Request(url, headers={"User-Agent": "rigst-ci-headers/1"})
    try:
        with opener.open(pedido, timeout=timeout) as r:  # noqa: S310 - URL https da frota
            return r.status, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}


def diretivas_csp(valor):
    saida = {}
    for parte in valor.split(";"):
        partes = parte.strip().split()
        if partes:
            saida[partes[0].lower()] = [p.lower() for p in partes[1:]]
    return saida


def avaliar(cabecalhos):
    """Lista de (severidade, mensagem) para um conjunto de cabeçalhos."""
    achados = []
    hsts = cabecalhos.get("strict-transport-security", "")
    if not hsts:
        achados.append(("erro", "sem Strict-Transport-Security"))
    else:
        m = re.search(r"max-age=(\d+)", hsts)
        if not m or int(m.group(1)) < SEIS_MESES:
            achados.append(("erro", f"HSTS com max-age curto: {hsts!r}"))

    if cabecalhos.get("x-content-type-options", "").lower() != "nosniff":
        achados.append(("erro", "sem X-Content-Type-Options: nosniff"))

    csp = cabecalhos.get("content-security-policy", "")
    diretivas = diretivas_csp(csp) if csp else {}
    if not cabecalhos.get("x-frame-options") and "frame-ancestors" not in diretivas:
        achados.append(("erro", "sem proteção contra frame (X-Frame-Options ou frame-ancestors)"))

    if not csp:
        achados.append(("aviso", "sem Content-Security-Policy"))
    else:
        script = diretivas.get("script-src", diretivas.get("default-src", []))
        if "'unsafe-eval'" in script:
            achados.append(("aviso", "CSP com 'unsafe-eval' em script-src"))
        if "object-src" not in diretivas and "default-src" not in diretivas:
            achados.append(("aviso", "CSP sem object-src nem default-src"))

    if not cabecalhos.get("referrer-policy"):
        achados.append(("aviso", "sem Referrer-Policy"))
    if not cabecalhos.get("permissions-policy"):
        achados.append(("aviso", "sem Permissions-Policy"))
    return achados


def argumentos(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="+", help="URLs https a conferir.")
    p.add_argument("--fail-on", default="erro", choices=["aviso", "erro", "nunca"])
    return p.parse_args(argv)


def main(argv=None):
    args = argumentos(argv)
    limite = ORDEM.get(args.fail_on, 99)
    reprovou = False
    linhas = ["## Cabeçalhos de segurança", "", "| URL | status | achados |", "|---|---|---|"]
    for url in args.urls:
        if not url.startswith("https://"):
            print(f"::error::só https: {url}")
            reprovou = True
            continue
        try:
            status, cabecalhos = buscar(url)
        except (OSError, urllib.error.URLError) as exc:
            print(f"::error::{url} não respondeu: {exc}")
            linhas.append(f"| {url} | — | não respondeu |")
            reprovou = True
            continue
        achados = avaliar(cabecalhos)
        for severidade, msg in achados:
            nivel = "error" if ORDEM[severidade] >= limite else "warning"
            print(f"::{nivel}::{url}: {msg}")
            reprovou = reprovou or ORDEM[severidade] >= limite
        resumo = "; ".join(f"{s}: {m}" for s, m in achados) or "ok"
        linhas.append(f"| {url} | {status} | {resumo} |")
        print(f"{url} {status}: {resumo}")
    print("\n".join(["", *linhas, ""]))
    return 1 if reprovou else 0


if __name__ == "__main__":
    sys.exit(main())
