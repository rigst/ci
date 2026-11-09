#!/usr/bin/env bash
# Backup diário do Postgres de um app. Cópia única, servida a todos os apps do
# layout current/shared/venv: nada aqui é específico de um projeto.
#
# APP_ROOT vem do lugar do próprio script (shared/scripts/ -> raiz do app), e as
# credenciais saem do shared/.env: DATABASE_URL quando existe, senão as chaves
# DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT. Antes havia uma cópia por app,
# divergindo em acentos, nome de lock e forma de ler o .env — e uma correção
# precisava ser repetida cinco vezes.
#
# Instalação em um app:
#   install -m 750 -o rod -g www-data servidor/backup_postgres.sh \
#     /var/www/PROJETO/shared/scripts/backup_postgres.sh
set -Eeuo pipefail

APP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$(basename "$APP_ROOT")"
ENV_FILE="$APP_ROOT/shared/.env"
BACKUP_DIR="$APP_ROOT/shared/backups/postgres"
LOG_DIR="$APP_ROOT/shared/logs"
LOG_FILE="$LOG_DIR/postgres-backup.log"
# O lock morava em /tmp. O cron diário roda como rod e cria o arquivo rod:rod
# 0664; o backup pré-migração do CD roda como deploy (grupo www-data) e esbarra
# no sticky bit do /tmp — "Permission denied" na linha do flock. Como o bloco
# inteiro redireciona para o log, o erro não chega ao CD: o deploy morre com
# exit 1 e nenhuma mensagem (aconteceu no sistema_questoes em 2026-09-10). Em
# shared/locks/ o diretório é setgid www-data e group-writable, então os dois
# usuários abrem o mesmo lock.
LOCK_FILE="$APP_ROOT/shared/locks/${APP}_postgres_backup.lock"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

# Quem chama pelo CD é o "deploy" (cd-deploy.sh roda este script direto, sem
# sudo). O dump sai `chmod 600` do dono, então um dump criado pelo deploy é
# ilegível para o rod — e é o rod que roda o backup offsite, cujo
# `copy_latest_dump` faz `cp -p` sob `set -e`. Um único deploy com migração
# derrubaria o offsite dos nove projetos na noite seguinte.
#
# O offsite procura com `find -maxdepth 1`, então mandar o dump pré-migração
# para um subdiretório resolve: ele continua existindo para rollback, e o
# offsite segue enxergando só os dumps diários do rod.
if [[ "$(id -un)" != "rod" ]]; then
  BACKUP_DIR="$BACKUP_DIR/predeploy"
fi

mkdir -p "$BACKUP_DIR" "$LOG_DIR" "$(dirname "$LOCK_FILE")"

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
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] backup start"

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
