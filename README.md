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

Os jobs rodam em paralelo; só o `sonar` espera o `pytest`, porque precisa do
`coverage.xml`. O job final `resultado` consolida tudo — **é ele que deve ser
exigido no branch protection**, não os doze individualmente.

**As etapas 8 a 12 nascem desligadas.** As sete primeiras valem para qualquer
projeto Django sem configuração; estas cinco não. Quatro exigem alguma coisa do
repositório (um lock gerado, um teste marcado, uma lista de rotas para
auditar), e mesmo as que não exigem mudariam o veredito de pipelines que hoje
estão verdes. Ligá-las por padrão faria a próxima subida da tag `v1` quebrar
sete repositórios ao mesmo tempo. A ordem de adoção está no
[RUNBOOK](RUNBOOK.md#5-ligar-as-checagens-de-conformidade).

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
| `ci-ref` | `"v1"` | Ref deste repo de onde vêm os configs e scripts |

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

O último existe por um limite do `liccheck`: ele lê os metadados pelo
`pkg_resources` do próprio interpretador e não tem equivalente ao `--python` do
`pip-licenses`. Auditar um venv de produção com ele exigiria instalá-lo lá
dentro — mexer no ambiente que está no ar só para medi-lo. O script contorna
isso lendo o venv de fora e aplicando a mesma política.

Projeto que ainda usa `manage.py test` deve começar com `soft-fail: "pytest"`
até migrar.

## Armadilhas conhecidas

Coisas que já custaram uma sessão de depuração. Todas verificadas na prática.

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

## Versionamento

Os projetos apontam para a tag móvel `@v1`, que acompanha correções
compatíveis. Mudança que quebre contrato de input sai como `v2`.

Mover a `v1` publica para todos os projetos de uma vez:

```bash
git tag -f v1 && git push -f origin v1
```

## Licença

[MIT](LICENSE).
