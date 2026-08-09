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

Os jobs rodam em paralelo; só o `sonar` espera o `pytest`, porque precisa do
`coverage.xml`. O job final `resultado` consolida tudo — **é ele que deve ser
exigido no branch protection**, não os sete individualmente.

## Adoção gradual: `soft-fail`

Ligar sete checagens de uma vez num projeto existente trava o merge no primeiro
dia. O input `soft-fail` recebe uma lista separada por vírgula das etapas que
rodam e reportam sem derrubar o build:

```yaml
with:
  soft-fail: "mypy,pytest"
```

Valores aceitos: `ruff`, `mypy`, `pytest`, `bandit`, `pip-audit`, `gitleaks`,
`django`. O padrão é `mypy`, porque tipar um projeto Django existente é o item
mais demorado da lista. Conforme cada etapa zera, tire-a da lista.

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
| `ci-ref` | `"v1"` | Ref deste repo de onde vêm os configs |

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
`pytest-django` e `pytest-cov` por conta própria — não precisam estar no
`requirements.txt`.

O que o projeto precisa ter:

- `pytest-django` configurado (`DJANGO_SETTINGS_MODULE` em `pytest.ini`,
  `setup.cfg` ou `pyproject.toml`), ou os testes não encontram os settings;
- `manage.py` na raiz, para as checagens do Django.

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
