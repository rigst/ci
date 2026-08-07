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

## O que roda

| # | Etapa | Job | Ferramenta |
|---|-------|-----|-----------|
| 1 | Lint rápido | `ruff` | `ruff check` + `ruff format --check` |
| 2 | Tipos | `mypy` | `mypy` + `django-stubs` |
| 3 | Testes e cobertura | `pytest` | `pytest-cov` → Codecov (e artefato p/ Sonar) |
| 4 | Segurança | `security` | `bandit` (código) + `pip-audit` (dependências) |
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
| `django-check-fail-level` | `WARNING` | Nível que faz `check --deploy` falhar |
| `postgres` | `true` | Sobe PostgreSQL para os testes |
| `postgres-version` | `"16"` | Tag da imagem |
| `coverage-fail-under` | `0` | Cobertura mínima; `0` desliga |
| `soft-fail` | `"mypy"` | Etapas que reportam sem bloquear |
| `run-ruff` … `run-django-checks` | `true` | Liga/desliga cada etapa |
| `run-codecov` | `true` | Envia cobertura ao Codecov |
| `run-sonar` | `false` | Roda o SonarQube Cloud |
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
precisa ser público ou ter plano pago, e precisa de um `sonar-project.properties`
na raiz:

```properties
sonar.organization=rigst
sonar.projectKey=rigst_nome-do-projeto
sonar.sources=.
sonar.exclusions=**/migrations/**,**/static/**,**/node_modules/**
sonar.python.coverage.reportPaths=coverage.xml
sonar.python.version=3.12
```

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

## Versionamento

Os projetos apontam para a tag móvel `@v1`, que acompanha correções
compatíveis. Mudança que quebre contrato de input sai como `v2`.

## Licença

[MIT](LICENSE).
