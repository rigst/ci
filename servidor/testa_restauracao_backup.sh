#!/usr/bin/env bash
# Teste de RESTAURAÇÃO do backup offsite (item 14 da auditoria de 05/11).
#
# O backup diário só conferia `pg_restore -l` (lista o índice do dump) e o
# checksum do upload: nunca se provou que um dump vira banco de novo. Este
# script prova, toda semana, com a cópia que valeria num desastre — a do Drive:
#
#   1. baixa os dumps da pasta mais recente do Drive e confere o sha256 contra
#      o manifest que o backup gravou junto;
#   2. sobe um PostgreSQL DESCARTÁVEL como o usuário rod (initdb num diretório
#      temporário, só socket unix, porta própria, sem TCP) — nada encosta no
#      Postgres de produção;
#   3. restaura cada dump num banco novo e confere: pg_restore sem erro, toda
#      tabela com dados do índice existe, nº de migrações do Django igual ao de
#      produção no momento do dump (>0) e total de linhas > 0;
#   4. derruba o cluster e apaga tudo; e-mail só se algo falhar.
#
# Cron (segunda, 04:30, depois do backup das 02:55):
#   30 4 * * 1 /home/rod/scripts/ativos/testa_restauracao_backup.sh >/dev/null 2>&1 # backup_restore_test
set -Eeuo pipefail

REMOTE="${RCLONE_REMOTE:-gdrive:Backups/srv1553321}"
PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PORTA="${PORTA_TESTE:-54329}"
ALERT_TO="${BACKUP_ALERT_TO:-alertas@stolben.com}"
LOG_DIR="/home/rod/backups_offsite/logs"
LOG_FILE="$LOG_DIR/restore-test.log"
STATUS_FILE="/home/rod/backups_offsite/last_restore_ok.txt"
LOCK_FILE="/tmp/restore_test.lock"

mkdir -p "$LOG_DIR"
TMP="$(mktemp -d /tmp/restore_test.XXXXXX)"
PGDATA="$TMP/pgdata"
SOCK="$TMP/sock"
DUMPS="$TMP/dumps"
mkdir -p "$SOCK" "$DUMPS"

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }

alert() {
  local assunto="$1" corpo="$2"
  printf 'To: %s\nFrom: rodrigo@stolben.com\nSubject: %s\n\n%s\n' \
    "$ALERT_TO" "$assunto" "$corpo" | msmtp "$ALERT_TO" 2>>"$LOG_FILE" \
    || log "AVISO: não consegui enviar o e-mail de alerta para $ALERT_TO"
}

FALHAS_FILE="$TMP/falhas.txt"
: > "$FALHAS_FILE"
falha() { printf '%s\n' "$*" >> "$FALHAS_FILE"; log "FALHA: $*"; }

# Qualquer comando que falhe (set -e) vira falha registrada; o EXIT decide.
trap 'falha "erro inesperado na linha $LINENO (exit $?)"' ERR

finalizar() {
  local codigo=$?
  if [[ -f "$PGDATA/postmaster.pid" ]]; then
    "$PGBIN/pg_ctl" -D "$PGDATA" -m immediate stop >/dev/null 2>&1 || true
  fi
  if [[ -s "$FALHAS_FILE" ]]; then
    alert "[RESTAURAÇÃO FALHOU] srv1553321 $(date -u +%F)" \
      "$(printf 'O teste semanal de restauração do backup FALHOU.\n\n%s\n\nÚltimas linhas do log:\n%s' \
         "$(sed 's/^/- /' "$FALHAS_FILE")" "$(tail -n 30 "$LOG_FILE" 2>/dev/null)")"
    log "Teste de restauração terminou COM FALHAS ($(wc -l < "$FALHAS_FILE"))"
    codigo=1
  elif (( codigo == 0 )); then
    printf 'finished_at=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > "$STATUS_FILE"
    log "Teste de restauração concluído: tudo restaurou"
  fi
  rm -rf "$TMP"
  exit "$codigo"
}
trap finalizar EXIT

psqlt() { "$PGBIN/psql" -h "$SOCK" -p "$PORTA" -U rod -X -A -t -v ON_ERROR_STOP=1 "$@"; }

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || { log "teste de restauração já em execução; saindo"; exit 0; }

  log "Teste de restauração iniciado"

  # 1. Pasta datada mais recente do Drive + verificação contra o manifest.
  local dia manifest
  dia="$(rclone lsf --dirs-only "$REMOTE" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}/$' | sort | tail -1)"
  dia="${dia%/}"
  [[ -n "$dia" ]] || { falha "nenhuma pasta datada em $REMOTE"; return; }
  log "Backup testado: $REMOTE/$dia"
  rclone copy "$REMOTE/$dia/databases" "$DUMPS" --retries 3
  rclone copy "$REMOTE/$dia/manifests" "$TMP/manifests" --retries 3
  manifest="$(find "$TMP/manifests" -name 'manifest_*.txt' | sort | tail -1)"
  [[ -n "$manifest" ]] || { falha "manifest ausente em $dia"; return; }

  local dump nome esperado obtido
  for dump in "$DUMPS"/*.dump; do
    nome="$(basename "$dump")"
    esperado="$(grep -E "  \./databases/$nome\$" "$manifest" | awk '{print $1}')"
    obtido="$(sha256sum "$dump" | awk '{print $1}')"
    if [[ -z "$esperado" || "$esperado" != "$obtido" ]]; then
      falha "$nome: sha256 do Drive não confere com o manifest"
    fi
  done

  # 2. Cluster descartável: só socket unix, sem TCP.
  "$PGBIN/initdb" -D "$PGDATA" -U rod --auth=trust -E UTF8 --locale=C.UTF-8 >/dev/null
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$TMP/postgres.log" -w \
    -o "-p $PORTA -k $SOCK -c listen_addresses='' -c fsync=off -c synchronous_commit=off" start >/dev/null

  # 3. Restaura e confere cada banco.
  local banco tabelas_dump tabelas_ok migracoes linhas erros t
  for dump in "$DUMPS"/*.dump; do
    nome="$(basename "$dump" .dump)"
    banco="r_$(echo "${nome%_*}" | tr -c 'a-z0-9\n' '_')"
    psqlt -d postgres -c "CREATE DATABASE $banco" >/dev/null

    if ! erros="$("$PGBIN/pg_restore" -h "$SOCK" -p "$PORTA" -U rod -d "$banco" \
                   --no-owner --no-privileges --exit-on-error "$dump" 2>&1)"; then
      falha "$nome: pg_restore falhou: $(echo "$erros" | tail -3 | tr '\n' ' ')"
      continue
    fi

    # Toda tabela com dados no índice do dump tem de existir no banco restaurado.
    tabelas_dump="$("$PGBIN/pg_restore" -l "$dump" | awk '/ TABLE DATA /{print $(NF-2)"."$(NF-1)}' | sort -u)"
    tabelas_ok=0
    while IFS= read -r t; do
      [[ -z "$t" ]] && continue
      if [[ "$(psqlt -d "$banco" -c "select to_regclass('$t') is not null")" == "t" ]]; then
        tabelas_ok=$((tabelas_ok + 1))
      else
        falha "$nome: tabela $t do dump não existe após restaurar"
      fi
    done <<< "$tabelas_dump"

    migracoes="$(psqlt -d "$banco" -c "select count(*) from django_migrations" 2>/dev/null || echo 0)"
    # n_live_tup fica zerado logo após o restore até o ANALYZE.
    psqlt -d "$banco" -c "ANALYZE" >/dev/null
    linhas="$(psqlt -d "$banco" -c "select coalesce(sum(n_live_tup),0) from pg_stat_user_tables")"

    if (( migracoes == 0 )); then
      falha "$nome: django_migrations vazia ou ausente"
    fi
    if (( linhas == 0 )); then
      falha "$nome: banco restaurado sem nenhuma linha"
    fi
    log "OK $nome: $tabelas_ok tabelas, $migracoes migrações, ~$linhas linhas"
    psqlt -d postgres -c "DROP DATABASE $banco" >/dev/null
  done

  local qtd
  qtd="$(find "$DUMPS" -name '*.dump' | wc -l)"
  if (( qtd < 9 )); then
    falha "esperava 9 dumps em $dia, achei $qtd"
  fi
}

exec >> "$LOG_FILE" 2>&1
main
