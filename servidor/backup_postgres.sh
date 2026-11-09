#!/usr/bin/env bash
# Backup diário do Postgres de um app. Cópia ÚNICA, servida a todos os nove
# apps: nada aqui é específico de um projeto.
#
# Os apps têm dois layouts, e o script descobre em qual está pelo diretório onde
# ele próprio mora:
#
#   shared  ->  .../PROJETO/shared/scripts/backup_postgres.sh
#               dumps em PROJETO/shared/backups/postgres, logs em shared/logs
#   flat    ->  .../PROJETO/deploy/backup_postgres.sh
#               checkout direto, sem shared/. Os dumps ficam FORA do working
#               tree, em /home/rod/backups/PROJETO, para não interagir com
#               `git clean`/`git status` do deploy.
#
# As credenciais saem do .env do app: DATABASE_URL quando existe, senão as
# chaves DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT.
#
# Antes havia nove cópias deste arquivo, divergindo em acentos, nome de lock,
# forma de ler o .env e presença do `-maxdepth 1` na retenção — uma correção
# precisava ser repetida nove vezes, e a de 09/11/2026 (ver RUNBOOK 6.1) foi
# repetida em cinco.
#
# Instalação (o caminho importa: o sudoers do `deploy` e o crontab do rod
# apontam para ele literalmente):
#   layout shared:
#     install -m 750 -o rod -g www-data servidor/backup_postgres.sh \
#       /var/www/PROJETO/shared/scripts/backup_postgres.sh
#   layout flat: o arquivo é versionado em deploy/ no repo do próprio app,
#     porque o CD sobrescreve o working tree a cada deploy.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "$(basename "$SCRIPT_DIR")" in
  scripts)
    # .../PROJETO/shared/scripts -> PROJETO
    LAYOUT=shared
    APP_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
    ;;
  deploy)
    # .../PROJETO/deploy -> PROJETO
    LAYOUT=flat
    APP_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
    ;;
  *)
    echo "ERRO: layout não reconhecido; o script tem de estar em shared/scripts/ ou deploy/, e está em $SCRIPT_DIR" >&2
    exit 1
    ;;
esac

APP="$(basename "$APP_ROOT")"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
# Só para os testes: no servidor os dumps do layout flat vivem em
# /home/rod/backups, que é 0750 do rod — de onde o rclone os manda ao Drive.
DUMPS_BASE="${BACKUP_DUMPS_BASE:-/home/rod/backups}"

if [[ "$LAYOUT" == shared ]]; then
  ENV_FILE="$APP_ROOT/shared/.env"
  BACKUP_DIR="$APP_ROOT/shared/backups/postgres"
  LOG_DIR="$APP_ROOT/shared/logs"
  # O lock morava em /tmp. O cron diário roda como rod e cria o arquivo rod:rod
  # 0664; o backup pré-migração do CD roda como deploy (grupo www-data) e
  # esbarra no sticky bit do /tmp — "Permission denied" na linha do flock. Como
  # o bloco inteiro redireciona para o log, o erro não chega ao CD: o deploy
  # morre com exit 1 e nenhuma mensagem (aconteceu no sistema_questoes em
  # 2026-09-10). Em shared/locks/ o diretório é setgid www-data e
  # group-writable, então os dois usuários abrem o mesmo lock.
  LOCK_FILE="$APP_ROOT/shared/locks/${APP}_postgres_backup.lock"
else
  ENV_FILE="$APP_ROOT/.env"
  BACKUP_DIR="$DUMPS_BASE/$APP/postgres"
  LOG_DIR="$DUMPS_BASE/$APP/logs"
  # Aqui o CD chama o script com `sudo -n -u rod`, então cron e deploy são o
  # MESMO usuário e o sticky bit do /tmp não morde. Mesmo assim o lock mora
  # junto dos dumps, em 0750 do rod: um diretório por app, sem depender de
  # quem mais escreve em /tmp.
  LOCK_FILE="$DUMPS_BASE/$APP/${APP}_postgres_backup.lock"
fi

# Quem chama pelo CD no layout shared é o "deploy" (cd-deploy.sh roda este
# script direto, sem sudo). O dump sai `chmod 600` do dono, então um dump criado
# pelo deploy é ilegível para o rod — e é o rod que roda o backup offsite, cujo
# `copy_latest_dump` faz `cp -p`. Mandar o dump pré-migração para um
# subdiretório resolve: ele continua existindo para rollback, e o offsite (que
# procura com `find -maxdepth 1`) segue enxergando só os dumps diários do rod.
#
# No layout flat não existe esse problema: o CD usa `sudo -n -u rod`, porque os
# dumps vivem em /home/rod/backups e o deploy não escreve lá.
if [[ "$LAYOUT" == shared && "$(id -un)" != "rod" ]]; then
  BACKUP_DIR="$BACKUP_DIR/predeploy"
fi

mkdir -p "$BACKUP_DIR" "$LOG_DIR" "$(dirname "$LOCK_FILE")"
LOG_FILE="$LOG_DIR/postgres-backup.log"

# Lê uma chave do .env sem executá-lo: sourcear quebra em senha com caractere
# especial de shell (aconteceu com divisor_pdf, que tem vírgula e ponto na
# senha). Pega a última ocorrência, tira aspas e CR.
read_env_var() {
  local key="$1" file="$2" val
  val="$(grep -E "^${key}=" "$file" | tail -n1 || true)"
  val="${val#"${key}="}"
  val="${val%$'\r'}"
  val="${val%\"}"; val="${val#\"}"
  val="${val%\'}"; val="${val#\'}"
  printf '%s' "$val"
}

main() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] backup start ($APP, layout $LAYOUT)"

  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERRO: arquivo de ambiente não encontrado: $ENV_FILE"
    exit 1
  fi

  local database_url db_name db_user db_pass db_host db_port
  database_url="$(read_env_var DATABASE_URL "$ENV_FILE")"

  if [[ -n "$database_url" ]]; then
    db_name="$(printf '%s' "$database_url" | sed -E 's#^[^:]+://[^/]+/([^?]+).*$#\1#')"
    [[ -z "$db_name" || "$db_name" == "$database_url" ]] && db_name="database"
  else
    db_name="$(read_env_var DB_NAME "$ENV_FILE")"
    db_user="$(read_env_var DB_USER "$ENV_FILE")"
    db_pass="$(read_env_var DB_PASSWORD "$ENV_FILE")"
    db_host="$(read_env_var DB_HOST "$ENV_FILE")"; db_host="${db_host:-localhost}"
    db_port="$(read_env_var DB_PORT "$ENV_FILE")"; db_port="${db_port:-5432}"

    if [[ -z "$db_name" || -z "$db_user" ]]; then
      echo "ERRO: nem DATABASE_URL nem DB_NAME/DB_USER definidos em $ENV_FILE"
      exit 1
    fi
  fi

  local timestamp outfile tmpfile latest_size
  timestamp="$(date -u +'%Y%m%dT%H%M%SZ')"
  outfile="$BACKUP_DIR/${db_name}_${timestamp}.dump"
  tmpfile="$outfile.tmp"

  flock -n 9 || { echo "Backup já em execução, saindo"; exit 0; }

  if [[ -n "$database_url" ]]; then
    pg_dump --format=custom --no-owner --no-privileges \
      --file "$tmpfile" "$database_url"
  else
    PGPASSWORD="$db_pass" pg_dump --format=custom --no-owner --no-privileges \
      -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" \
      --file "$tmpfile"
  fi

  pg_restore -l "$tmpfile" >/dev/null

  mv "$tmpfile" "$outfile"
  chmod 600 "$outfile"

  # -maxdepth 1 é obrigatório: sem ele o find desce em predeploy/, que é do
  # usuário `deploy` e não dá write ao grupo. Quando um dump pré-migração
  # completa a retenção, o -delete toma "Permission denied", o find sai != 0 e,
  # por set -e, este script sai 1 — derrubando o backup OFFSITE inteiro, que
  # chama este script debaixo do próprio set -e (aconteceu em 07-09/11/2026:
  # três noites sem cópia no Drive). A retenção do predeploy/ é do CD, que roda
  # como `deploy` e tem permissão lá.
  find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' -mtime +"$RETENTION_DAYS" -delete

  latest_size="$(du -h "$outfile" | awk '{print $1}')"
  echo "Backup concluído: $outfile ($latest_size)"
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] backup end"
}

main "$@" >> "$LOG_FILE" 2>&1 9> "$LOCK_FILE"
