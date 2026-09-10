# ci

Pipeline de CI compartilhado dos projetos Python/Django do [@rigst](https://github.com/rigst).

Cada projeto passa a ter um `.github/workflows/ci.yml` de dez linhas em vez de
uma cópia divergente do mesmo pipeline. Ajuste feito aqui vale para todos.

## Uso

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  ci:
    uses: rigst/ci/.github/workflows/python-django.yml@v1
    secrets: inherit
    with:
      django-settings-module: config.settings.production
```

Um exemplo completo, com as variáveis que os checks do Django exigem, está em
[`examples/caller-ci.yml`](examples/caller-ci.yml).

Para adotar o pipeline num projeto novo, configurar Codecov/Sonar, ligar a
proteção de branch ou fazer um deploy de rotina, siga o
[**RUNBOOK.md**](RUNBOOK.md) — são os procedimentos já executados nos projetos
existentes, com os comandos exatos.

## O que roda

| # | Etapa | Job | Ferramenta |
|---|-------|-----|-----------|
| 1 | Lint rápido | `ruff` | `ruff check` + `ruff format --check` |
| 2 | Tipos | `mypy` | `mypy` + `django-stubs` |
| 3 | Testes e cobertura | `pytest` | `pytest-cov` → Codecov (e artefato p/ Sonar) |
| 4 | Segurança | `security` | `bandit` (código, portão por severidade) + `pip-audit` (dependências) |
| 5 | Segredos | `gitleaks` | `gitleaks` sobre o **histórico completo** |
| 6 | Agregação | `sonar` | SonarQube Cloud, consome a cobertura do passo 3 |
| 7 | Django | `django` | `check --deploy` + `makemigrations --check` |
| 8 | Licenças | `licencas` | `liccheck` (veredito) + `pip-licenses` (inventário) |
| 9 | SBOM | `sbom` | `cyclonedx-py` sobre o ambiente resolvido |
| 10 | Integridade das dependências | `lock` | lock confere com o `requirements.txt` e instala sob `--require-hashes` |
| 11 | Ponta a ponta | `e2e` | `pytest -m e2e` com Playwright num navegador real |
| 12 | Acessibilidade | `a11y` | `axe-core` sobre as páginas servidas |
| 13 | Qualidade do diff | `diff-quality` | regras sobre **as linhas que o PR adiciona** |
| 14 | Frontend | `frontend` | `stylelint` (CSS) + `eslint` (JS) + `djlint` (templates) |
| 15 | Layout | `layout` | Playwright medindo a página em 390/768/1440 px |

Os jobs rodam em paralelo; só o `sonar` espera o `pytest`, porque precisa do
`coverage.xml`. O job final `resultado` consolida tudo — **é ele que deve ser
exigido no branch protection**, não os quinze individualmente.

**As etapas 8 a 15 nascem desligadas.** As sete primeiras valem para qualquer
projeto Django sem configuração; estas cinco não. Quatro exigem alguma coisa do
repositório (um lock gerado, um teste marcado, uma lista de rotas para
auditar), e mesmo as que não exigem mudariam o veredito de pipelines que hoje
estão verdes. Ligá-las por padrão faria a próxima subida da tag `v1` quebrar
nove repositórios ao mesmo tempo. A ordem de adoção está no
[RUNBOOK](RUNBOOK.md#5-ligar-as-checagens-de-conformidade).

Há ainda um workflow que não é de PR: `relatorio-frota.yml` roda uma vez por mês
e publica churn histórico e duplicação entre os repositórios. Está descrito em
[Relatório da frota](#relatório-da-frota).

## Adoção gradual: `soft-fail`

Ligar sete checagens de uma vez num projeto existente trava o merge no primeiro
dia. O input `soft-fail` recebe uma lista separada por vírgula das etapas que
rodam e reportam sem derrubar o build:

```yaml
with:
  soft-fail: "mypy,pytest"
```

Valores aceitos: `ruff`, `mypy`, `pytest`, `bandit`, `pip-audit`, `gitleaks`,
`django`, `licencas`, `sbom`, `lock`, `e2e`, `a11y`. O padrão é `mypy`, porque
tipar um projeto Django existente é o item mais demorado da lista. Conforme
cada etapa zera, tire-a da lista.

Atenção ao ligar as etapas 8 a 12 num projeto que já declara `soft-fail: ""`:
lista vazia significa *nada tolerado*, então a checagem nova entra bloqueando
no primeiro dia. Declare explicitamente o que está entrando, por exemplo
`soft-fail: "a11y,e2e"`.

### Ambiente separado para os testes

`django-env` descreve produção, que é o ambiente que o `check --deploy` precisa
auditar. Rodar os testes nesses mesmos settings quebra em `SECURE_SSL_REDIRECT`,
TLS obrigatório no banco e `ALLOWED_HOSTS` sem `testserver`.

Para isso existem `test-settings-module` e `test-env`, que valem **só no job de
testes**. O `test-env` é aplicado depois do `django-env`, e no `$GITHUB_ENV` a
última atribuição de uma chave vence — então declare apenas as diferenças:

```yaml
with:
  django-env: |
    DJANGO_ALLOWED_HOSTS=example.com
    DATABASE_URL=postgres://postgres:postgres@localhost:5432/postgres
  test-env: |
    DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver
    DJANGO_DB_SSL_REQUIRE=false
```

`test-settings-module` vira variável de ambiente, e não chave de `pytest.ini`,
porque na precedência do `pytest-django` a variável vence o `ini` — declarar no
`ini` não bastaria para escapar dos settings de produção.

### Bandit: portão por severidade

O bandit imprime sempre o relatório completo, mas só derruba o build a partir de
`bandit-severity` (padrão `high`). Isso permite travar "nenhum achado alto" sem
precisar anotar dezenas de achados médios já auditados — que é o caminho que
leva a `# nosec` cego, e transforma a ferramenta em ruído.

Quando o passivo médio zerar, baixe para `medium`.

## Conformidade: licenças, SBOM e dependências travadas

### Licenças (`run-licencas`)

O veredito é do `liccheck`, com a política em
[`configs/liccheck.ini`](configs/liccheck.ini) — sobrescrita por um
`liccheck.ini` na raiz do projeto, se existir.

A lista de licenças autorizadas não é genérica: parte do fato de que os
projetos são publicados sob **AGPL-3.0**. Isso torna compatível praticamente
todo software livre, inclusive GPL e LGPL, e deixa como alvo real o que não
pode ser redistribuído — proprietário, e sobretudo **licença não declarada**,
que é o achado que de fato aparece. Por isso o nível padrão é `CAUTIOUS`.

O job também publica `licencas-inventario.md`, uma tabela de todas as
dependências resolvidas com licença, autor e URL. Serve para manter o
`LICENCAS.md` do projeto sem transcrever nada à mão.

### SBOM (`run-sbom`)

`cyclonedx-py` sobre o ambiente **resolvido**, não sobre o `requirements.txt`:
o SBOM descreve o que o pip realmente instalou, com transitivas e versões
exatas. Sai em CycloneDX JSON, com `--output-reproducible` para que dois builds
do mesmo commit gerem arquivos idênticos e o diff signifique alguma coisa.

A retenção padrão é de 90 dias, muito acima da dos outros artefatos, porque o
valor do SBOM é retroativo: quando sair uma CVE nova, é ele que responde se a
versão afetada estava embarcada naquele commit.

### Dependências travadas por hash (`run-lock`)

Duas checagens distintas, e é a segunda que justifica o job:

1. `conferir_lock.py` — o lock corresponde ao `requirements.txt`. Roda em
   segundos e sem rede. Existe porque o Dependabot atualiza o
   `requirements.txt` mas **não reconhece o lock** como arquivo de
   dependências: sem esta conferência os dois divergem em silêncio.
2. `pip install --require-hashes` de verdade — pega transitiva faltando e hash
   errado. Nenhuma outra etapa acusa isso, porque todas as demais instalam pelo
   `requirements.txt`, com o resolvedor livre.

O lock é gerado na máquina do desenvolvedor com
[`scripts/gerar_lock.py`](scripts/gerar_lock.py), que exige **pin exato** em
todas as linhas. Faixa de versão e lock não convivem — o procedimento de
conversão está no [RUNBOOK](RUNBOOK.md#51-converter-faixa-em-pin-exato).

Se produção roda numa versão de Python diferente da que o pipeline analisa
(o `sistema_arq` resolve o lock para o 3.14 da imagem), use
`lock-python-version`; conferir na versão errada acusa divergência que não
existe.

## Ponta a ponta e acessibilidade

### e2e (`run-e2e`)

`pytest -m e2e` com `pytest-playwright` num navegador de verdade, sobre o
`live_server` do `pytest-django`. Com o job ligado, **a suíte comum passa a
excluir o marcador `e2e`** automaticamente — sem isso os testes de navegador
rodariam duas vezes, e na segunda sem Playwright instalado.

Requer o marcador declarado no `pytest.ini` do projeto, senão o pytest emite
aviso de marcador desconhecido:

```ini
markers =
    e2e: teste de ponta a ponta em navegador
```

### a11y (`run-a11y`)

`axe-core` injetado pelo Playwright nas páginas listadas em `a11y-paths`,
servidas pelo `runserver` sob os **settings de teste** — sob os de produção o
`SECURE_SSL_REDIRECT` devolveria 301 e o axe auditaria uma tela vazia.

A reprovação é por impacto (`a11y-fail-on`, padrão `serious`), não por
contagem. Quarenta avisos `minor` de contraste são dívida de design; um único
`critical` é conteúdo inalcançável por leitor de tela. Tratar os dois pelo
mesmo número é o caminho para a equipe silenciar a ferramenta.

Comece com `a11y-fail-on: none` para medir o passivo sem bloquear nada, e
aperte depois. O relatório completo sai como artefato `a11y.json`.

Para páginas que exigem login, use `a11y-setup-command` para semear os dados e
inclua a rota autenticada em `a11y-paths` — o script aceita também uma sessão
gravada pelo Playwright via `--storage-state`.

No `static-site.yml` o mesmo job existe sem configuração: com `a11y-paths`
vazio ele audita **todo `*.html` do repositório**, que num site sem build é a
cobertura completa.

## Qualidade: dívida nova contra dívida herdada

As etapas 13 a 15 existem para uma pergunta que as doze anteriores não
respondem: **esta alteração aumentou a dívida técnica?** É outra pergunta que
"este repositório está limpo?", e nos projetos do rigst a segunda já tem
resposta conhecida — não está, e não vai estar tão cedo.

A separação entre as duas é o que faz o pipeline ser usável:

| | o que olha | o que faz com o passivo |
|---|---|---|
| `diff-quality` | só as linhas que o PR adiciona | ignora: dívida herdada não aparece |
| `frontend` | o arquivo inteiro | mostra tudo, mas quase tudo entra como aviso |
| `layout` | a página renderizada | mede o estado atual, não a variação |

### `diff-quality`: o motor, e por que não há arquivo de baseline

A regra "problemas antigos ficam registrados, problemas novos bloqueiam" é
geralmente implementada com um arquivo de baseline versionado no projeto. Aqui
não é, de propósito. Um arquivo desses:

- conflita em merge a cada PR corretivo, porque todo conserto o reescreve;
- não existe em PR de fork, onde o CI mais precisa de um veredito;
- e transforma "regravei a baseline" no atalho universal para silenciar
  qualquer regra incômoda, sem que a revisão perceba.

O merge-base já é a baseline. Está no git, ninguém precisa mantê-lo, e não dá
para editá-lo sem reescrever a história. O job compara `base...HEAD`, analisa o
arquivo inteiro com AST — para não confundir `except Exception` escrito numa
string com o escrito no código — e relata só o achado cuja linha aparece entre
as adicionadas.

O efeito lateral que mais importa: **linha apenas deslocada não é novidade.**
Inserir uma docstring no topo de um módulo empurra tudo para baixo, e um
registro por `arquivo+linha` acusaria o módulo inteiro como problema novo no
primeiro PR. O teste `test_linha_deslocada_nao_e_novidade` existe para isso.

O que ele cobra:

| regra | severidade |
|---|---|
| `except:`/`except Exception` novo com corpo vazio | erro |
| `except:`/`except Exception` novo sem `raise` nem log com contexto | erro |
| `\|safe` novo em template | erro |
| `RemoveField`/`DeleteModel` novo, ou `AlterField` obrigatório sem `default` | erro |
| erro de sintaxe em arquivo Python | erro |
| `!important` novo em CSS | aviso |
| `style=` novo em template | aviso |
| função nova acima de 50 linhas | aviso |
| arquivo que cresce mais de 15% num único PR | aviso |
| `TODO`/`FIXME`/`workaround`/`gambiarra` novo | aviso |
| chamada externa nova sem `timeout=` | aviso |
| `mark_safe` novo | aviso |

### A saída de emergência custa uma justificativa

Portão bloqueante sem escape documentado não sobrevive ao primeiro caso
legítimo: ou alguém desliga a etapa inteira, ou escreve o código pior só para
passar. Um comentário na linha do achado — ou no bloco de comentário colado
nela — dispensa a regra:

```python
try:
    import sentry_sdk
# qualidade: ignorar excecao-engolida — monitoramento é opcional,
# o app precisa subir sem ele
except Exception:
    pass
```

Sem motivo legível depois do travessão, a própria supressão vira erro
(`supressao-sem-motivo`). A sintaxe funciona em qualquer linguagem — procura no
texto da linha, não no comentário formal — então `/* qualidade: ignorar
important-novo — ... */` vale em CSS.

É o que o campo "justificativa" faria num arquivo de baseline, sem o arquivo:
a exceção aparece no diff, o revisor a lê, e o git guarda quem a assinou.

### `frontend` e o passivo que ele enxerga

Ao contrário do `diff-quality`, esta etapa analisa o arquivo inteiro e portanto
vê tudo que já estava lá. Medição da frota em 2026-09-10:

| ferramenta | achados | concentração |
|---|---|---|
| Stylelint | 221 | todos `no-duplicate-selectors`; 129 no `sistema_orcamentos` |
| djlint | 37 | `H020` (marcação morta) e `H043` (button sem type) |
| ESLint | 58 avisos, **0 erros** | `no-unused-vars` e `no-empty` |

Daí a calibragem: **todo achado de Stylelint entra como aviso**, porque
seletor duplicado herdado é dívida a conter, não build a derrubar. O que
bloqueia é ESLint de severidade 2 e template estruturalmente quebrado — que
estão em zero na frota, então a etapa pode entrar bloqueando sem quebrar nada.
O controle de `!important` não está aqui: fica no `diff-quality`, que só cobra
as ocorrências novas.

As três ferramentas são pinadas por versão exata, como o axe-core e o gitleaks:
uma regra nova numa versão futura reprovaria nove repositórios de uma vez, sem
ninguém ter mudado uma linha de código.

### `layout`: medir, não comparar pixels

O layout é verificado com asserções medidas — `getBoundingClientRect`,
`elementFromPoint` — e não por comparação de screenshots.

Screenshot responde "mudou?", que é a pergunta errada: reprova a mudança
legítima, aceita a quebra que já estava na referência, e depende de as fontes do
runner serem as da máquina onde a referência foi gravada. A asserção responde
"está quebrado?", com veredito estável entre execuções e legível no log sem
abrir imagem nenhuma.

O que é medido em cada largura declarada:

| regra | severidade |
|---|---|
| `overflow-horizontal` — a página rola de lado | erro |
| `elemento-fora-da-viewport` — só o infrator mais externo, e só o que ninguém corta | erro |
| `conteudo-coberto` — barra fixa prendendo conteúdo numa extremidade | erro |
| `conteudo-cortado` — `overflow:hidden` escondendo texto | aviso |
| `alvo-pequeno` — abaixo de 24px (WCAG 2.5.8), contando o `<label>` junto | aviso |
| `imagem-distorcida` — proporção renderizada diferente da do arquivo | aviso |

Duas outras calibragens saíram de olhar o passivo depois que os gates fecharam,
quando 75 dos 574 achados da frota se revelaram ruído:

- **Elemento acessivelmente oculto não é medido.** `.visually-hidden` e `.ds-sr`
  são `1px` recortados por `clip`/`clip-path`: existem para o leitor de tela.
  Acusá-los como alvo de toque de 13px ou conteúdo cortado rendeu 36 achados.
- **O alvo de um controle com `<label>` é o label.** Clicar no texto marca o
  campo, então a área que aceita o clique inclui os dois. Medir só o `<input>`
  acusava todo checkbox de 19px com rótulo ao lado — inclusive o do aceite
  legal, em cinco projetos, que chegou a entrar numa lista de prioridade como
  se fosse defeito real.
- **A exceção "inline" do WCAG 2.5.8 passou a ser medida como o critério a
  define.** A primeira versão testava `closest('p, li, td, th')` e errava por
  literalismo: o rodapé "Um app Stölben · © 2026 · Privacidade · Termos" são
  links inline numa linha de texto, mas dentro de um `<footer>`. Eram 125
  achados na frota, quase todos assim. O que define a exceção não é a tag do
  pai, é o link estar cercado de texto.
- No ESLint, `no-empty` passou a permitir `catch` vazio: os 15 achados eram o
  mesmo `try { localStorage.setItem(...) } catch (e) {}`, que é o tratamento
  correto para storage que lança em navegação privada.

A regra de transbordo ignora o que está cortado ou inteiramente fora, e isso
também veio de medição. Ao apertar os gates, os 60 achados de
`elemento-fora-da-viewport` do `site_stolben` e o único do `sistema_orcamentos`
eram três padrões legítimos: brilho decorativo dentro de um hero recortado,
gaveta fora da tela até ser aberta, e tabela larga dentro de contêiner rolável.
Já os de `sistema_questoes`, `divisor_pdf` e `sistema_trilhas` vinham com
`overflow-horizontal` na mesma página — quebra de verdade, e a mesma nos três,
por causa dos templates duplicados do `legal/`.

`body` e `html` não contam como recorte: `body { overflow-x: hidden }` é o
truque de esconder o sintoma. O primeiro teste da correção provou por que isso
importa — aceitá-lo fez a quebra real desaparecer junto com os falsos
positivos.

A regra de cobertura é direcional, e isso veio de uma medição real. "Coberto
agora" não é "inalcançável": conteúdo que passa sob um cabeçalho fixo enquanto
se rola é normal — basta rolar de volta. A primeira versão ignorava isso e
devolveu onze achados no `sistema_orcamentos`, todos do mesmo `header`, todos
falsos. O que prende de verdade é a barra que cobre conteúdo numa extremidade
onde não há mais para onde rolar, então a medição roda no início e no fim da
rolagem e, em cada ponto, só considera a barra ancorada naquele lado.

Como o `a11y`, o job só alcança o que está declarado em `layout-paths`; tela
autenticada exige `layout-setup-command` ou uma sessão gravada.

## Relatório da frota

`relatorio-frota.yml` roda no dia 1 de cada mês (e sob `workflow_dispatch`).
Clona os repositórios da frota e publica dois números que nenhum job de PR
consegue produzir:

- **Churn cruzado com tamanho e proporção de commits de conserto.** Churn
  sozinho não é defeito — arquivo muito alterado pode ser o mais vivo do
  sistema. O produto dos três só fica alto quando os três estão altos, e essa
  é a fila de refatoração.
- **Duplicação entre repositórios.** Um job de PR tem um repositório em mãos;
  os outros oito não estão lá. A comparação é por função normalizada (literais
  de texto e comentários apagados), para que duas cópias que divergiram só numa
  mensagem de erro ainda contem como a mesma coisa.

Precisa do secret `FROTA_TOKEN` (PAT de leitura). Sem ele o workflow avisa e sai
sem reprovar: relatório informativo que falha vira ruído no e-mail.

Não bloqueia nada, por decisão. A remoção da duplicação está fora do escopo do
CI — não há biblioteca compartilhada entre os projetos, e criá-la é decisão de
arquitetura. O que o relatório garante é que o número seja conhecido e não
cresça sem que ninguém perceba.

## Configuração das ferramentas

O workflow usa a configuração do próprio projeto quando ela existe
(`pyproject.toml`, `ruff.toml`, `mypy.ini`, `.bandit`, `.gitleaks.toml`).
Quando não existe, cai no baseline de [`configs/`](configs/) — então um projeto
sem nenhum arquivo de config já roda o pipeline inteiro.

Os baselines assumem Django: ignoram `migrations/`, liberam `import *` em
settings e não tratam senha fictícia de teste como segredo vazado.

## Inputs

| Input | Padrão | Descrição |
|---|---|---|
| `python-version` | `"3.12"` | Versão do Python em todos os jobs |
| `requirements-file` | `requirements.txt` | Dependências de runtime |
| `dev-requirements-file` | `""` | Dependências de teste, se separadas |
| `source-paths` | `"."` | Pastas analisadas por mypy e bandit |
| `django-settings-module` | `""` | `DJANGO_SETTINGS_MODULE` dos checks |
| `django-env` | `""` | `CHAVE=valor` por linha, valores fictícios de CI |
| `run-diff-quality` | `false` | Cobra qualidade nas linhas que o PR adiciona |
| `diff-quality-fail-on` | `erro` | `erro`, `aviso` ou `nenhum` (só relata) |
| `diff-quality-limite-funcao` | `50` | Linhas a partir das quais função **nova** vira aviso |
| `diff-quality-limite-crescimento` | `15` | Crescimento de arquivo, em %, que vira aviso |
| `run-frontend` | `false` | Stylelint + ESLint + djlint |
| `frontend-paths` | `static` | Diretórios de CSS/JS. **Nunca a raiz** |
| `template-paths` | `templates` | Diretórios de template Django |
| `run-stylelint` / `run-eslint` / `run-djlint` | `true` | Partes da etapa de frontend |
| `frontend-fail-on` | `erro` | `erro`, `aviso` ou `nenhum` |
| `run-layout` | `false` | Mede a página renderizada com Playwright |
| `layout-paths` | `""` | Caminhos medidos, um por linha. Vazio com o job ligado falha |
| `layout-larguras` | `390,768,1440` | Viewports medidas, em px |
| `layout-fail-on` | `erro` | `erro`, `aviso` ou `nenhum` |
| `layout-alvo-minimo` | `24` | Lado mínimo de alvo clicável, em px |
| `layout-setup-command` | `""` | Semeadura antes de medir |
| `layout-port` | `8002` | Porta do servidor durante a medição |
| `test-settings-module` | `""` | Settings só do job de testes; vence o `pytest.ini` |
| `test-env` | `""` | Variáveis só do job de testes; aplicadas **depois** do `django-env` |
| `django-check-fail-level` | `WARNING` | Nível que faz `check --deploy` falhar |
| `postgres` | `true` | Sobe PostgreSQL para os testes |
| `postgres-version` | `"16"` | Tag da imagem |
| `coverage-fail-under` | `0` | Cobertura mínima; `0` desliga |
| `apt-packages` | `""` | Pacotes de sistema instalados antes dos testes |
| `bandit-severity` | `"high"` | Severidade a partir da qual o bandit bloqueia |
| `soft-fail` | `"mypy"` | Etapas que reportam sem bloquear |
| `run-ruff` … `run-django-checks` | `true` | Liga/desliga cada etapa |
| `run-codecov` | `true` | Envia cobertura ao Codecov |
| `run-sonar` | `false` | Roda o SonarQube Cloud |
| `sonar-project-key` | `""` | Chave do projeto; obrigatória com `run-sonar` |
| `sonar-organization` | `""` | Chave da organização; obrigatória com `run-sonar` |
| `sonar-args` | `""` | Propriedades extras do scanner, separadas por espaço |
| `run-licencas` | `false` | Confere licenças e publica o inventário |
| `liccheck-level` | `"CAUTIOUS"` | `STANDARD`, `CAUTIOUS` ou `PARANOID` |
| `run-sbom` | `false` | Gera o SBOM CycloneDX |
| `sbom-spec-version` | `"1.6"` | Versão do esquema CycloneDX |
| `sbom-retention-days` | `90` | Retenção do artefato de SBOM |
| `run-lock` | `false` | Confere o lock e instala com `--require-hashes` |
| `lock-file` | `requirements.lock` | Arquivo de lock com hashes |
| `lock-python-version` | `""` | Python de produção, se diferente do analisado |
| `run-e2e` | `false` | Roda os testes marcados `e2e` no navegador |
| `e2e-args` | `"-m e2e"` | Argumentos do pytest no job de e2e |
| `e2e-browser` | `"chromium"` | Navegador instalado pelo Playwright |
| `run-a11y` | `false` | Audita acessibilidade com axe-core |
| `a11y-paths` | `""` | Caminhos auditados, um por linha; obrigatório com `run-a11y` |
| `a11y-tags` | `wcag2a,wcag2aa,wcag21a,wcag21aa` | Tags de regra do axe-core |
| `a11y-fail-on` | `"serious"` | Impacto que reprova; `none` só relata |
| `a11y-setup-command` | `""` | Comando que semeia dados antes da auditoria |
| `a11y-port` | `8001` | Porta do servidor durante a auditoria |
| `ci-ref` | `""` | Ref deste repo para configs e scripts. Vazio = o commit do próprio workflow |

## Secrets

Ambos opcionais — passe com `secrets: inherit`.

| Secret | Necessário para |
|---|---|
| `CODECOV_TOKEN` | Upload de cobertura ao Codecov |
| `SONAR_TOKEN` | `run-sonar: true` |

O `gitleaks` roda pelo binário oficial, sem necessidade de licença.

## SonarQube Cloud

`run-sonar` vem **desligado por padrão**: o plano gratuito do SonarQube Cloud
(ex-SonarCloud) cobre apenas repositórios públicos. Para ligar, o projeto
precisa ser público ou ter plano pago.

Não é preciso `sonar-project.properties` na raiz: o pipeline monta as
propriedades e só pede as duas que identificam o projeto.

```yaml
    with:
      run-sonar: true
      sonar-project-key: rigst_nome-do-projeto
      sonar-organization: rigst
```

Já vêm configurados: versão do Python, `coverage.xml` como fonte de cobertura,
e exclusões de migrações, `staticfiles/`, `node_modules/` e virtualenvs. Testes,
settings e migrações ficam fora do cálculo de cobertura. Para ajustar algo
específico do projeto, use `sonar-args` (ex.: `-Dsonar.exclusions=...`).

`sonar.sources` fica no padrão — a raiz do repositório — para que templates,
CSS e JS também sejam analisados, não só o Python.

O `SONAR_HOST_URL` **não** deve ser declarado: ausente, o action assume o
SonarQube Cloud.

## Pré-requisitos no projeto

O pipeline instala `ruff`, `mypy`, `bandit`, `pip-audit`, `pytest`,
`pytest-django`, `pytest-cov`, `liccheck`, `pip-licenses`, `cyclonedx-bom`,
`pytest-playwright` e `axe-core` por conta própria — não precisam estar no
`requirements.txt`.

O que o projeto precisa ter:

- `pytest-django` configurado (`DJANGO_SETTINGS_MODULE` em `pytest.ini`,
  `setup.cfg` ou `pyproject.toml`), ou os testes não encontram os settings;
- `manage.py` na raiz, para as checagens do Django;
- com `run-lock`: um `requirements.txt` de **pins exatos** e um lock gerado por
  `scripts/gerar_lock.py`;
- com `run-e2e`: o marcador `e2e` declarado no `pytest.ini`;
- com `run-a11y`: `a11y-paths` preenchido — sem isso o job falha dizendo isso.

## Scripts compartilhados

Ficam em [`scripts/`](scripts/) e chegam aos projetos pelo checkout em
`.ci-shared`. Todos rodam sozinhos e aceitam `--help`.

| Script | Para quê |
|---|---|
| `gerar_lock.py` | Gera o lock com hashes. Roda **na sua máquina** — acessa o PyPI |
| `conferir_lock.py` | Confere lock × `requirements.txt`. Roda no CI, sem rede |
| `a11y.py` | Injeta o axe-core numa lista de páginas e relata por impacto |
| `conferir_licencas_instaladas.py` | Aplica a política a um venv já instalado, sem tocá-lo |
| `diff_quality.py` | Cobra qualidade só nas linhas que o PR adiciona, contra o merge-base |
| `layout.py` | Mede a página em várias larguras e acusa overflow, corte e cobertura |
| `frontend_relatorio.py` | Junta Stylelint, ESLint e djlint num veredito e num resumo só |
| `frota.py` | Churn histórico e duplicação entre repositórios (relatório mensal) |
| `css_inalcancavel.py` | Acha e remove regra de CSS que a cascata já tornou inalcançável |

O último existe por um limite do `liccheck`: ele lê os metadados pelo
`pkg_resources` do próprio interpretador e não tem equivalente ao `--python` do
`pip-licenses`. Auditar um venv de produção com ele exigiria instalá-lo lá
dentro — mexer no ambiente que está no ar só para medi-lo. O script contorna
isso lendo o venv de fora e aplicando a mesma política.

Projeto que ainda usa `manage.py test` deve começar com `soft-fail: "pytest"`
até migrar.

## Armadilhas conhecidas

Coisas que já custaram uma sessão de depuração. Todas verificadas na prática.

**`ALLOWED_HOSTS` sem `127.0.0.1` fazia o a11y e o layout medirem a tela de
erro do Django — em silêncio.** O servidor das duas etapas responde em
`127.0.0.1`; um projeto cujo `test-env` não inclua esse host devolve
`DisallowedHost` (HTTP 400) em toda rota. E a página de erro do Django **tem**
layout: tabelas largas de `META` que estouram qualquer viewport de celular.

O resultado era pior do que uma falha: o job passava, o relatório vinha cheio,
e os achados apontavam `table.meta` e `table.req` — seletores que não existem
em nenhum dos projetos. Três repositórios diferentes acusavam números
**idênticos** de quebra de layout, o que era a pista: eram a mesma página de
erro. No axe o efeito era o oposto e igualmente ruim — a tela de erro passa
quase limpa, então a acessibilidade ficava verde sem ter auditado nada.

Hoje os dois scripts conferem o status da resposta e reprovam a rota com
4xx/5xx, citando `ALLOWED_HOSTS` na mensagem. A lição geral: **numa etapa que
mede uma página, "carregou" não é o mesmo que "é a página certa"** — e um
relatório cheio de achados não prova que a ferramenta olhou para o lugar certo.

**O djlint troca de formato dentro do GitHub Actions.** No terminal ele imprime
um bloco legível — cabeçalho com o nome do arquivo, depois `CODIGO linha:coluna
mensagem`. Detectando o Actions, passa a emitir `::warning file=...,line=...`.
O consolidador lia só o primeiro formato, e no primeiro PR de teste o único
achado de template sumiu da contagem sem nenhum erro no log: a etapa devolveu
"zero achados", que é indistinguível de "está limpo". Hoje o parser aceita os
dois, e `tests/test_frontend_relatorio.py` cobre cada um.

Vale a regra geral: numa etapa que agrega ferramentas, zero achados precisa ser
uma afirmação verificada, não o que sobra quando o parser não entende a saída.

**Fixar o workflow por SHA não fixa os scripts junto — `ci-ref` precisa
acompanhar.** Todo checkout de `.ci-shared` obedece ao input `ci-ref`,
independente do commit de onde o workflow veio. Um projeto que fixe o `uses:`
no SHA de um commit de trabalho e não passe `ci-ref` roda o YAML daquele commit
com os scripts de outro. O sintoma é `No such file or directory` em vários
scripts de uma vez, com a etapa de a11y passando ao lado, porque o `a11y.py`
existe nos dois commits.

**`github.job_workflow_sha` não resolve isso, apesar de documentado.** Foi a
primeira tentativa de correção: fazer o `ci-ref` vazio cair no commit do
próprio arquivo de workflow. Em execução real a variável voltou **vazia**, e o
`actions/checkout` com `ref: ''` cai silenciosamente no branch padrão do
`rigst/ci` — trocando o sintoma "scripts da v1" por "scripts da main", que é
pior. A expressão hoje tem três degraus (`ci-ref` → `job_workflow_sha` → `v1`),
mas na prática é o primeiro que decide. `github.workflow_sha` também não serve:
num workflow reutilizável ele devolve o commit do chamador.

O que de fato protege é a guarda: cada job que usa `.ci-shared` confere os
arquivos de que precisa antes de usá-los, e imprime a ref pedida, o
`job_workflow_sha` e o commit efetivo do checkout. Duas rodadas de CI foram
gastas antes dela existir, procurando o erro no lugar errado.

O `-ignore` no `actionlint.yml` continua necessário: a ferramenta 1.7.12 ainda
não tem `job_workflow_sha` na tabela de contexto.

**O Stylelint escreve o relatório em stderr quando encontra alguma coisa.** Um
`2>/dev/null` no passo apaga exatamente o caso que interessa e devolve
silêncio — que se parece com sucesso. Rodar sem achado nenhum imprime em
stdout; com achados, some. Por isso o job usa `--output-file` e o veredito sai
de ler o arquivo, nunca do pipe.

**`npm install --no-save` chamado duas vezes apaga o que a primeira instalou.**
Sem `package.json`, cada chamada reescreve a árvore a partir de um manifesto
vazio. Instalando `eslint` e depois `globals` em passos separados, o binário do
eslint deixa de existir e o job falha dizendo que o comando não foi encontrado —
mensagem que não aponta para a instalação anterior. Os três pacotes vão numa
chamada só.

**Glob de CSS a partir da raiz do repositório varre o `venv/` inteiro.** Um
`stylelint "**/*.css"` no topo de um projeto Django não devolve erro: fica
percorrendo dezenas de milhares de arquivos até o timeout do job. Daí o input
`frontend-paths`, que nomeia os diretórios de origem.

**O `--configuration` do djlint ignora o arquivo em silêncio se ele tiver a
tabela `[tool.djlint]`.** O cabeçalho só vale dentro de um `pyproject.toml`; no
arquivo avulso, o djlint lê, não reclama de nada e descarta as chaves. O
sintoma é o lint rodando com o conjunto de regras padrão como se a config não
existisse. `configs/djlint.toml` é TOML puro por isso.

**O `--include` do djlint adiciona regras, não restringe.** Passar
`--include "H020,H025"` esperando limitar o conjunto traz H020 e H025 *além* das
regras já ativas — no `sistema_trilhas` isso somou 41 ocorrências de T003 que
não apareciam antes. A restrição se faz por `ignore`. Como a lista de exclusão
é aberta por natureza, a versão do djlint é pinada: sem o pin, uma regra nova
numa versão futura reprovaria nove repositórios no primeiro bump do Dependabot.

**`client.get()` do test client do Django tem a forma de uma chamada de rede.**
A regra `sem-timeout` do `diff_quality.py` chegou a tratar `client` como módulo
HTTP e produziu 21 falsos positivos em três arquivos de teste do dojo — mais
achados falsos do que verdadeiros na frota inteira. `client` e `cliente` saíram
da heurística.

**Lista de globais de navegador escrita à mão gera `no-undef` falso.** A
primeira versão de `configs/eslint.config.mjs` listava os globais manualmente
para poupar uma dependência; a medição na frota devolveu sete `no-undef`, cinco
dos quais eram globais legítimos esquecidos (`EventSource`, `NodeFilter`,
`DataTransfer`, `FontFace`, `HTMLFormElement`). A config usa o pacote `globals`,
e à mão ficam só as bibliotecas carregadas por `<script>` nos templates.

**`no-cond-assign: always` acusa o laço idiomático de regex.** As três únicas
ocorrências da frota eram `while ((m = re.exec(s)) !== null)`, e nenhuma era
defeito. A config usa `except-parens`, em que os parênteses extras já são a
declaração de intenção que a regra pede.

**O `diff-quality` precisa de `fetch-depth: 0`.** Com o checkout raso o
merge-base não existe no clone, e `git diff base...HEAD` falha com erro de
revisão desconhecida — que não se parece nem um pouco com a causa. O job já
pede o histórico completo; a armadilha aparece ao copiar o passo para outro
lugar.

**Renomear o branch padrão no GitHub não propaga para o SonarQube Cloud.** Ele
guarda o nome do branch principal por projeto, definido na importação. Depois de
renomear, o job fica **verde** mas nenhuma análise nova aparece — o scanner só
envia a tarefa e sai, sem saber se ela foi aceita. Diagnóstico sem token:

```bash
curl -s "https://sonarcloud.io/api/measures/component?component=<key>&branch=<novo>&metricKeys=ncloc"
```

`Organization is not allowed to access data from non main branches` confirma o
descasamento. Corrija em *Administration* → *Branches and Pull Requests* →
renomear o branch principal. O Codecov não sofre disso.

**Análise Automática e análise por CI se excluem.** Se a Automática estiver
ligada no projeto, a do CI é recusada. Desligue em *Administration* →
*Analysis Method*.

**O Codecov exige token mesmo em repositório público.** O upload sem
autenticação não existe mais no GitHub Actions; sem `CODECOV_TOKEN` o passo
falha com `Token required - not valid tokenless upload`. O token é por
repositório; o do Sonar é por conta e serve para todos.

**`SONAR_HOST_URL` ausente é o correto** para o SonarQube Cloud. Declarar a
variável aponta o scanner para outro lugar e quebra a análise.

**`ruff --fix` desliga signals do Django se o `apps.py` não estiver protegido.**
O `ready()` importa o módulo de signals só pelo efeito colateral de registrar os
receivers; para o ruff é import não usado, e o `--fix` troca por `pass`. O build
continua verde se nenhum teste cobrir aquele signal. O baseline em
[`configs/ruff.toml`](configs/ruff.toml) já ignora `F401` em `**/apps.py`, mas
projeto com config própria precisa repetir a exceção. Depois de rodar `--fix`
pela primeira vez num projeto, confira:

```bash
grep -A3 "def ready" */apps.py | grep -B1 pass
```

**`playwright install --with-deps` trava quando o espelho de apt do runner
está fora.** A flag dispara um `apt-get update`, e com o
`azure.archive.ubuntu.com` respondendo `Ign:` o passo fica repetindo o
fallback por vinte minutos, sem uma linha de erro. Três execuções seguidas do
`site_stolben` pararam no mesmo ponto, em runners diferentes, enquanto o mesmo
job no `sistema_trilhas` passava em três minutos. Por isso o `--with-deps`
saiu: a imagem do `ubuntu-latest` já traz as bibliotecas de sistema do
navegador, e sem a flag os jobs de `e2e` e `a11y` não tocam mais o apt. Se
algum dia faltar um `.so`, o sintoma é o navegador não abrir, e aí a flag
volta. O `timeout-minutes: 20` nos dois jobs existe para que uma trava dessas
reprove rápido em vez de segurar o runner por horas.

**Input opcional interpolado sozinho no corpo de um `if` quebra o passo.**
Um `${{ inputs.algo }}` que forma a única linha do bloco `then` deixa o bloco
**sem nenhum comando** quando o input está vazio — e `if ...; then fi` é erro de
sintaxe no bash, não bloco vazio. O passo morre com

```
syntax error near unexpected token `fi'
```

depois de os comandos anteriores já terem rodado, o que aponta a suspeita para o
lugar errado. Aconteceu com o `a11y-setup-command`. A forma segura é passar por
uma variável primeiro:

```bash
SETUP="${{ inputs.a11y-setup-command }}"
if [ -n "$SETUP" ]; then
  bash -c "$SETUP"
fi
```

O idioma usado nos outros passos (`if [ -n "${{ inputs.x }}" ]; then pip install
-r "${{ inputs.x }}"; fi`) não sofre disso: o corpo tem texto literal além da
interpolação, então nunca fica vazio.

**`live_server` + Playwright exige `DJANGO_ALLOW_ASYNC_UNSAFE`.** Sem a
variável, a suíte e2e morre com `SynchronousOnlyOperation: You cannot call this
from an async context` — durante a **criação do banco de teste**, antes de
qualquer teste rodar, e sem mencionar nem o Playwright nem o `live_server`.

A causa é que a API síncrona do Playwright deixa um event loop montado na
thread, e o Django recusa operação síncrona de banco na presença dele. Aqui a
proteção não protege de nada: as chamadas continuam na mesma thread, e o loop é
só o driver do navegador. O job `e2e` já exporta a variável; fora de teste ela
não deve ser usada.

**No e2e, semeie os dados dentro do teste.** O `live_server` roda o servidor
noutra thread, então ele não enxerga uma transação de teste comum — é por isso
que o fixture já traz `transactional_db`. View que depende de registro
publicado devolve 404 num banco recém-criado, e o sintoma parece rota errada.
Aconteceu com `/termos/` no `sistema_arq`: a view levanta `Http404` de
propósito enquanto não houver `DocumentoLegal` vigente. O mesmo vale para o job
`a11y`, e é para isso que existe o `a11y-setup-command`.

**O `liccheck` quebra com `setuptools` 81 ou mais novo.** Ele importa
`pkg_resources`, que o setuptools removeu — e como os venvs do Python 3.12+ já
não trazem setuptools, o sintoma é um `ModuleNotFoundError: No module named
'pkg_resources'` que não menciona o liccheck em lugar nenhum. O job instala
`liccheck "setuptools<81"` junto por causa disso. Não adianta instalar
setuptools sem o pino: a versão atual é justamente a que não tem o módulo.

**O `liccheck` compara licença por igualdade exata, não por substring.** O modo
regex existe, mas só sob `--as-regex`, que este pipeline não usa. Antes de
comparar ele: prefere os `Classifier: License ::` ao campo `License`; remove
**um** sufixo `" license"` do fim; divide em `" OR "`; e passa a minúsculas.

Duas consequências que custam tempo:

- escrever `mit license` na política é inútil — essa string nunca chega à
  comparação, porque a normalização já a transformou em `mit`. As entradas de
  [`configs/liccheck.ini`](configs/liccheck.ini) estão na forma
  pós-normalização, e é por isso que a lista parece redundante: o mesmo pacote
  aparece como `BSD License` ou `BSD-3-Clause` conforme declare classifier
  antigo ou expressão SPDX.
- a divisão é em `" OR "`, **não em `" AND "`**. Um pacote sob
  `Apache-2.0 AND MIT` chega inteiro à comparação e precisa estar listado
  assim, mesmo com as duas metades já autorizadas. Aconteceu com `aiohttp` e
  `greenlet`.

**Pacote sem classifier de licença cai no campo `License` em texto corrido.**
`pymupdf` declara `Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial
License`, e `pypdfium2` declara `BSD-3-Clause, Apache-2.0, dependency
licenses`. Nenhuma normalização transforma isso em SPDX. As duas estão
autorizadas como literal, com a justificativa em comentário na política — no
caso do `pymupdf`, com o registro de que optar pelo ramo AGPL é decisão de mão
única: enquanto ele estiver embarcado, o projeto não pode ser relicenciado para
nada mais permissivo.

**Não chame `response.close()` dentro de uma `TestCase`.** O `close()` dispara
`request_finished`, cujo receiver `close_old_connections` fecha a conexão do
banco — dentro do `atomic` da `TestCase` o autocommit diverge do configurado, e
o Django trata isso como conexão suspeita. Fechada dentro do atomic, ela não é
reaberta, e **todo teste seguinte da mesma classe** morre com
`the connection is closed`. Em SQLite o sintoma desaparece, porque fechar um
banco em memória é no-op — o que faz parecer divergência entre bancos quando é
defeito do teste. Para consumir um `FileResponse`, itere
`response.streaming_content`: o test client embrulha o iterador num
`closing_iterator_wrapper` que desconecta o receiver antes de fechar.

## Deploy contínuo (CD)

`deploy-django.yml` e `deploy-static.yml` fecham o elo que faltava depois do
CI: quando o CI conclui com sucesso em `main`, um `deploy.yml` no próprio
projeto dispara, via SSH, o `deploy/cd-deploy.sh` que já mora naquele
repositório. Este repositório só entrega o "discador" (a mecânica de SSH,
`concurrency` e o comando forçado) — a lógica de deploy de cada app fica
versionada e revisável no próprio projeto, não aqui.

```yaml
# .github/workflows/deploy.yml
name: CD

on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]

jobs:
  deploy:
    if: >
      github.event.workflow_run.conclusion == 'success' &&
      github.event.workflow_run.head_branch == 'main' &&
      github.event.workflow_run.event == 'push'
    uses: rigst/ci/.github/workflows/deploy-django.yml@v1
    secrets:
      CD_SSH_KEY: ${{ secrets.CD_SSH_KEY }}
    with:
      ssh-host: "app.exemplo.com"
```

Para site estático, troque por `deploy-static.yml@v1` — mesma estrutura, sem
`services`/healthcheck. Para adotar num projeto novo (gerar a chave, criar o
`cd-deploy.sh`, registrar o secret), siga o **RUNBOOK.md**, seção 7.

## Versionamento

Os projetos apontam para a tag móvel `@v1`, que acompanha correções
compatíveis. Mudança que quebre contrato de input sai como `v2`.

Os workflows de CD, por terem acesso a credenciais de produção, são exceção
recomendada a essa regra: fixe por SHA (`@<sha>  # v1`) nos consumidores, como
já é a prática em `sistema_arq`/`sistema_financas`/`sistema_orcamentos` para o
`python-django.yml`.

Mover a `v1` publica para todos os projetos de uma vez:

```bash
git tag -f v1 && git push -f origin v1
```

## Licença

[MIT](LICENSE).
