#!/usr/bin/env python3
"""Mede o layout renderizado em várias larguras e acusa o que quebrou.

    python scripts/layout.py --base-url http://127.0.0.1:8000 \
        --paths "/" --paths "/entrar/" --larguras 390,768,1440

Serve tanto para aplicação Django quanto para site estático — como o `a11y.py`,
o que muda é apenas quem serve a `--base-url`.

Por que medir em vez de comparar screenshots: a comparação por pixel responde
"mudou?", que é a pergunta errada. Ela reprova a mudança legítima, aceita a
quebra que já estava na referência, e depende de as fontes do runner serem as
mesmas da máquina onde a referência foi gravada — que não são. A suíte visual
de um destes projetos já falha de forma idêntica com a árvore limpa, o que
mostra o custo desse acoplamento. Aqui a pergunta é "está quebrado?", e a
resposta sai de `getBoundingClientRect`: objetiva, estável entre execuções, e
legível no log sem abrir imagem nenhuma.

Duas decisões que não são óbvias:

1. **Só o infrator mais externo é relatado.** Quando um contêiner vaza da
   viewport, todo descendente vaza junto. Relatar os duzentos faria a saída
   inútil; relatar o de fora aponta o elemento que precisa ser consertado.

2. **Transbordo cortado, ou inteiramente fora, não é quebra de layout.** A
   primeira versão desta regra acusava em massa três padrões legítimos: o
   brilho decorativo dentro de um hero recortado, a gaveta fora da tela até
   ser aberta, e a tabela larga dentro de um contêiner rolável — que é
   justamente a forma recomendada de tratá-la.

   Dois filtros separam isso de uma quebra real. Um ancestral **interno** com
   `overflow-x` em `hidden`, `clip`, `auto` ou `scroll` que corte o excedente:
   o usuário não vê nada errado, e no caso de `auto`/`scroll` ainda alcança o
   conteúdo rolando aquele contêiner. E o elemento que está **inteiramente**
   fora da viewport, que é posicionamento deliberado — o que quebra layout é
   o que fica meio dentro e meio fora.

   `body` e `html` ficam de fora da conta: `body { overflow-x: hidden }` é o
   truque de esconder o sintoma, não um contêiner projetado. Aceitá-lo
   silenciaria a regra inteira, e o primeiro teste provou isso — a quebra de
   verdade sumiu junto com os três falsos positivos.

3. **`elementFromPoint` decide o que é conteúdo coberto, e a regra é
   direcional.** A alternativa (medir a altura da barra fixa e supor) erra nos
   dois sentidos. Perguntar ao navegador quem está no ponto responde de fato se
   o usuário consegue ler ou tocar aquilo.

   Mas "coberto agora" não é "inalcançável": conteúdo que passa sob um
   cabeçalho fixo enquanto se rola é normal — basta rolar de volta. A primeira
   versão desta regra ignorava isso e devolveu onze achados num único projeto,
   todos do mesmo `header` fixo, todos falsos. O que prende de verdade é a
   barra que cobre o conteúdo numa extremidade onde não há mais para onde
   rolar: um cabeçalho no topo com a página em `scrollTop = 0`, ou um rodapé
   fixo com a página no fim. Por isso a medição roda nas duas extremidades e,
   em cada uma, só considera a barra ancorada naquele lado.
"""

import argparse
import json
import pathlib
import sys
from urllib.parse import urljoin

SEVERIDADES = ["aviso", "erro"]

# JavaScript que roda dentro da página. Devolve uma lista de achados já
# classificados: manter a regra aqui, e não no Python, evita ida e volta pelo
# protocolo do navegador para cada um dos milhares de elementos da página.
MEDIDOR = """
(config) => {
  const achados = [];
  const larguraViewport = window.innerWidth;
  const alturaViewport = window.innerHeight;
  const TOLERANCIA = 2;  // arredondamento de subpixel não é bug de layout

  const visivel = (el) => {
    const estilo = getComputedStyle(el);
    if (estilo.display === 'none' || estilo.visibility === 'hidden') return false;
    if (parseFloat(estilo.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const seletor = (el) => {
    if (el.id) return `${el.tagName.toLowerCase()}#${el.id}`;
    const classe = (el.className || '').toString().trim().split(/\\s+/).filter(Boolean)[0];
    return classe ? `${el.tagName.toLowerCase()}.${classe}` : el.tagName.toLowerCase();
  };

  const registrar = (regra, severidade, el, detalhe) => {
    achados.push({ regra, severidade, seletor: el ? seletor(el) : null, detalhe });
  };

  // 1. A página inteira rola de lado. É o sintoma que o usuário sente antes de
  // qualquer outro, e quase sempre tem uma única causa.
  const excedente = document.documentElement.scrollWidth - larguraViewport;
  if (excedente > TOLERANCIA) {
    registrar('overflow-horizontal', 'erro', null,
      `a página rola ${Math.round(excedente)}px para o lado`);
  }

  const todos = Array.from(document.body.querySelectorAll('*')).filter(visivel);

  // 2. Elemento que vaza da viewport. Só o mais externo: o pai que vaza leva
  // todo mundo junto, e listar os filhos afogaria o achado que importa.
  const vaza = (el) => {
    const r = el.getBoundingClientRect();
    return r.right > larguraViewport + TOLERANCIA || r.left < -TOLERANCIA;
  };

  // ...e que ninguém corta. Transbordo cortado por um ancestral não é quebra
  // de layout: o usuário não vê nem alcança o excedente. Cobre três padrões
  // legítimos que a primeira versão desta regra acusava em massa — o brilho
  // decorativo posicionado por absolute dentro de um hero com overflow:hidden,
  // a gaveta fora da tela até ser aberta, e a tabela larga dentro de um
  // contêiner com overflow-x:auto, que é a forma recomendada de tratá-la.
  //
  // `auto` e `scroll` entram junto com `hidden` e `clip` de propósito: o
  // conteúdo continua alcançável, só que rolando aquele contêiner, e não a
  // página. Se a página inteira rolar de lado, a regra 1 acusa de qualquer
  // forma — ela é a rede de segurança desta.
  const CORTA = ['hidden', 'clip', 'auto', 'scroll'];
  const cortadoPorAncestral = (el) => {
    const r = el.getBoundingClientRect();
    let pai = el.parentElement;
    // Para em `body`: `body { overflow-x: hidden }` é o truque clássico de
    // esconder o sintoma, não um contêiner de rolagem projetado. Aceitá-lo
    // como recorte legítimo silenciaria a regra inteira — foi o que aconteceu
    // no primeiro teste, em que a quebra de verdade sumiu junto com os três
    // falsos positivos.
    while (pai && pai !== document.body && pai !== document.documentElement) {
      const estilo = getComputedStyle(pai);
      if (CORTA.includes(estilo.overflowX)) {
        const rp = pai.getBoundingClientRect();
        if (r.right > rp.right + TOLERANCIA || r.left < rp.left - TOLERANCIA) return true;
      }
      pai = pai.parentElement;
    }
    return false;
  };

  // Elemento INTEIRAMENTE fora da viewport é posicionamento deliberado — a
  // gaveta que espera ser aberta, o carrossel fora do quadro. O que quebra
  // layout é o que fica meio dentro e meio fora, porque é isso que o usuário
  // vê cortado ou empurra a página para o lado.
  const foraPorInteiro = (el) => {
    const r = el.getBoundingClientRect();
    return r.right <= TOLERANCIA || r.left >= larguraViewport - TOLERANCIA;
  };

  for (const el of todos) {
    if (!vaza(el)) continue;
    if (foraPorInteiro(el)) continue;
    if (cortadoPorAncestral(el)) continue;
    let pai = el.parentElement;
    let paiVaza = false;
    while (pai && pai !== document.body) {
      if (vaza(pai) && !foraPorInteiro(pai) && !cortadoPorAncestral(pai)) { paiVaza = true; break; }
      pai = pai.parentElement;
    }
    if (paiVaza) continue;
    const r = el.getBoundingClientRect();
    registrar('elemento-fora-da-viewport', 'erro', el,
      `ocupa de ${Math.round(r.left)}px a ${Math.round(r.right)}px numa viewport de ${larguraViewport}px`);
  }

  // 3. Conteúdo cortado: cabe menos do que existe, e o excedente foi escondido
  // em vez de quebrar. Diferente do item 2 — aqui nada vaza, some.
  for (const el of todos) {
    const estilo = getComputedStyle(el);
    if (estilo.overflowX !== 'hidden' && estilo.overflow !== 'hidden') continue;
    if (el.scrollWidth - el.clientWidth <= TOLERANCIA) continue;
    if (el.clientWidth === 0) continue;
    registrar('conteudo-cortado', 'aviso', el,
      `${el.scrollWidth - el.clientWidth}px de conteúdo escondidos por overflow:hidden`);
  }

  // 4. Alvo de toque pequeno demais. O limite vem do WCAG 2.2 (2.5.8), e a
  // regra vale em qualquer largura: mouse impreciso existe no desktop também.
  const interativos = 'a[href], button, input, select, textarea, [role="button"], [role="link"], [onclick]';
  for (const el of Array.from(document.querySelectorAll(interativos)).filter(visivel)) {
    const tipo = (el.getAttribute('type') || '').toLowerCase();
    if (tipo === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width >= config.alvoMinimo && r.height >= config.alvoMinimo) continue;
    // Link dentro de um parágrafo é exceção explícita do próprio critério:
    // sublinhado no meio de uma frase não tem como ter 24px de altura.
    if (el.tagName === 'A' && el.closest('p, li, td, th')) continue;
    registrar('alvo-pequeno', 'aviso', el,
      `${Math.round(r.width)}x${Math.round(r.height)}px, mínimo ${config.alvoMinimo}px`);
  }

  // 5. Imagem distorcida: a proporção renderizada não bate com a do arquivo, e
  // não há object-fit para justificar.
  for (const img of Array.from(document.images).filter(visivel)) {
    if (!img.naturalWidth || !img.naturalHeight) continue;
    const ajuste = getComputedStyle(img).objectFit;
    if (ajuste === 'cover' || ajuste === 'contain' || ajuste === 'scale-down') continue;
    const r = img.getBoundingClientRect();
    const proporcaoReal = img.naturalWidth / img.naturalHeight;
    const proporcaoTela = r.width / r.height;
    const desvio = Math.abs(proporcaoTela - proporcaoReal) / proporcaoReal;
    if (desvio > 0.1) {
      registrar('imagem-distorcida', 'aviso', img,
        `proporção ${proporcaoTela.toFixed(2)} contra ${proporcaoReal.toFixed(2)} do arquivo`);
    }
  }

  return { achados, altura: document.documentElement.scrollHeight, alturaViewport };
}
"""

# Passada de cobertura, feita nas duas extremidades da rolagem. `lado` diz qual
# barra pode prender conteúdo ali: no topo da página, só a fixa ancorada em
# cima; no fim, só a ancorada embaixo. Medida com elementFromPoint, e não pela
# altura da barra.
COBERTURA = """
(lado) => {
  const achados = [];
  const seletor = (el) => {
    if (el.id) return `${el.tagName.toLowerCase()}#${el.id}`;
    const classe = (el.className || '').toString().trim().split(/\\s+/).filter(Boolean)[0];
    return classe ? `${el.tagName.toLowerCase()}.${classe}` : el.tagName.toLowerCase();
  };
  const MARGEM = 4;
  const fixo = (el) => {
    const p = getComputedStyle(el).position;
    if (p !== 'fixed' && p !== 'sticky') return false;
    const r = el.getBoundingClientRect();
    // Ancorada no lado que importa. Uma barra que flutua no meio da tela não
    // entra em nenhum dos dois casos, e de fato não prende nada: rolar a
    // revela ou a afasta.
    if (lado === 'topo') return r.top <= MARGEM;
    return r.bottom >= window.innerHeight - MARGEM;
  };

  const candidatos = 'p, li, h1, h2, h3, a[href], button, input, label, td';
  const vistos = new Set();
  for (const el of document.querySelectorAll(candidatos)) {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) continue;
    if (r.top < 0 || r.bottom > window.innerHeight) continue;
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const noPonto = document.elementFromPoint(x, y);
    if (!noPonto || noPonto === el || el.contains(noPonto) || noPonto.contains(el)) continue;

    // Só acusa quando quem cobre é elemento fixo ou grudado: sobreposição
    // dentro do fluxo normal costuma ser decoração intencional.
    let acima = noPonto;
    let culpado = null;
    while (acima && acima !== document.body) {
      if (fixo(acima)) { culpado = acima; break; }
      acima = acima.parentElement;
    }
    if (!culpado) continue;
    const chave = seletor(culpado);
    if (vistos.has(chave)) continue;
    vistos.add(chave);
    achados.push({
      regra: 'conteudo-coberto', severidade: 'erro', seletor: seletor(el),
      detalhe: `coberto por ${chave} (position: ${getComputedStyle(culpado).position})`
        + ` com a página ${lado === 'topo' ? 'no início' : 'no fim'} da rolagem,`
        + ` sem mais para onde rolar`,
    });
  }
  return achados;
}
"""


def caminho_no_repositorio(caminho):
    """Devolve o caminho só se ele estiver dentro do checkout.

    Os caminhos chegam dos relatórios das ferramentas e da linha de comando —
    dados, e não constantes. Um `../` numa entrada leria arquivo de fora da
    árvore analisada; confinar é mais barato do que confiar."""
    raiz = pathlib.Path.cwd().resolve()
    try:
        alvo = (raiz / caminho).resolve()
        alvo.relative_to(raiz)
    except (ValueError, OSError):
        return None
    return alvo


def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True, help="Origem onde as páginas são servidas.")
    p.add_argument("--paths", action="append", default=[], help="Caminho a medir. Pode repetir.")
    p.add_argument(
        "--larguras",
        default="390,768,1440",
        help="Larguras de viewport, em px, separadas por vírgula.",
    )
    p.add_argument(
        "--fail-on",
        default="erro",
        choices=SEVERIDADES + ["nenhum"],
        help="Severidade mínima que reprova. 'nenhum' só relata.",
    )
    p.add_argument("--alvo-minimo", type=int, default=24, help="Lado mínimo de alvo clicável, em px.")
    p.add_argument("--out", default="layout.json", help="Relatório completo em JSON.")
    p.add_argument("--storage-state", default="", help="Sessão gravada pelo Playwright.")
    p.add_argument("--timeout", type=int, default=30000, help="Tempo máximo por página, em ms.")
    return p.parse_args()


def medir(page, url, largura, alvo_minimo, timeout):
    page.set_viewport_size({"width": largura, "height": 900})
    page.goto(url, wait_until="load", timeout=timeout)
    # Mesmo motivo do a11y.py: os projetos usam HTMX, e parte do layout (e das
    # quebras) só existe depois da primeira troca.
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass

    resultado = page.evaluate(MEDIDOR, {"alvoMinimo": alvo_minimo})
    achados = resultado["achados"]

    # No topo da página não há como rolar para cima: o que estiver sob um
    # cabeçalho fixo aqui está inalcançável.
    achados += page.evaluate(COBERTURA, "topo")

    if resultado["altura"] > resultado["alturaViewport"]:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(300)  # dar tempo à barra que aparece ao rolar
        achados += page.evaluate(COBERTURA, "fim")

    return achados


def relatar(url, largura, achados):
    bloqueantes = 0
    for a in achados:
        bloqueia = a["severidade"] == "erro"
        bloqueantes += bloqueia
        nivel = "error" if bloqueia else "warning"
        alvo = f" | {a['seletor']}" if a.get("seletor") else ""
        # Anotação numa linha só: o Actions trunca a mensagem no primeiro \n.
        print(f"::{nivel}::[{largura}px] {url} — {a['regra']}: {a['detalhe']}{alvo}")
    return bloqueantes


def main():
    args = argumentos()

    caminhos = [c.strip() for c in args.paths if c.strip()]
    if not caminhos:
        print("Nenhum caminho informado — nada a medir.", file=sys.stderr)
        return 1

    try:
        larguras = [int(v.strip()) for v in args.larguras.split(",") if v.strip()]
    except ValueError:
        print(f"--larguras espera números separados por vírgula: {args.larguras}", file=sys.stderr)
        return 1
    if not larguras:
        print("Nenhuma largura informada.", file=sys.stderr)
        return 1

    # Import tardio para que --help funcione sem o Playwright instalado.
    from playwright.sync_api import sync_playwright

    relatorio = {}
    bloqueantes = 0
    total = 0
    falhas_de_carga = []

    with sync_playwright() as pw:
        navegador = pw.chromium.launch()
        contexto = navegador.new_context(
            storage_state=args.storage_state or None,
            ignore_https_errors=True,
        )
        page = contexto.new_page()

        for caminho in caminhos:
            url = caminho if "://" in caminho else urljoin(args.base_url, caminho)
            for largura in larguras:
                chave = f"{url} @{largura}px"
                print(f"\n--- {chave} ---")
                try:
                    achados = medir(page, url, largura, args.alvo_minimo, args.timeout)
                except Exception as e:
                    # Página que não carrega é falha da medição, não layout
                    # limpo. Sem isto um erro de rota devolveria "zero achados".
                    print(f"::error::{chave} não pôde ser medida: {e}")
                    falhas_de_carga.append(chave)
                    continue
                relatorio[chave] = achados
                total += len(achados)
                bloqueantes += relatar(url, largura, achados)
                print(f"{len(achados)} achado(s) de layout.")

        navegador.close()

    destino = caminho_no_repositorio(args.out)
    if destino is None:
        print(f"::error::--out aponta para fora do diretório analisado: {args.out}")
        return 1
    destino.write_text(
        json.dumps(relatorio, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n{total} achado(s) em {len(relatorio)} combinação(ões) de página e largura.")
    print(f"Relatório completo: {args.out}")

    if falhas_de_carga:
        print(f"::error::{len(falhas_de_carga)} página(s) não puderam ser medidas.")
        return 1
    if args.fail_on == "nenhum":
        return 0
    if args.fail_on == "aviso" and total:
        print(f"::error::{total} achado(s) de layout.")
        return 1
    if bloqueantes:
        print(f"::error::{bloqueantes} achado(s) de layout com severidade erro.")
        return 1
    if total:
        print(f"Nenhum atinge o limite de reprovação ({args.fail_on}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
