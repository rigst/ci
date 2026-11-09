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

# Marca que o teste realmente rodou. Sem isto, uma saída antecipada com código
# 0 (lock já tomado) caía no ramo de sucesso do `finalizar` e gravava a
# sentinela SEM ter restaurado nada — o `verifica_backup_recente.sh` passaria a
# ver uma prova de restauração que nunca aconteceu.
RESTAUROU=0

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
  elif (( codigo == 0 && RESTAUROU == 1 )); then
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
  #
  # A pasta do dia pode conter MAIS DE UM run: o backup foi re-executado à mão
  # (foi o que aconteceu em 09/11/2026, duas vezes no mesmo dia ao fechar o
  # incidente das três noites sem backup). Cada run grava seu próprio manifest
  # e seu próprio conjunto de dumps.
  #
  # Testar "todos os dumps da pasta" contra "o manifest mais novo" é errado por
  # dois motivos: os dumps do run antigo não constam nesse manifest, e o
  # conjunto deixa de ser coerente (bancos de momentos diferentes).
  #
  # Qual dump é de qual run NÃO se descobre pelo nome: o timestamp no nome é o
  # do `pg_dump` daquele banco, não o RUN_ID (num mesmo run eles variam alguns
  # segundos — 124205Z a 124208Z no run acima). A fonte de verdade é o próprio
  # manifest, que lista os arquivos do run; é dele que sai a lista a baixar.
  local dia manifest run_id
  # `|| true` segura o pipefail: remote sem nenhuma pasta de data faz o grep
  # sair 1 e, sob `set -E`, o trap ERR dispararia aqui — tornando a checagem
  # explicativa da linha seguinte inalcançável e trocando a mensagem por um
  # "erro inesperado na linha N".
  dia="$(rclone lsf --dirs-only "$REMOTE" | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}/$' | sort | tail -1 || true)"
  dia="${dia%/}"
  [[ -n "$dia" ]] || { falha "nenhuma pasta datada em $REMOTE"; return; }
  rclone copy "$REMOTE/$dia/manifests" "$TMP/manifests" --retries 3
  manifest="$(find "$TMP/manifests" -name 'manifest_*.txt' | sort | tail -1)"
  [[ -n "$manifest" ]] || { falha "manifest ausente em $dia"; return; }

  run_id="$(basename "$manifest" .txt)"
  run_id="${run_id#manifest_}"
  log "Backup testado: $REMOTE/$dia (run $run_id)"

  # Nomes dos dumps deste run, a partir da seção [checksums] do manifest.
  local lista="$TMP/dumps-do-run.txt"
  sed -n 's#^[0-9a-f]\{64\}  \./databases/\(.*\.dump\)$#\1#p' "$manifest" | sort -u > "$lista"
  if [[ ! -s "$lista" ]]; then
    falha "manifest do run $run_id não lista nenhum dump"
    return
  fi
  rclone copy "$REMOTE/$dia/databases" "$DUMPS" --files-from "$lista" --retries 3

  shopt -s nullglob
  local dumps=("$DUMPS"/*.dump)
  shopt -u nullglob
  if (( ${#dumps[@]} != $(wc -l < "$lista") )); then
    falha "baixei ${#dumps[@]} dos $(wc -l < "$lista") dumps do run $run_id"
    return
  fi
  log "Dumps do run: ${#dumps[@]}"

  local dump nome esperado obtido
  for dump in "${dumps[@]}"; do
    nome="$(basename "$dump")"
    # `|| true` segura o pipefail: sem match, o grep sai 1 e o trap ERR
    # transformaria isto num "erro inesperado na linha N" opaco em vez da
    # falha explicativa logo abaixo.
    esperado="$(grep -E "  \./databases/$nome\$" "$manifest" | awk '{print $1}' || true)"
    obtido="$(sha256sum "$dump" | awk '{print $1}')"
    if [[ -z "$esperado" ]]; then
      falha "$nome: ausente no manifest do run $run_id"
    elif [[ "$esperado" != "$obtido" ]]; then
      falha "$nome: sha256 do Drive não confere com o manifest"
    fi
  done

  # 2. Cluster descartável: só socket unix, sem TCP.
  "$PGBIN/initdb" -D "$PGDATA" -U rod --auth=trust -E UTF8 --locale=C.UTF-8 >/dev/null
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$TMP/postgres.log" -w \
    -o "-p $PORTA -k $SOCK -c listen_addresses='' -c fsync=off -c synchronous_commit=off" start >/dev/null

  # 3. Restaura e confere cada banco.
  local banco tabelas_dump tabelas_ok migracoes linhas erros t
  for dump in "${dumps[@]}"; do
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

  # O backup cobre nove bancos: menos que isso significa que algum app saiu do
  # offsite sem ninguém notar. Conta os dumps DESTE run, não os da pasta.
  if (( ${#dumps[@]} < 9 )); then
    falha "esperava 9 dumps no run $run_id, achei ${#dumps[@]}"
  fi

  # Chegou ao fim tendo restaurado: só agora o `finalizar` pode gravar a
  # sentinela de sucesso.
  RESTAUROU=1
}

exec >> "$LOG_FILE" 2>&1
main
