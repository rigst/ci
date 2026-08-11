# Runbook

Procedimentos repetíveis para os projetos que usam este CI. Escrito a partir do
que foi executado em `sistema_trilhas`, `sistema_arq` e `site_stolben` — os
comandos abaixo são os que rodaram de verdade, não um esboço.

Substitua `PROJETO` pelo nome do repositório em cada bloco.

---

## 1. Adotar o CI num projeto

### 1.1 Criar o chamador

`.github/workflows/ci.yml` no projeto:

```yaml
name: CI

# O pipeline vive em github.com/rigst/ci. Ajuste lá vale para todos os projetos.

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
      source-paths: "app1 app2 config"
      soft-fail: "mypy,pytest,bandit,pip-audit"
```

Comece com tudo em `soft-fail`. A primeira execução mostra o tamanho real do
passivo; retire da lista conforme cada etapa zerar. Ligar tudo de uma vez num
projeto existente produz um build vermelho que ninguém olha.

Para site estático, troque por `static-site.yml@v1`, que não recebe parâmetros.

### 1.2 Ajustar o que a primeira execução acusar

Os tropeços recorrentes, em ordem de frequência:

| Sintoma | Causa | Correção |
|---|---|---|
| `exit code 5` no pytest | nenhum teste coletado | `python_files = tests.py tests_*.py test_*.py` no `pytest.ini` |
| Erros de `SECURE_SSL_REDIRECT`, TLS ou `ALLOWED_HOSTS` nos testes | testes rodando nos settings de produção | `test-settings-module` e `test-env` |
| `check --deploy` falha em aviso deliberado | `--fail-level WARNING` | `django-check-fail-level: ERROR`, com o motivo em comentário |
| Conexão recusada em Redis/Celery | projeto espera serviço externo | esvazie as URLs no `test-env` e deixe o fallback do projeto agir |
| `files.E001` no `check` | `FILE_UPLOAD_TEMP_DIR` aponta para `media/tmp`, que não é versionado | torne a setting configurável por env e aponte para `/tmp` no CI |
| `ValueError: Unable to configure handler 'file'` | handler de log abre o arquivo ao importar os settings | `LOG_DIR=/tmp` no `django-env` |
| `PermissionError: /var/www/...` nos testes | `STATIC_ROOT`/`MEDIA_ROOT` apontam para o servidor | aponte para `/tmp/...` no `test-env` |
| `FileNotFoundError` de binário (`pdftoppm`, `gs`) | dependência de sistema | `apt-packages: "poppler-utils"` |

### 1.2.1 Depois do primeiro `ruff --fix`, confira os signals

Esta é a mais perigosa da lista, porque **não falha o build**:

```bash
grep -A3 "def ready" */apps.py | grep -B1 pass
```

O `ready()` do `AppConfig` importa o módulo de signals só pelo efeito colateral
de registrar os receivers. Para o ruff é import não usado, e o `--fix` troca a
linha por `pass`, desligando todos os signals do app em silêncio. O baseline
compartilhado já ignora `F401` em `**/apps.py`, mas projeto com config própria
precisa repetir a exceção. Aconteceu em `sistema_orcamentos`: o recálculo de
totais parou, e só um teste de domínio pegou.

### 1.2.2 Dependência desatualizada esconde CVE

**Faixa não atualiza nada sozinha.** Declarar `Django>=6.0,<7.0` só permite que
uma versão nova entre quando alguém roda `pip install -U`; o venv de produção
fica parado onde estava. `sistema_trilhas` e `sistema_questoes` declaravam faixa
e mesmo assim seguiam no 6.0.6 com três CVEs, enquanto os projetos de pin exato
já estavam corrigidos — foi essa suposição errada que criou o ponto cego. Pin
exato e faixa envelhecem igual; muda só o comando que corrige.

O `pip-audit` do pipeline audita o `requirements.txt`, **não o venv que está
rodando**. Os dois divergem. Para auditar produção de verdade:

```bash
/var/www/PROJETO/venv/bin/pip freeze > /tmp/prod.txt
pip-audit -r /tmp/prod.txt
```

**Ao corrigir CVE, prenda o minor.** `pip install -U -r requirements.txt` numa
faixa `<7.0` traz o minor seguinte (6.0.6 → **6.1**), que é mudança de
comportamento no meio de um hotfix de segurança. Peça a série explicitamente:

```bash
venv/bin/pip install -U -r requirements.txt "Django>=6.0,<6.1" cryptography
```

`cryptography` entra à mão porque é dependência transitiva: não está no
`requirements.txt`, e o `-U` não alcança o que não está listado.

Migração de minor é trabalho à parte, com a suíte rodada antes.

Nunca forje variável de ambiente só para calar uma checagem de segurança: isso
produz verde falso. Se o aviso é deliberado, suba o `fail-level` e registre por
quê.

### 1.3 Ligar a proteção de branch

Só depois que o gate `ci / CI` estiver verde ao menos uma vez — a proteção exige
um check que precisa existir.

```bash
cat > /tmp/prot.json <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["ci / CI"] },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
gh api -X PUT repos/rigst/PROJETO/branches/main/protection --input /tmp/prot.json
```

Conferir:

```bash
gh api repos/rigst/PROJETO/branches/main/protection \
  --jq '.required_status_checks.contexts, .enforce_admins.enabled'
```

**O nome do check é `ci / CI`, não `CI`.** O prefixo é o nome do job no
chamador. Errar isso deixa o branch travado esperando um check que nunca chega.
Para descobrir o nome exato num repositório:

```bash
SHA=$(gh api repos/rigst/PROJETO/commits/main --jq '.sha')
gh api "repos/rigst/PROJETO/commits/$SHA/check-runs" --jq '.check_runs[].name'
```

No repositório `ci` o check chama-se `actionlint`.

**`enforce_admins: false` é deliberado**: o dono continua podendo dar push
direto em `main`, enquanto contribuição externa passa obrigatoriamente por PR
com CI verde. Para fechar também para o dono — o que obriga PR para tudo,
inclusive correção de uma linha — troque para `true`.

**Repositório privado não aceita proteção de branch** no plano gratuito: a API
responde `Upgrade to GitHub Pro or make this repository public`. Ou o
repositório abre, ou a proteção fica de fora — não há meio-termo.

**Não exija `SonarCloud Code Analysis`.** Esse check vem do app do SonarCloud e
reflete o Quality Gate, que fica vermelho enquanto houver dívida antiga.
Exigi-lo trava todo merge por motivo não relacionado à mudança.

---

## 2. Codecov e SonarQube Cloud

### 2.1 Codecov

Token **por repositório**, em [codecov.io](https://codecov.io) → projeto →
*Settings* → *Repository Upload Token*.

```bash
gh secret set CODECOV_TOKEN --repo rigst/PROJETO
```

Sem `--body`: o `gh` pede o valor de forma oculta e o token não fica no
histórico do shell.

É obrigatório mesmo em repositório público — o upload sem autenticação não
existe mais no GitHub Actions.

### 2.2 SonarQube Cloud

Token **por conta**, o mesmo serve para todos os projetos. Avatar → *My Account*
→ *Security* → *Generate Tokens*.

```bash
gh secret set SONAR_TOKEN --repo rigst/PROJETO
```

No `ci.yml`:

```yaml
      run-sonar: true
      sonar-project-key: rigst_PROJETO
      sonar-organization: rigst
```

Em cada projeto no SonarCloud, desligue **Automatic Analysis** em
*Administration* → *Analysis Method*: ela e a análise por CI se excluem.

Só funciona em repositório público (limite do plano gratuito).

### 2.3 Depois de renomear um branch

O SonarCloud guarda o nome do branch principal por projeto, definido na
importação, e **não acompanha o rename feito no GitHub**. O sintoma é cruel: o
job fica verde e nenhuma análise nova aparece.

```bash
# Se responder "not allowed to access data from non main branches", é isso.
curl -s "https://sonarcloud.io/api/measures/component?component=rigst_PROJETO&branch=main&metricKeys=ncloc"
```

Corrija em *Administration* → *Branches and Pull Requests* → renomear o branch
principal. O Codecov não sofre disso.

---

## 3. Deploy de rotina

Vale para os projetos Django em `/var/www`, servidos por systemd + Gunicorn com
virtualenv. **Não há Docker em produção**, apesar de algum `DEPLOY.md` antigo
descrever esse modelo.

Layout: `sistema_trilhas` fica direto em `/var/www/sistema_trilhas`;
`sistema_arq` usa `/var/www/sistema_arq/current` com `venv/` e `shared/.env` ao
lado.

### 3.1 Antes de puxar

```bash
D=/var/www/PROJETO
git -C $D fetch origin
git -C $D status -sb                       # quantos commits atrás, o que está modificado
git -C $D rev-parse --short HEAD           # ANOTE: é o ponto de rollback
git -C $D diff --name-only HEAD..origin/main -- '*/migrations/*'   # vazio = sem migração
git -C $D diff --name-only HEAD..origin/main -- requirements.txt   # vazio = sem pip install
git -C $D diff --name-only HEAD..origin/main -- 'static/*' 'templates/*'
```

Com migração no meio, gere backup do banco antes. Sem migração, o deploy é
código puro e o rollback é trivial.

Se `status` acusar arquivo modificado, confirme que o conteúdo já está publicado
antes de descartar:

```bash
git -C $D diff --quiet origin/main -- CAMINHO && echo "idêntico ao publicado — seguro descartar"
git -C $D checkout -- CAMINHO
```

### 3.2 Puxar e verificar

```bash
git -C $D pull --ff-only origin main
```

Os comandos do Django precisam do ambiente de produção, e cada projeto o
carrega de um jeito:

```bash
# sistema_trilhas — carrega .env sozinho via python-dotenv; basta os settings
cd /var/www/sistema_trilhas
DJANGO_SETTINGS_MODULE=config.settings.production ./venv/bin/python manage.py check --deploy
DJANGO_SETTINGS_MODULE=config.settings.production ./venv/bin/python manage.py migrate --check

# sistema_arq — lê do ambiente do systemd; exporte o .env compartilhado
cd /var/www/sistema_arq/current
set -a && . /var/www/sistema_arq/shared/.env && set +a
/var/www/sistema_arq/venv/bin/python manage.py check --deploy --fail-level ERROR
/var/www/sistema_arq/venv/bin/python manage.py migrate --check
```

`migrate --check` sai diferente de zero se houver migração não aplicada — é a
confirmação de que o banco está no ponto do código.

Se `static/` mudou:

```bash
DJANGO_SETTINGS_MODULE=config.settings.production ./venv/bin/python manage.py collectstatic --noinput
```

### 3.3 Reiniciar

```bash
sudo systemctl restart trilhas.service trilhas_celery.service
sudo systemctl restart sistema_arq.service sistema_arq_celery.service sistema_arq_celerybeat.service

systemctl is-active trilhas.service
curl -s -o /dev/null -w "%{http_code}\n" https://trilhas.stolben.com
```

Site estático (`site_stolben`) não tem serviço: o nginx serve os arquivos do
diretório, e o `pull` já concluiu o deploy.

### 3.4 Rollback

Sem migração no meio, é voltar o código e reiniciar:

```bash
git -C $D reset --hard SHA_ANOTADO
sudo systemctl restart SERVICO
```

Com migração aplicada, não reverta a migração às cegas: restaure banco e mídia
do mesmo ponto no tempo.

---

## 4. Ordem recomendada num projeto novo

1. Chamador com tudo em `soft-fail` → primeira execução → medir o passivo
2. Zerar `ruff` (é o mais rápido: `ruff check --fix` e `ruff format`)
3. Fazer o `pytest` coletar e passar
4. Retirar do `soft-fail` o que já estiver limpo
5. `CODECOV_TOKEN`, depois `SONAR_TOKEN`
6. Proteção de branch exigindo `ci / CI`
7. Deploy
