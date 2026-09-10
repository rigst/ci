#!/usr/bin/env python3
"""Cobra qualidade apenas nas linhas que o pull request adiciona.

    python scripts/diff_quality.py --base origin/main --fail-on erro

A pergunta que este script responde não é "este repositório está limpo?" — não
está, e nenhum dos oito projetos ficaria verde no primeiro dia. É "esta
alteração aumentou a dívida?". Por isso ele analisa o arquivo inteiro (com AST,
quando é Python) mas só relata o achado cuja linha de âncora aparece no diff
contra o merge-base.

Três decisões que não são óbvias:

1. **Não existe arquivo de baseline.** A alternativa seria gravar os problemas
   conhecidos num JSON versionado, e foi assim que a primeira versão do plano
   estava escrita. Um arquivo desses conflita em merge a cada PR corretivo,
   não existe em PR de fork, e "regravei a baseline" vira o atalho universal
   para silenciar qualquer regra incômoda. O merge-base já é a baseline: está
   no git, ninguém precisa mantê-lo, e não dá para editá-lo sem reescrever a
   história.

2. **A âncora é a linha adicionada, não `arquivo+linha` guardado.** Inserir uma
   linha no topo de um módulo desloca todas as outras; um registro por
   `arquivo+linha` transformaria o módulo inteiro em "problema novo" no primeiro
   PR. Aqui a linha deslocada não está no conjunto de linhas adicionadas do
   diff, então não é relatada.

3. **Análise estrutural, filtro textual.** Achar `except Exception` com grep
   erra em string, comentário e docstring, e não sabe dizer se o corpo trata ou
   engole o erro. O AST sabe. O diff entra só no fim, para decidir o que é novo.

4. **Existe saída, e ela custa uma justificativa.** Um comentário
   `qualidade: ignorar <regra> — motivo` na linha do achado, ou no bloco de
   comentário colado nela, dispensa a regra. Sem motivo legível, a própria
   supressão vira erro. Portão sem saída documentada não sobrevive ao primeiro
   caso legítimo — alguém desliga a etapa, ou escreve o código pior só para
   passar. Assim a exceção fica no diff, o revisor a lê e o git guarda quem a
   assinou.

Severidade tem dois níveis: `erro` bloqueia (quando `--fail-on erro`) e `aviso`
apenas relata. O RUNBOOK manda rodar a primeira vez com `--fail-on nenhum`,
medir o passivo, e só então apertar.
"""

import argparse
import ast
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
from collections import defaultdict

SEVERIDADES = ["aviso", "erro"]

# Métodos de logger que contam como "tratou o erro". Um `except` que registra
# com contexto é decisão de projeto; um que não faz nada é o remendo que este
# script existe para conter.
METODOS_DE_LOG = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}

# Funções de rede que precisam de timeout explícito. Sem ele, o default é
# esperar para sempre: uma task do Celery pendurada num host que não responde
# não aparece como erro, aparece como fila que parou.
CHAMADAS_DE_REDE = {
    "get", "post", "put", "patch", "delete", "head", "options", "request",
    "urlopen", "send",
}
# `client` e `cliente` ficaram de fora depois de medir: o test client do
# Django tem exatamente a mesma forma (`client.get("/rota/")`) e gerou 21
# falsos positivos em três arquivos de teste do dojo — mais achados falsos
# do que verdadeiros na frota inteira.
MODULOS_DE_REDE = {"requests", "httpx", "urllib", "urllib3", "session", "sessao"}

# Operações de migração que destroem dado. Renomear não entra: é arriscado, mas
# reversível, e acusá-lo geraria falso positivo em todo PR de refatoração.
MIGRACOES_DESTRUTIVAS = {"RemoveField", "DeleteModel", "RemoveConstraint", "RemoveIndex"}

MARCADORES_PROVISORIOS = re.compile(
    r"\b(TODO|FIXME|XXX|HACK|WORKAROUND|GAMBIARRA|TEMPOR[ÁA]RI[OA]|PROVIS[ÓO]RI[OA]|POR ENQUANTO)\b",
    re.IGNORECASE,
)
FILTRO_SAFE = re.compile(r"\|\s*safe\b")
# `style="..."` num template é a versão HTML do remendo: alguém precisou de
# uma exceção e a escreveu onde a folha de estilo não alcança. As 231
# ocorrências herdadas da frota ficam onde estão; esta regra cobra as novas.
ESTILO_INLINE = re.compile(r"""\bstyle\s*=\s*["']""")
IMPORTANTE = re.compile(r"!\s*important\b", re.IGNORECASE)

# Escape documentado. Vale para qualquer linguagem porque procura no texto
# da linha, não no comentário formal: `# qualidade: ignorar <regra> — motivo`,
# `/* qualidade: ignorar ... */`, `{# qualidade: ignorar ... #}`.
#
# Um portão bloqueante sem saída documentada não sobrevive ao primeiro caso
# legítimo: ou alguém desliga a etapa inteira, ou escreve o código pior só
# para passar. Exigir o motivo mantém a decisão revisável — ela aparece no
# diff, o revisor a lê, e o git guarda quem a escreveu. É o que o campo
# "justificativa" faria num arquivo de baseline, sem o arquivo de baseline.
SUPRESSAO = re.compile(
    r"qualidade:\s*ignorar\s+(?P<regra>[a-z][a-z-]+)\s*(?:[—–-]+\s*(?P<motivo>.*))?",
    re.IGNORECASE,
)

# Início de comentário nas linguagens que estes projetos usam.
COMENTARIO = re.compile(r"^\s*(#|//|/\*|\*|\{#|<!--)")

# Motivo abaixo disso não é justificativa, é formalidade para passar no portão.
MOTIVO_MINIMO = 15

# Caminhos que nunca são código do projeto. Espelha as exclusões que o job do
# Sonar já usa, para os dois não discordarem sobre o que é o projeto.
IGNORADOS = [
    "*/migrations/__init__.py",
    "*/node_modules/*",
    "*/staticfiles/*",
    "*/static/*/vendor/*",
    "*/venv/*",
    "*/.venv/*",
    "*/__pycache__/*",
    "*.min.js",
    "*.min.css",
    "*/e2e/.tmp/*",
]


class Achado:
    def __init__(self, arquivo, linha, regra, severidade, mensagem):
        self.arquivo = arquivo
        self.linha = linha
        self.regra = regra
        self.severidade = severidade
        self.mensagem = mensagem

    def como_dicionario(self):
        return {
            "arquivo": self.arquivo,
            "linha": self.linha,
            "regra": self.regra,
            "severidade": self.severidade,
            "mensagem": self.mensagem,
        }


# --------------------------------------------------------------------------
# leitura do diff
# --------------------------------------------------------------------------

CABECALHO_ARQUIVO = re.compile(r"^\+\+\+ (?:b/)?(.*)$")
CABECALHO_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def ler_diff(texto):
    """Devolve {arquivo: {"adicionadas": set(linhas), "removidas": int}}."""
    resultado = {}
    arquivo = None
    for linha in texto.splitlines():
        cabecalho = CABECALHO_ARQUIVO.match(linha)
        if cabecalho:
            caminho = cabecalho.group(1).strip()
            # `+++ /dev/null` é remoção de arquivo: não há linha nova a cobrar.
            arquivo = None if caminho == "/dev/null" else caminho
            if arquivo:
                resultado.setdefault(arquivo, {"adicionadas": set(), "removidas": 0})
            continue
        if arquivo is None:
            continue
        hunk = CABECALHO_HUNK.match(linha)
        if hunk:
            removidas = int(hunk.group(2) or 1)
            inicio = int(hunk.group(3))
            quantas = int(hunk.group(4)) if hunk.group(4) is not None else 1
            resultado[arquivo]["removidas"] += removidas
            resultado[arquivo]["adicionadas"].update(range(inicio, inicio + quantas))
    return resultado


def rodar(comando):
    completo = subprocess.run(
        comando, capture_output=True, text=True, errors="replace", check=False
    )
    return completo.returncode, completo.stdout, completo.stderr


def obter_diff(base):
    """`base...HEAD` faz o git comparar contra o merge-base, não contra a ponta
    da base. É a diferença entre "o que este PR fez" e "o que este PR fez mais
    tudo que entrou na main desde que ele começou"."""
    codigo, saida, erro = rodar(["git", "diff", "--unified=0", "--no-color", "-M", f"{base}...HEAD"])
    if codigo != 0:
        # Repositório sem histórico comum costuma ser checkout raso: o job
        # precisa de fetch-depth: 0, e dizer isso poupa a rodada de adivinhação.
        print(f"::error::não consegui comparar com {base}: {erro.strip()}", file=sys.stderr)
        print(
            "::error::confira se o checkout do job usa fetch-depth: 0.",
            file=sys.stderr,
        )
        return None
    return ler_diff(saida)


def ignorado(caminho):
    return any(fnmatch.fnmatch("/" + caminho, padrao) or fnmatch.fnmatch(caminho, padrao)
               for padrao in IGNORADOS)


# --------------------------------------------------------------------------
# regras de Python (estruturais, via AST)
# --------------------------------------------------------------------------

def _tem_raise(no):
    return any(isinstance(f, ast.Raise) for f in ast.walk(no))


def _tem_log_com_contexto(no):
    """Um `logger.error("falhou")` sem nada do erro não é tratamento — é ruído
    no log. Exigimos ao menos um argumento além de literal vazio."""
    for filho in ast.walk(no):
        if not isinstance(filho, ast.Call):
            continue
        alvo = filho.func
        nome = alvo.attr if isinstance(alvo, ast.Attribute) else getattr(alvo, "id", "")
        if nome in METODOS_DE_LOG and (filho.args or filho.keywords):
            return True
    return False


def _corpo_vazio(corpo):
    if len(corpo) != 1:
        return False
    unico = corpo[0]
    if isinstance(unico, ast.Pass):
        return True
    # `...` como corpo é a mesma coisa que pass, escrito de outro jeito.
    return (
        isinstance(unico, ast.Expr)
        and isinstance(unico.value, ast.Constant)
        and unico.value.value is Ellipsis
    )


def _nome_da_excecao(handler):
    tipo = handler.type
    if tipo is None:
        return "except:"
    if isinstance(tipo, ast.Name):
        return tipo.id
    if isinstance(tipo, ast.Tuple):
        nomes = [e.id for e in tipo.elts if isinstance(e, ast.Name)]
        for generica in ("Exception", "BaseException"):
            if generica in nomes:
                return generica
    return ""


def _kwarg(chamada, nome):
    return any(k.arg == nome for k in chamada.keywords)


def _eh_chamada_de_rede(chamada):
    alvo = chamada.func
    if not isinstance(alvo, ast.Attribute) or alvo.attr not in CHAMADAS_DE_REDE:
        return False
    raiz = alvo.value
    while isinstance(raiz, ast.Attribute):
        raiz = raiz.value
    nome = getattr(raiz, "id", "") or ""
    return nome.lower() in MODULOS_DE_REDE


def _campo_obrigatorio_sem_default(chamada):
    """AlterField que torna a coluna NOT NULL sem default trava a migração numa
    tabela que já tem linhas. É a família de erro que só aparece em produção."""
    for argumento in list(chamada.args) + [k.value for k in chamada.keywords]:
        if not isinstance(argumento, ast.Call):
            continue
        nulo = next((k for k in argumento.keywords if k.arg == "null"), None)
        permite_nulo = bool(nulo and isinstance(nulo.value, ast.Constant) and nulo.value.value)
        if not permite_nulo and not _kwarg(argumento, "default"):
            return True
    return False


def analisar_python(caminho, fonte, adicionadas, limite_funcao):
    try:
        arvore = ast.parse(fonte, filename=caminho)
    except SyntaxError as e:
        # Erro de sintaxe é bloqueio imediato pela §8 do planejamento, e o ruff
        # também o pegaria — mas o ruff pode estar em soft-fail.
        linha = e.lineno or 1
        return [Achado(caminho, linha, "sintaxe", "erro", f"não compila: {e.msg}")]

    achados = []
    eh_migracao = "/migrations/" in "/" + caminho

    for no in ast.walk(arvore):
        if isinstance(no, ast.ExceptHandler):
            if no.lineno not in adicionadas:
                continue
            nome = _nome_da_excecao(no)
            if nome not in ("except:", "Exception", "BaseException"):
                continue
            if _corpo_vazio(no.body):
                achados.append(Achado(
                    caminho, no.lineno, "excecao-engolida", "erro",
                    f"`{nome}` novo com corpo vazio: o erro desaparece sem deixar rastro.",
                ))
            elif not _tem_raise(no) and not _tem_log_com_contexto(no):
                achados.append(Achado(
                    caminho, no.lineno, "excecao-generica", "erro",
                    f"`{nome}` novo sem `raise` nem log com contexto. "
                    "Capture a exceção específica, ou registre o erro e o que se tentava fazer.",
                ))

        elif isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if no.lineno not in adicionadas:
                continue
            fim = getattr(no, "end_lineno", no.lineno)
            tamanho = fim - no.lineno + 1
            if tamanho > limite_funcao:
                achados.append(Achado(
                    caminho, no.lineno, "funcao-grande", "aviso",
                    f"função `{no.name}` nasce com {tamanho} linhas (limite {limite_funcao}).",
                ))

        elif isinstance(no, ast.Call):
            if no.lineno not in adicionadas:
                continue
            if eh_migracao:
                alvo = no.func
                nome = alvo.attr if isinstance(alvo, ast.Attribute) else getattr(alvo, "id", "")
                if nome in MIGRACOES_DESTRUTIVAS:
                    achados.append(Achado(
                        caminho, no.lineno, "migracao-destrutiva", "erro",
                        f"`{nome}` apaga dado e não tem volta. "
                        "Se é intencional, separe num PR próprio com backup conferido.",
                    ))
                elif nome == "AlterField" and _campo_obrigatorio_sem_default(no):
                    achados.append(Achado(
                        caminho, no.lineno, "migracao-destrutiva", "erro",
                        "`AlterField` para coluna obrigatória sem `default`: "
                        "falha em tabela que já tem linhas.",
                    ))
            elif _eh_chamada_de_rede(no) and not _kwarg(no, "timeout"):
                achados.append(Achado(
                    caminho, no.lineno, "sem-timeout", "aviso",
                    "chamada externa sem `timeout=`: o default é esperar para sempre.",
                ))
            else:
                alvo = no.func
                nome = alvo.attr if isinstance(alvo, ast.Attribute) else getattr(alvo, "id", "")
                if nome == "mark_safe":
                    achados.append(Achado(
                        caminho, no.lineno, "escape-desligado", "aviso",
                        "`mark_safe` novo: confirme que o conteúdo não vem do usuário.",
                    ))

    return achados


# --------------------------------------------------------------------------
# regras textuais (valem para qualquer arquivo, só nas linhas adicionadas)
# --------------------------------------------------------------------------

def analisar_texto(caminho, fonte, adicionadas):
    achados = []
    sufixo = pathlib.Path(caminho).suffix.lower()
    linhas = fonte.splitlines()

    for numero in sorted(adicionadas):
        if numero > len(linhas):
            continue
        conteudo = linhas[numero - 1]

        if sufixo == ".css" and IMPORTANTE.search(conteudo):
            achados.append(Achado(
                caminho, numero, "important-novo", "aviso",
                "`!important` novo. Costuma ser sintoma de especificidade "
                "disputada — prefira ajustar o seletor de origem.",
            ))

        if sufixo == ".html" and FILTRO_SAFE.search(conteudo):
            achados.append(Achado(
                caminho, numero, "escape-desligado", "erro",
                "`|safe` novo desliga o escape do Django nesta variável.",
            ))

        if sufixo == ".html" and ESTILO_INLINE.search(conteudo):
            achados.append(Achado(
                caminho, numero, "estilo-inline", "aviso",
                "`style=` novo no template: a folha de estilo não alcança essa regra, "
                "e ela vence qualquer seletor.",
            ))

        marcador = MARCADORES_PROVISORIOS.search(conteudo)
        if marcador:
            achados.append(Achado(
                caminho, numero, "marcador-provisorio", "aviso",
                f"marcador `{marcador.group(1)}` novo: remendo declarado no próprio código.",
            ))

    return achados


def analisar_crescimento(caminho, base, dados, limite_percentual):
    """Arquivo que engorda muito num PR só é o retrato do remendo: cada conserto
    empilha um caso a mais no mesmo lugar em vez de reorganizá-lo."""
    codigo, antes, _ = rodar(["git", "show", f"{base}:{caminho}"])
    if codigo != 0:
        return []  # arquivo novo: nascer grande é outra regra, não esta
    total_antes = len(antes.splitlines())
    if total_antes < 100:
        return []  # em arquivo pequeno, qualquer adição estoura o percentual
    crescimento = len(dados["adicionadas"]) - dados["removidas"]
    if crescimento <= 0:
        return []
    percentual = 100.0 * crescimento / total_antes
    if percentual < limite_percentual:
        return []
    return [Achado(
        caminho, 1, "arquivo-cresceu", "aviso",
        f"cresceu {percentual:.0f}% num único PR "
        f"({total_antes} → {total_antes + crescimento} linhas, limite {limite_percentual:.0f}%).",
    )]


# --------------------------------------------------------------------------
# saída
# --------------------------------------------------------------------------

def aplicar_supressoes(achados, fontes):
    """Remove o achado que tem supressão justificada; acusa a que não tem motivo.

    A supressão é procurada na própria linha do achado e na linha imediatamente
    acima — as duas convenções que as pessoas usam sem pensar. Não se procura
    mais longe de propósito: uma marca a cinco linhas de distância deixa de ser
    visível para quem lê o código depois.
    """
    resultado = []
    for achado in achados:
        linhas = fontes.get(achado.arquivo, [])
        candidatas = []
        if 1 <= achado.linha <= len(linhas):
            candidatas.append(linhas[achado.linha - 1])
        # Sobe pelo bloco de comentário colado na linha, e para no primeiro
        # código. Um motivo bem escrito raramente cabe em uma linha, e limitar
        # a busca a "a linha de cima" obrigaria a espremer a justificativa —
        # que é justamente a parte que precisa ser legível.
        numero = achado.linha - 1
        while 1 <= numero <= len(linhas) and COMENTARIO.match(linhas[numero - 1]):
            candidatas.append(linhas[numero - 1])
            numero -= 1

        marca = None
        for texto in candidatas:
            encontrada = SUPRESSAO.search(texto)
            if encontrada and encontrada.group("regra").lower() == achado.regra:
                marca = encontrada
                break

        if marca is None:
            resultado.append(achado)
            continue

        motivo = (marca.group("motivo") or "").strip().rstrip("*/#-} ").strip()
        if len(motivo) < MOTIVO_MINIMO:
            resultado.append(Achado(
                achado.arquivo, achado.linha, "supressao-sem-motivo", "erro",
                f"`qualidade: ignorar {achado.regra}` sem justificativa legível. "
                "Escreva depois de um travessão por que este caso é legítimo.",
            ))
    return resultado


def anotar(achados):
    for a in achados:
        nivel = "error" if a.severidade == "erro" else "warning"
        # Com file= e line= a anotação aparece ancorada em "Files changed" do
        # PR, e não só perdida no log do job.
        print(f"::{nivel} file={a.arquivo},line={a.linha},title={a.regra}::{a.mensagem}")


def escrever_resumo(achados, base, fail_on):
    destino = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destino:
        return
    por_regra = defaultdict(list)
    for a in achados:
        por_regra[a.regra].append(a)

    linhas = ["## Qualidade do diff", ""]
    if not achados:
        linhas.append(f"Nenhum achado nas linhas adicionadas contra `{base}`.")
    else:
        erros = sum(1 for a in achados if a.severidade == "erro")
        avisos = len(achados) - erros
        linhas.append(f"{erros} erro(s) e {avisos} aviso(s) nas linhas adicionadas contra `{base}`.")
        linhas.append("")
        linhas.append("| regra | severidade | ocorrências |")
        linhas.append("|---|---|---|")
        for regra in sorted(por_regra, key=lambda r: -len(por_regra[r])):
            grupo = por_regra[regra]
            linhas.append(f"| `{regra}` | {grupo[0].severidade} | {len(grupo)} |")
        linhas.append("")
        linhas.append("<details><summary>Achados</summary>")
        linhas.append("")
        for a in achados:
            linhas.append(f"- `{a.arquivo}:{a.linha}` — **{a.regra}** — {a.mensagem}")
        linhas.append("")
        linhas.append("</details>")
    linhas.append("")
    linhas.append(f"Reprovação configurada em `{fail_on}`.")
    with open(destino, "a", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")


# --------------------------------------------------------------------------

def argumentos():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True, help="Ref da base (ex.: origin/main).")
    p.add_argument(
        "--fail-on",
        default="erro",
        choices=SEVERIDADES + ["nenhum"],
        help="Severidade mínima que reprova. 'nenhum' só relata.",
    )
    p.add_argument("--limite-funcao", type=int, default=50, help="Linhas por função nova.")
    p.add_argument(
        "--limite-crescimento",
        type=float,
        default=15.0,
        help="Crescimento máximo de um arquivo, em percentual, num único PR.",
    )
    p.add_argument("--out", default="diff-quality.json", help="Relatório em JSON.")
    return p.parse_args()


def main():
    args = argumentos()
    diff = obter_diff(args.base)
    if diff is None:
        return 1

    achados = []
    fontes = {}
    analisados = 0
    for caminho, dados in sorted(diff.items()):
        if ignorado(caminho) or not dados["adicionadas"]:
            continue
        arquivo = pathlib.Path(caminho)
        if not arquivo.is_file():
            continue
        try:
            fonte = arquivo.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binário ou ilegível: não é código a cobrar

        analisados += 1
        fontes[caminho] = fonte.splitlines()
        if arquivo.suffix == ".py":
            achados += analisar_python(caminho, fonte, dados["adicionadas"], args.limite_funcao)
        achados += analisar_texto(caminho, fonte, dados["adicionadas"])
        achados += analisar_crescimento(caminho, args.base, dados, args.limite_crescimento)

    achados = aplicar_supressoes(achados, fontes)
    achados.sort(key=lambda a: (SEVERIDADES.index(a.severidade) * -1, a.arquivo, a.linha))
    anotar(achados)

    pathlib.Path(args.out).write_text(
        json.dumps(
            {
                "base": args.base,
                "arquivos_analisados": analisados,
                "achados": [a.como_dicionario() for a in achados],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    escrever_resumo(achados, args.base, args.fail_on)

    erros = sum(1 for a in achados if a.severidade == "erro")
    avisos = len(achados) - erros
    print(f"\n{analisados} arquivo(s) com linhas novas; {erros} erro(s), {avisos} aviso(s).")
    print(f"Relatório completo: {args.out}")

    if args.fail_on == "nenhum":
        return 0
    limite = SEVERIDADES.index(args.fail_on)
    bloqueantes = sum(1 for a in achados if SEVERIDADES.index(a.severidade) >= limite)
    if bloqueantes:
        print(f"::error::{bloqueantes} achado(s) de severidade >= {args.fail_on} nas linhas novas.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
