#!/usr/bin/env bash
set -euo pipefail

# Esqueleto do deploy/cd-deploy.sh de um app Django (RUNBOOK.md, seção 7.4).
# As variáveis do topo são o que muda de um app para outro.
#
# Disparado via SSH pelo usuário "deploy" (authorized_keys com command=
# forçado — RUNBOOK seção 7.3). Roda inteiro como "deploy"; só o reload e os
# restarts precisam de sudo (sudoers próprio de "deploy", seção 7.2 — o
# restart da unidade web já está lá para isto).

APP_DIR=/var/www/PROJETO/current
FETCH_URL=https://github.com/rigst/PROJETO.git
VENV=/var/www/PROJETO/venv
ENV_FILE=/var/www/PROJETO/shared/.env   # vazio se o settings carrega o .env sozinho (seção 7.4)
WEB_SERVICE=PROJETO.service             # reload (SIGHUP) — precisa de ExecReload, seção 7.1.1
OTHER_SERVICES=()                       # celery etc., restart
# /healthz/ que toca banco, cache e broker e responde 200 ou 503 — RUNBOOK
# seção 7.8. A home NÃO serve: ela redireciona para o login (302) mesmo com o
# banco fora, e o smoke-test passava assim.
HEALTH_URL="https://PROJETO.stolben.com/healthz/"
HEALTH_HEADER=""                        # NUNCA token literal aqui — montado depois do .env
BACKUP_SCRIPT=""
EXTRA_ENV=""
LOCK_FILE=/tmp/PROJETO_cd_deploy.lock

versao_instalada() {
  "$VENV/bin/python" -c "import importlib.metadata as m; print(m.version('$1'))" 2>/dev/null \
    || echo ausente
}

gunicorn_mais_novo_que_o_mestre() {
  local pkg inicio t_pkg t_svc
  pkg="$(ls -d "$VENV"/lib/python*/site-packages/gunicorn 2>/dev/null | head -1)"
  [[ -d "$pkg" ]] || return 1
  inicio="$(systemctl show -p ActiveEnterTimestamp --value "$WEB_SERVICE")"
  [[ -n "$inicio" ]] || return 1
  t_pkg="$(stat -c %Y "$pkg")" || return 1
  t_svc="$(date -d "$inicio" +%s 2>/dev/null)" || return 1
  (( t_pkg > t_svc ))
}

carregar_env() {
  set -a
  # shellcheck disable=SC1090
  [[ -n "$ENV_FILE" ]] && source "$ENV_FILE"
  [[ -n "$EXTRA_ENV" ]] && eval "export $EXTRA_ENV"
  set +a
  [[ -n "${DJANGO_HEALTHZ_TOKEN:-}" ]] && HEALTH_HEADER="X-Healthz-Token: $DJANGO_HEALTHZ_TOKEN"
  return 0
}

# Só 200 conta. 3xx não: um /healthz/ que redireciona é rota errada ou
# middleware de login na frente, e é exatamente o que o smoke-test precisa
# pegar. Seis tentativas, 3 s entre elas: o reload troca os workers aos poucos.
saude_ok() {
  [[ -n "$HEALTH_URL" ]] || return 0
  local codigo=""
  for _ in 1 2 3 4 5 6; do
    codigo="$(curl -s -o /dev/null --max-time 10 -w '%{http_code}' \
      ${HEALTH_HEADER:+-H "$HEALTH_HEADER"} "$HEALTH_URL" || true)"
    [[ "$codigo" == "200" ]] && return 0
    sleep 3
  done
  echo "healthz: $HEALTH_URL respondeu '$codigo'"
  return 1
}

reiniciar_servicos() {
  local forcar_restart="$1"
  if [[ "$forcar_restart" == "sim" ]]; then
    sudo systemctl restart "$WEB_SERVICE"
  else
    sudo systemctl reload "$WEB_SERVICE"
  fi
  for unidade in "${OTHER_SERVICES[@]}"; do
    sudo systemctl restart "$unidade"
  done
}

# Volta o CÓDIGO para o commit anterior e confere a saúde de novo. Só é
# chamado quando o deploy não trouxe migração: com migração aplicada, o
# código antigo rodaria contra o schema novo, e o caminho certo é restaurar o
# banco do backup pré-deploy (RUNBOOK seção 3.4) — decisão de gente.
rollback() {
  local antes="$1" sha="$2"
  echo "::: Rollback automático: voltando de $sha para $antes"
  git reset --hard "$antes"
  "$VENV/bin/pip" install --require-hashes -r requirements.lock
  "$VENV/bin/python" manage.py collectstatic --noinput
  # restart, não reload: o pip acima pode ter trocado pacotes que o mestre
  # do gunicorn carregou no boot.
  reiniciar_servicos sim
  if saude_ok; then
    echo "Rollback concluído: $antes está no ar. O deploy de $sha FALHOU."
  else
    echo "Rollback feito, mas o healthz continua falhando: intervenção manual."
  fi
}

main() {
  local sha
  sha="$(printf '%s' "${SSH_ORIGINAL_COMMAND:-}" | awk '{print $2}')"
  [[ "$sha" =~ ^[0-9a-f]{7,40}$ ]] || { echo "SHA inválido: '$sha'"; exit 1; }

  cd "$APP_DIR"
  git fetch "$FETCH_URL" main
  git merge-base --is-ancestor "$sha" FETCH_HEAD \
    || { echo "SHA não é ancestral do main remoto: $sha"; exit 1; }

  local antes; antes="$(git rev-parse HEAD)"
  local tem_migracao
  tem_migracao="$(git diff --name-only "HEAD..$sha" -- '*/migrations/*')"

  if [[ -n "$tem_migracao" && -n "$BACKUP_SCRIPT" ]]; then
    "$BACKUP_SCRIPT"
  fi

  git merge --ff-only "$sha"

  local gunicorn_antes gunicorn_depois
  gunicorn_antes="$(versao_instalada gunicorn)"
  "$VENV/bin/pip" install --require-hashes -r requirements.lock
  gunicorn_depois="$(versao_instalada gunicorn)"

  carregar_env

  "$VENV/bin/python" manage.py check --deploy --fail-level ERROR
  "$VENV/bin/python" manage.py migrate --check || "$VENV/bin/python" manage.py migrate
  "$VENV/bin/python" manage.py collectstatic --noinput

  if [[ "$gunicorn_antes" != "$gunicorn_depois" ]] || gunicorn_mais_novo_que_o_mestre; then
    reiniciar_servicos sim
  else
    reiniciar_servicos nao
  fi

  if ! saude_ok; then
    if [[ -n "$tem_migracao" ]]; then
      echo "Smoke-test falhou e o deploy aplicou migração: SEM rollback automático."
      echo "Código: git -C $APP_DIR reset --hard $antes. Banco: backup pré-deploy (RUNBOOK 3.4)."
    else
      rollback "$antes" "$sha"
    fi
    exit 1
  fi

  echo "Deploy de $sha concluído (era $antes)."
}

(
  flock -n 9 || { echo "Deploy já em andamento, saindo."; exit 1; }
  main "$@"
) 9>"$LOCK_FILE"; exit $?
# O `exit` na MESMA linha é proposital: o bash lê o script aos poucos, e o
# merge/rollback acima reescreve este arquivo no disco. Sem ele, o bash
# continuaria lendo do offset antigo no arquivo novo.
