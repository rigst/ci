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
8. Conformidade (seção 5), na ordem barato → caro

---

## 5. Ligar as checagens de conformidade

As etapas 8 a 12 (`licencas`, `sbom`, `lock`, `e2e`, `a11y`) vêm desligadas.
Ligue **uma por vez**, na ordem abaixo — é a ordem do custo de adoção, não a
da importância.

### 5.0 As duas que não pedem nada do projeto

`licencas` e `sbom` funcionam em qualquer repositório sem preparação:

```yaml
      run-licencas: true
      run-sbom: true
      soft-fail: "licencas"     # até o primeiro relatório sair limpo
```

Se o `liccheck` reprovar, leia o achado antes de mexer na política. Só há dois
desfechos legítimos, e nenhum deles é afrouxar o nível:

1. a licença é compatível com AGPL-3.0, mas está escrita numa forma que a
   normalização não reconhece → acrescente a **string literal** em
   `configs/liccheck.ini`, com o motivo em comentário, e publique o `rigst/ci`;
2. a licença não é compatível → o pacote sai. Trocar de dependência é o
   trabalho; não existe configuração que resolva isso.

### 5.1 Converter faixa em pin exato

Pré-requisito do `lock`, e correção de um problema que já custou CVE viva em
produção: **faixa não atualiza nada sozinha**. `Django>=6.0,<7.0` só deixa uma
versão nova entrar quando alguém roda `pip install -U`; até lá o venv fica onde
está, com o `requirements.txt` parecendo saudável.

Parta do que está instalado em produção, não do que o arquivo diz — é a versão
que roda hoje, então converter para ela não muda comportamento nenhum:

```bash
/var/www/PROJETO/venv/bin/pip freeze | sort > /tmp/prod.txt
grep -iE '^(django|celery|redis)==' /tmp/prod.txt     # confira as principais
```

Reescreva o `requirements.txt` com `==` **apenas nas dependências diretas** —
as transitivas ficam para o lock, e listá-las à mão vira manutenção eterna.
Preserve os comentários: eles explicam por que cada pacote está ali.

Confirme que nada mudou, e só então gere o lock:

```bash
cd /caminho/do/clone
python -m venv /tmp/v && /tmp/v/bin/pip install -q -r requirements.txt
diff <(/tmp/v/bin/pip freeze | sort) /tmp/prod.txt     # deve sair vazio ou quase

python scripts/gerar_lock.py --python-version 3.12
python scripts/conferir_lock.py
```

`--python-version` é a versão **de produção**, não a da sua máquina: resolver
no 3.12 e instalar no 3.14 pode dar conjuntos diferentes, e sob
`--require-hashes` a diferença vira falha de build em vez de aviso. Se as duas
divergirem, informe também `lock-python-version` no chamador.

Pacote publicado só como sdist precisa sair da resolução:

```bash
python scripts/gerar_lock.py --sdist-only ofxparse=beautifulsoup4,lxml,six
```

No chamador:

```yaml
      run-lock: true
      lock-python-version: "3.14"    # só se produção diferir do pipeline
```

A partir daí, **toda mudança em `requirements.txt` exige regerar o lock**. O
Dependabot não sabe disso — é o job `lock` que segura a divergência.

### 5.2 Primeiro teste e2e

Declare o marcador no `pytest.ini`, senão o pytest avisa que não o conhece:

```ini
markers =
    e2e: teste de ponta a ponta em navegador
```

O `live_server` do `pytest-django` sobe um servidor real; a `page` vem do
`pytest-playwright`. Comece por um caminho que prove que a pilha inteira está
de pé — roteamento, template, estáticos e JS:

```python
import pytest

@pytest.mark.e2e
def test_pagina_de_login_carrega(live_server, page):
    page.goto(f"{live_server.url}/entrar/")
    assert page.locator("form").count() == 1
```

```yaml
      run-e2e: true
      soft-fail: "e2e"
```

Um smoke test só, no começo. Suíte e2e grande envelhece mal, e a primeira
falha intermitente ensina a equipe a ignorar o vermelho.

### 5.3 Medir o passivo de acessibilidade antes de bloquear

Rode uma vez sem reprovar nada, para saber o tamanho do problema:

```yaml
      run-a11y: true
      a11y-fail-on: none
      a11y-paths: |
        /
        /entrar/
```

Baixe o artefato `a11y.json`, veja a distribuição por impacto, corrija os
`critical` e `serious` e só então aperte para `a11y-fail-on: serious`.

Rota que exige login precisa de dados semeados:

```yaml
      a11y-setup-command: python manage.py loaddata ci_a11y
```

Para site estático não há nada a configurar: `a11y-paths` vazio audita todo
`*.html` do repositório.

---

## 6. Conformidade dos venvs de produção (neste servidor)

O job `licencas` do CI audita o **repositório**. O que está instalado no
servidor diverge dele — faixa que não atualizou, transitiva que entrou sem
passar pelo arquivo, correção de CVE feita à mão que nunca voltou para o repo.
Foi assim que o `pymupdf` (AGPL-ou-comercial) chegou ao `sistema_questoes` sem
ninguém decidir nada a respeito.

Quem cobre isso é `/home/rod/scripts/ativos/conformidade_producao.sh`, no cron
às segundas, 3:40 UTC. Para cada `/var/www/*/venv` ele gera o SBOM CycloneDX e
audita as licenças **sem instalar nada no venv** — o `pip-licenses` lê de fora,
e `conferir_licencas_instaladas.py` aplica a mesma política do CI.

```bash
# rodar à mão
/home/rod/scripts/ativos/conformidade_producao.sh

# histórico de SBOM (retenção de 365 dias)
ls /home/rod/conformidade/sbom/

# o que mudou entre duas datas
diff <(jq -r '.components[]|"\(.name) \(.version)"' /home/rod/conformidade/sbom/PROJETO-DATA1.cdx.json) \
     <(jq -r '.components[]|"\(.name) \(.version)"' /home/rod/conformidade/sbom/PROJETO-DATA2.cdx.json)
```

Alerta por e-mail só quando acha algo; silêncio significa limpo — mesma
convenção do `audita_venvs_producao.sh`, que cobre CVE e roda diariamente.

**A política vem do clone em `/home/rod/ci`.** Depois de publicar uma mudança
em `configs/liccheck.ini`, rode `git -C /home/rod/ci pull`, senão o servidor
segue auditando pela lista antiga.

As ferramentas ficam em `/home/rod/conformidade/venv`, separadas tanto dos
venvs de produção quanto do `/home/rod/auditoria-venvs/venv` do `pip-audit` —
atualizar uma auditoria não pode quebrar a outra.

---

## 7. Deploy contínuo (CD)

Automatiza a seção 3 (deploy de rotina): a partir do momento em que este
workflow é adotado, todo push em `main` que passar no CI é implantado
sozinho. Existe um único usuário `deploy` no servidor, compartilhado por
todos os apps — o que muda de um app para outro é a chave SSH (uma por app,
gerada aqui) e o `deploy/cd-deploy.sh` (versionado no próprio projeto).

### 7.1 Criar o usuário `deploy` (uma vez, serve para todos os apps)

```bash
sudo adduser --system --shell /bin/bash --home /home/deploy --ingroup www-data --disabled-password deploy
```

**O shell precisa ser `/bin/bash` (nunca `/usr/sbin/nologin`)**: o comando
forçado do `authorized_keys` (seção 7.3) é executado pelo shell da própria
conta — um shell "nologin" recusa rodar qualquer comando, inclusive o
forçado, e o SSH de deploy simplesmente para de funcionar, sem aviso.

O grupo `www-data` é o mesmo que `rod` já usa: os diretórios de app
(`rod:www-data`, a maioria `2775` com setgid) já ficam graváveis por
`deploy` sem precisar mudar dono nem permissão de nada existente. Confirme
por app antes de depender disso:

```bash
sudo -u deploy touch /var/www/PROJETO/current/.teste && sudo -u deploy rm /var/www/PROJETO/current/.teste
```

(Para checkout direto — sem `current/` — teste na raiz do projeto.)

### 7.1.1 `ExecReload` na unidade web (uma vez por app)

O CD usa `systemctl reload` no serviço que serve HTTP — SIGHUP faz o
gunicorn trocar os workers sem derrubar o socket de escuta, zero downtime
de verdade, em vez de reiniciar (que tem uma janela real de 502 enquanto os
workers novos sobem — visto no piloto). Sem `ExecReload=` na unit, `systemctl
reload` falha na hora (`systemd` não sabe como recarregar um `Type=simple`
sem essa diretiva). Adicione, logo depois do bloco `ExecStart=` (que pode
ter continuação de linha com `\` — insira depois da última):

```ini
ExecReload=/bin/kill -s HUP $MAINPID
```

```bash
sudo systemctl daemon-reload
# confirma que é reload de verdade, não restart disfarçado:
antes=$(systemctl show -p MainPID --value PROJETO.service)
sudo systemctl reload PROJETO.service
depois=$(systemctl show -p MainPID --value PROJETO.service)
[ "$antes" = "$depois" ] && echo "OK: mesmo master, reload gracioso"
```

Só o serviço **web** usa `reload`. Celery (worker/beat) continua com
`restart` — não serve HTTP ao vivo, então a janela de restart não é visível
pra ninguém, e `reload` não traz benefício ali.

### 7.2 Sudoers do `deploy` — arquivo novo, nunca edite o do `rod`

`/etc/sudoers.d/deploy-cd`, uma linha por unidade systemd real (sudoers casa
por string exata; comandos com múltiplos argumentos não casam com uma regra
por unidade, por isso o script trata unidade por unidade, nunca todas de
uma vez). O serviço web ganha as duas linhas (`reload` é o que o CD usa;
`restart` fica disponível pra rollback/depuração manual sem precisar de
outra regra depois):

```
deploy ALL=(ALL) NOPASSWD: \
  /usr/bin/systemctl reload PROJETO.service, \
  /usr/bin/systemctl restart PROJETO.service, \
  /usr/bin/systemctl restart PROJETO_celery.service
```

Valide antes de instalar (`visudo -c -f` não aplica, só confere sintaxe — não
precisa que o arquivo já esteja em `/etc/sudoers.d`):

```bash
visudo -c -f /etc/sudoers.d/deploy-cd
```

Cada app novo soma as linhas das suas próprias unidades. Isso é
deliberadamente um arquivo **novo e aditivo**: o `sudo` amplo que `rod` usa
para administração interativa não muda em nada.

### 7.3 Gerar a chave e o comando forçado (uma vez por app)

```bash
APP=PROJETO
mkdir -p /tmp/cd-keys && cd /tmp/cd-keys

ssh-keygen -t ed25519 -C "cd-deploy-$APP" -f "./cd_$APP" -N ""
gh secret set CD_SSH_KEY --repo "rigst/$APP" < "./cd_$APP"

sudo install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
echo "restrict,command=\"/var/www/$APP/current/deploy/cd-deploy.sh\" $(cat "cd_$APP.pub")" \
  | sudo tee -a /home/deploy/.ssh/authorized_keys > /dev/null
sudo chown deploy:deploy /home/deploy/.ssh/authorized_keys
sudo chmod 600 /home/deploy/.ssh/authorized_keys
shred -u "./cd_$APP" "./cd_$APP.pub"
```

`restrict` (OpenSSH ≥ 7.2) equivale a listar
`no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty` — mesmo
efeito, uma palavra só. Ajuste o caminho do `command=` para checkout direto
(sem `current/`).

**Não existe segunda chave para o servidor buscar código no GitHub**: os
projetos deste ecossistema são públicos, então `cd-deploy.sh` busca via HTTPS
anônimo (`git fetch https://github.com/rigst/PROJETO.git main`), sem
credencial nenhuma e sem tocar no remote `origin` (SSH) que `rod` usa
interativamente. Se um projeto futuro for privado, essa premissa cai — volte
a avaliar uma Deploy Key dedicada nesse caso.

Capture a impressão digital do host **uma vez**, fora do workflow — nunca via
`ssh-keyscan` a cada execução, que seria confiar cegamente na rede a cada
deploy:

```bash
ssh-keyscan -H IP_OU_HOSTNAME_DO_VPS > /tmp/known_hosts_vps
gh variable set SSH_KNOWN_HOSTS --repo "rigst/$APP" < /tmp/known_hosts_vps
rm /tmp/known_hosts_vps
```

### 7.4 `deploy/cd-deploy.sh` no projeto

Versionado no próprio app, ao lado dos outros artefatos de `deploy/`. Roda
inteiro como o usuário `deploy` — só o reload/restart no fim precisa de
`sudo` (seção 7.2). Esqueleto (variáveis do topo são o que muda de um app
para outro):

```bash
#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/var/www/PROJETO/current
FETCH_URL=https://github.com/rigst/PROJETO.git
VENV=/var/www/PROJETO/venv
ENV_FILE=/var/www/PROJETO/shared/.env
WEB_SERVICE=PROJETO.service      # reload (SIGHUP) — precisa de ExecReload, seção 7.1.1
OTHER_SERVICES=()                # celery etc., restart — array vazio se não houver
HEALTH_URL=""            # vazio pula o smoke-test
HEALTH_HEADER=""         # NUNCA um token literal aqui — monta depois do source do .env (ver abaixo)
BACKUP_SCRIPT=""         # caminho do backup_postgres.sh, se existir
EXTRA_ENV=""             # variáveis extras, ex.: "DJANGO_ENV=production"
LOCK_FILE=/tmp/PROJETO_cd_deploy.lock

main() {
  local sha
  sha="$(printf '%s' "${SSH_ORIGINAL_COMMAND:-}" | awk '{print $2}')"
  [[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || { echo "SHA inválido: '$sha'"; exit 1; }

  cd "$APP_DIR"
  git fetch "$FETCH_URL" main
  git merge-base --is-ancestor "$sha" FETCH_HEAD \
    || { echo "SHA não é ancestral do main remoto: $sha"; exit 1; }

  local antes; antes="$(git rev-parse HEAD)"

  local tem_migracao tem_requirements
  tem_migracao="$(git diff --name-only "HEAD..$sha" -- '*/migrations/*')"
  tem_requirements="$(git diff --name-only "HEAD..$sha" -- requirements.txt)"

  [[ -n "$tem_migracao" && -n "$BACKUP_SCRIPT" ]] && "$BACKUP_SCRIPT"

  git merge --ff-only "$sha"

  [[ -n "$tem_requirements" ]] && "$VENV/bin/pip" install -r requirements.txt

  set -a
  source "$ENV_FILE"
  [[ -n "$EXTRA_ENV" ]] && eval "export $EXTRA_ENV"
  set +a

  # Token de healthz (se o app tiver um) sai do .env recém-carregado, nunca
  # de uma linha fixa no topo do script — ver armadilha 7.7 (gitleaks pegou
  # um token de verdade hardcoded aqui num app real).
  [[ -n "${DJANGO_HEALTHZ_TOKEN:-}" ]] && HEALTH_HEADER="X-Healthz-Token: $DJANGO_HEALTHZ_TOKEN"

  "$VENV/bin/python" manage.py check --deploy --fail-level ERROR
  "$VENV/bin/python" manage.py migrate --check || "$VENV/bin/python" manage.py migrate
  "$VENV/bin/python" manage.py collectstatic --noinput

  sudo systemctl reload "$WEB_SERVICE"
  for unidade in "${OTHER_SERVICES[@]}"; do
    sudo systemctl restart "$unidade"
  done

  if [[ -n "$HEALTH_URL" ]]; then
    # Com reload o socket nunca cai, então isto deveria passar de primeira —
    # o retry fica como rede de segurança, não porque se espera precisar
    # dele. Numa unidade sem ExecReload (só restart), sem o retry um deploy
    # bom seria reportado como falho por pura corrida (visto no piloto: 502
    # na hora, 200 dois segundos depois).
    local codigo
    for _ in 1 2 3 4 5; do
      codigo="$(curl -s -o /dev/null -w '%{http_code}' ${HEALTH_HEADER:+-H "$HEALTH_HEADER"} "$HEALTH_URL")"
      [[ "$codigo" =~ ^[23][0-9][0-9]$ ]] && break   # 2xx/3xx: app sem /healthz/ pode redirecionar a home pro login
      sleep 2
    done
    [[ "$codigo" =~ ^[23][0-9][0-9]$ ]] || {
      echo "Smoke-test falhou ($codigo). Rollback: git -C $APP_DIR reset --hard $antes"
      exit 1
    }
  fi

  echo "Deploy de $sha concluído (era $antes)."
}

(
  flock -n 9 || { echo "Deploy já em andamento, saindo."; exit 1; }
  main "$@"
) 9>"$LOCK_FILE"
```

Diff de migrations/requirements sempre **antes** do `merge --ff-only` — depois
já é tarde, `HEAD` vira igual ao remoto. O corpo inteiro fica dentro de
`main()`, lido para memória antes de rodar: protege contra o próprio `git
merge` reescrever o arquivo enquanto ele está em execução.

Variações por layout:
- **Checkout direto** (sem `current/`): `ENV_FILE=$APP_DIR/.env`.
- **Site estático**: script mínimo, só `git fetch "$FETCH_URL" main &&
  git merge --ff-only "$sha"` dentro do mesmo `main()`/`flock`.
- Apps sem `manage.py check`/`migrate --check` de sentido (nenhum Django
  neste ecossistema hoje) ajustam essa etapa central; o resto do esqueleto
  vale igual.

### 7.5 O chamador (`deploy.yml`) e o teste antes de confiar nele

```yaml
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
    uses: rigst/ci/.github/workflows/deploy-django.yml@<SHA fixo>  # v1
    secrets:
      CD_SSH_KEY: ${{ secrets.CD_SSH_KEY }}
    with:
      ssh-host: "app.stolben.com"
```

Fixe por SHA, não `@v1` solto — este workflow tem acesso a credenciais de
produção. O nome em `workflows:` é o `name:` do workflow de CI **daquele**
projeto — a maioria chama `CI`, mas confira (`dojo`, por exemplo, chama
`testes`).

Antes de depender do gatilho automático, teste de ponta a ponta com
`workflow_dispatch`:

```bash
# temporariamente, troque o "on:" do deploy.yml por:
#   on: workflow_dispatch
gh workflow run deploy.yml --repo rigst/PROJETO
gh run watch --repo rigst/PROJETO
```

Corrija o que aparecer, só então troque para `workflow_run`.

**Bootstrap**: o primeiro merge do `deploy.yml` com `workflow_run` em `main`
não dispara nada sozinho — o evento só passa a existir a partir do *próximo*
push em `main`, depois que o próprio `deploy.yml` já estiver lá. Um commit
trivial subsequente confirma o disparo automático de verdade.

### 7.6 Verificação pós-adoção

```bash
systemctl show -p MainPID --value UNIDADE   # deve ser o mesmo de antes do deploy (reload, não restart)
git -C /var/www/PROJETO/current log -1      # SHA bate com o que passou no CI
curl -s -o /dev/null -w '%{http_code}\n' https://app.stolben.com
sudo -n -l -U deploy                        # só as linhas de reload/restart deste app
sudo -n -l -U rod                           # idêntico a antes — nada mudou pra rod
```

### 7.7 Armadilhas encontradas no piloto (sistema_arq)

Nenhuma delas apareceu no design — só rodando de verdade. Todas resolvidas
uma vez, para os 9 apps, no piloto original; um app novo pode esbarrar nas
mesmas se pular alguma etapa.

**Chave privada recusada com "error in libcrypto"**, mesmo sendo a chave
certa (funciona perfeitamente fora do Actions). Causa: a expressão `${{
secrets.X }}` do Actions corta a quebra de linha final ao interpolar um
secret multilinha, e uma chave OpenSSH sem `\n` depois de `-----END OPENSSH
PRIVATE KEY-----` é recusada pelo libcrypto com um erro que não aponta pra
causa real. Correção: grave com `printf '%s\n'`, nunca `printf '%s'` — seguro
mesmo que o secret já traga a quebra de linha (o parser tolera uma linha em
branco a mais no fim, não tolera nenhuma faltando).

**`fatal: detected dubious ownership in repository`**: git recusa operar num
repo cujo dono (`rod`) difere de quem roda o comando (`deploy`). Precisa da
exceção por caminho, rodada como `deploy` (`git config --global --add
safe.directory CAMINHO`) — não existe atalho por wildcard nem por dono.

**`error: cannot open '.git/FETCH_HEAD': Permission denied`** (e depois, o
mesmo padrão em arquivos da árvore de trabalho e de `staticfiles/`): o
diretório `.git` — e, historicamente, partes do checkout e do
`staticfiles/` — foi populado sem o bit setgid, então arquivos que `git`
recria a cada fetch (ou que `collectstatic` reescreve) saem com o grupo
primário de quem rodou o comando naquele dia (`rod`, grupo `rod`), não
`www-data`. `deploy`, membro de `www-data` mas não de `rod`, não consegue
escrever neles. Correção, por app, rodada como `rod` (que já é dono de tudo
isso e membro de `www-data`, então não precisa de sudo):

```bash
for dir in .git . shared/staticfiles; do   # ajuste os caminhos pro layout do app
  cd "/var/www/PROJETO/current/$dir" 2>/dev/null || continue
  chgrp -R www-data .
  find . -type d -exec chmod g+ws {} +   # setgid: todo arquivo NOVO já nasce com o grupo certo
  find . -type f -exec chmod g+w {} +
done
```

Rode isso **antes** do primeiro dry-run de cada app novo — pula o ciclo
inteiro de "falha, descobre o caminho que faltou, corrige, tenta de novo".
Exclua `venv/` (tem dono e ciclo de vida próprios) e qualquer `.env` (ver
próximo item — write não é o que falta ali).

**`.env` ilegível pelo `deploy`** (ou, num caso, legível por *qualquer*
usuário do sistema): o padrão correto é `640 rod:www-data` — `deploy` só
precisa *ler* (o script faz `source`, nunca escreve), e nem todo app tinha
isso; alguns estavam `600 rod:rod` (nem `deploy` lê) e um estava `664
rod:rod` (mundo-legível, sem querer, porque `rod` não é o dono do grupo
`www-data`). Corrigido nos 9 de uma vez:

```bash
chgrp www-data /var/www/PROJETO/.env-ou-shared/.env
chmod 640 /var/www/PROJETO/.env-ou-shared/.env
```

**Timeout intermitente de SSH do runner pro servidor** (`Connection timed
out`, ~2 minutos), sem fail2ban nem firewall envolvidos — confirmado com
`fail2ban-client status sshd` (zero banidos) e `ufw status` (porta 22 liberada
geral). É a rota de rede entre o pool de runners hospedados e este VPS
engasgando de vez em quando, não um bloqueio ativo. `deploy-django.yml` e
`deploy-static.yml` já cobrem isso com até 3 tentativas e `ConnectTimeout=15`
— mas o retry olha o **stderr do cliente ssh**, nunca só o código de saída:
um `cd-deploy.sh` que roda e falha de verdade (migration ruim, smoke-test
vermelho) também sai com código diferente de zero, e repetir *isso* seria
pior, não melhor (poderia colidir com o `flock` do próprio deploy anterior,
por exemplo). Nada a fazer num app novo — já vem pronto no workflow
reutilizável.

**O retry de conexão (7.5) não retentava de verdade** — sintoma: falha em
~15s (o `ConnectTimeout`), `exit 255`, **zero** saída no log, nenhuma
tentativa 2 ou 3. Causa: `deploy-django.yml`/`deploy-static.yml` viviam sob
`bash -e` (o padrão do `run:` do Actions), e `saida="$(ssh ...)"; codigo=$?`
solto **dispara o errexit na própria atribuição** quando o `ssh` falha — o
script morre ali, antes de `codigo=$?` sequer rodar. A correção (já aplicada
no workflow reutilizável, nada a fazer num app novo) é testar a atribuição
como condição de um `if`, a exceção documentada do `-e`:

```bash
if saida="$(ssh ... 2>&1)"; then codigo=0; else codigo=$?; fi
```

**`gitleaks` reprovou o CI com "generic-api-key" em `deploy/cd-deploy.sh`**:
o token real de `X-Healthz-Token` (copiado do `.env` pra testar o
smoke-test) foi hardcoded direto no `HEALTH_HEADER`, um arquivo git-tracked
— achado correto do scanner, não falso positivo. O esqueleto em 7.4 já
resolve isso lendo `DJANGO_HEALTHZ_TOKEN` do próprio `.env` depois do
`source`, nunca escrevendo o valor no script. Vale mesmo quando o token
ainda é o placeholder do `.env.example` (não é segredo de verdade, mas
hardcoded do mesmo jeito ensina o hábito errado).

**Smoke-test reprova um deploy bom** (502 na hora, 200 dois segundos depois):
o `curl` do healthcheck rodava uma vez só, logo após o `systemctl restart` —
tempo insuficiente pro gunicorn terminar de subir os workers. O esqueleto em
7.4 já tenta até 5 vezes com 2s de intervalo antes de desistir. A causa raiz
foi resolvida depois, na raiz: `reload` (7.1.1) em vez de `restart` no
serviço web elimina a corrida por completo, já que o socket nunca cai — o
retry no smoke-test virou rede de segurança, não a correção principal.

**Lock file em `/tmp` com dono errado, depois de testar o script à mão como
`rod` antes do primeiro dry-run real**: `/tmp` é world-writable (sticky bit),
mas uma vez que `/tmp/PROJETO_cd_deploy.lock` existe com um dono, só esse
dono consegue abri-lo pra escrita — e é exatamente isso que o `flock` faz
(`9>"$LOCK_FILE"`). Testar o script localmente como `rod` antes de o
`deploy` nunca ter rodado cria o arquivo como `rod:rod`; a primeira execução
de verdade via SSH (como `deploy`) falha com `Permission denied` no próprio
`open` do descritor 9, **sem nenhuma mensagem no stdout/stderr** — o jeito
de perceber é rodar `ssh -i chave deploy@host "deploy SHA"` à mão e ler o
erro, já que o log do Actions mostra só "exit code 1" sem mais nada.
Correção: `rm -f /tmp/PROJETO_cd_deploy.lock` depois de qualquer teste local
feito como `rod`, antes do primeiro dry-run real do `deploy.yml`.

**`backup_postgres.sh` pré-existente com o mesmo problema de grupo do item
anterior**: 3 dos 4 apps que já tinham esse script antes do CD (`sistema_
financas`, `sistema_orcamentos`, `sistema_vetorial` — só `sistema_arq` já
estava certo) tinham o próprio script, e o diretório de backups/logs, com
grupo `rod` em vez de `www-data`. `deploy` não conseguia nem executar o
script. Mesma correção do item anterior, aplicada a
`shared/scripts/backup_postgres.sh`, `shared/backups/` e `shared/logs/`.
