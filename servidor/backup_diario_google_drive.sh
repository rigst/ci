#!/usr/bin/env bash
# -E é obrigatório: sem ele o `trap ... ERR` lá embaixo NÃO é herdado por
# main(), que é onde tudo acontece. O backup falhou em 07, 08 e 09/11/2026 e
# nenhum e-mail `[BACKUP FALHOU]` saiu — quem acusou foi só o
# verifica_backup_recente.sh do dia seguinte, 3h depois.
#
# shellcheck disable=SC2094
# O bloco de main() redireciona para $LOG_FILE e, de propósito, lê esse mesmo
# arquivo com `tail` para colar o fim do log no e-mail de alerta. Append e
# leitura posicionada não conflitam aqui.
set -Eeuo pipefail

REMOTE="${RCLONE_REMOTE:-gdrive:Backups/srv1553321}"
# Retenção REMOTA (Drive) dos backups dos apps: janela de recuperação (DR).
# A guarda legal de 6 meses é dos REGISTROS DE ACESSO (logs), atendida à parte
# pelo nginx (logrotate 200 dias) — não pelos dumps aqui.
RETENTION_DAYS="${RETENTION_DAYS:-30}"
# Retenção LOCAL (staging em disco) desacoplada da remota: o disco só mantém os
# últimos dias para não encher.
LOCAL_RETENTION_DAYS="${LOCAL_RETENTION_DAYS:-14}"
BASE_DIR="/home/rod/backups_offsite"
RUN_ID="$(date -u +'%Y%m%dT%H%M%SZ')"
RUN_DATE="$(date -u +'%Y-%m-%d')"
STAGING="$BASE_DIR/staging/$RUN_ID"
LOG_DIR="$BASE_DIR/logs"
LOG_FILE="$LOG_DIR/google-drive-backup.log"
LOCK_FILE="/tmp/google_drive_backup.lock"
# Sentinela de sucesso (usada pela verificação de frescor do backup).
STATUS_FILE="$BASE_DIR/last_success.txt"
# Destino dos alertas por e-mail (via msmtp, conta default rodrigo@stolben.com).
ALERT_TO="${BACKUP_ALERT_TO:-alertas@stolben.com}"

mkdir -p "$STAGING/databases" "$STAGING/manifests" "$LOG_DIR"

log() {
  printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# Envia um alerta por e-mail (nunca aborta o script se o envio falhar).
alert() {
  local subject="$1" body="$2"
  printf 'To: %s\nFrom: rodrigo@stolben.com\nSubject: %s\n\n%s\n' \
    "$ALERT_TO" "$subject" "$body" | msmtp "$ALERT_TO" 2>>"$LOG_FILE" \
    || log "AVISO: não consegui enviar o e-mail de alerta para $ALERT_TO"
}

# Dispara em qualquer comando que falhe (set -e). Avisa o Rod por e-mail com o
# fim do log e propaga o erro (a sentinela de sucesso não é escrita).
on_error() {
  local exit_code=$? line="$1"
  log "ERRO fatal na linha $line (exit $exit_code)"
  alert "[BACKUP FALHOU] srv1553321 $(date -u +%F)" \
    "$(printf 'O backup offsite FALHOU.\nRUN_ID=%s  linha=%s  exit=%s\n\nÚltimas linhas do log:\n%s' \
       "$RUN_ID" "$line" "$exit_code" "$(tail -n 25 "$LOG_FILE" 2>/dev/null)")"
}
trap 'on_error $LINENO' ERR

# Apps cujo dump diário falhou ou veio velho. O backup segue com o que der (uma
# cópia parcial no Drive vale mais que nenhuma), mas no fim avisa e sai 1 sem
# gravar a sentinela de sucesso — então o verifica_backup_recente.sh continua
# cobrando. Antes, um único app com problema abortava o offsite dos nove.
DEGRADADOS=()

# Roda o backup_postgres.sh de um app sem derrubar o offsite se ele falhar.
run_app_backup() {
  local app="$1" script="$2" rc=0
  RETENTION_DAYS=14 "$script" || rc=$?
  if (( rc != 0 )); then
    log "AVISO: backup_postgres.sh de $app falhou (exit $rc); sigo com o dump anterior"
    DEGRADADOS+=("$app: backup_postgres.sh falhou (exit $rc)")
  fi
}

latest_dump() {
  local dir="$1"
  # O `|| true` segura o pipefail: diretório ausente faz o find sair != 0 e,
  # com `set -E`, isso dispararia o trap ERR aqui dentro da substituição de
  # comando (e-mail de falha fatal) em vez do aviso de degradação lá embaixo.
  { find "$dir" -maxdepth 1 -type f -name '*.dump' -printf '%T@ %p\n' 2>/dev/null || true; } \
    | sort -nr \
    | awk 'NR == 1 {sub(/^[^ ]+ /, ""); print}'
}

copy_latest_dump() {
  local app="$1"
  local dir="$2"
  local dump idade

  dump="$(latest_dump "$dir")"
  if [[ -z "$dump" ]]; then
    log "ERRO: nenhum dump encontrado para $app em $dir"
    DEGRADADOS+=("$app: nenhum dump em $dir")
    return 0
  fi

  cp -p "$dump" "$STAGING/databases/"
  pg_restore -l "$STAGING/databases/$(basename "$dump")" >/dev/null

  # Dump de ontem ou mais velho significa que o dump de hoje não foi gerado:
  # o arquivo sobe (melhor que nada), mas conta como degradação.
  idade=$(( $(date -u +%s) - $(stat -c %Y "$dump") ))
  if (( idade > 36 * 3600 )); then
    log "AVISO: dump de $app tem $(( idade / 3600 ))h: $(basename "$dump")"
    DEGRADADOS+=("$app: dump com $(( idade / 3600 ))h ($(basename "$dump"))")
  fi

  log "Dump incluído: $app -> $(basename "$dump")"
}

make_tar() {
  local name="$1"
  local source="$2"
  shift 2
  local extra=("$@")   # excludes adicionais (ex.: --exclude='media' em apps flat)
  local outfile="$STAGING/${name}_${RUN_ID}.tar.gz"

  tar -C "$(dirname "$source")" \
    --exclude='.git' \
    --exclude='.codex' \
    --exclude='.idea' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='node_modules' \
    --exclude='staticfiles' \
    --exclude='*.log' \
    "${extra[@]+"${extra[@]}"}" \
    -czf "$outfile" \
    "$(basename "$source")"

  log "Pacote incluído: $(basename "$outfile")"
}

# Config de infraestrutura (nginx, Alloy, unidades systemd) + inventário do
# servidor (pacotes, crontab, timers). Objetivo: reconstruir o servidor sem
# depender de memória. Inclui segredos embutidos de propósito (token do Alloy)
# — decisão do Rod em 2026-09-01, mesmo nível de exposição dos dumps de banco
# que já vão pro mesmo Drive.
#
# TLS (/etc/letsencrypt) e firewall (/etc/ufw) são root-only e o rod não tem
# NOPASSWD genérico — por isso não entram direto daqui. Se existir um estágio
# prévio deixado por um cron root em backups_offsite/root_staging (rsync de
# /etc/letsencrypt e /etc/ufw), ele é reaproveitado; senão, só avisa no log e
# seguimos sem eles (perda aceitável: o certbot reemite certificado sozinho).
snapshot_infra() {
  local tmp="$STAGING/_infra_tmp"
  local outfile="$STAGING/infra_${RUN_ID}.tar.gz"
  local root_staging="$BASE_DIR/root_staging"

  mkdir -p "$tmp/etc/systemd_system" "$tmp/inventory"

  if [[ -d /etc/nginx ]]; then
    cp -a /etc/nginx "$tmp/etc/"
  fi

  if [[ -f /etc/alloy/config.alloy ]]; then
    mkdir -p "$tmp/etc/alloy"
    cp -a /etc/alloy/config.alloy "$tmp/etc/alloy/"
  fi

  find /etc/systemd/system -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) \
    -exec cp -a {} "$tmp/etc/systemd_system/" \; 2>/dev/null || true

  if [[ -r "$root_staging/letsencrypt" ]]; then
    cp -a "$root_staging/letsencrypt" "$tmp/etc/"
    log "infra: /etc/letsencrypt incluído via root_staging"
  else
    log "AVISO infra: /etc/letsencrypt não disponível (root-only, sem root_staging) — chaves TLS fora do backup"
  fi

  if [[ -r "$root_staging/ufw" ]]; then
    cp -a "$root_staging/ufw" "$tmp/etc/"
    log "infra: /etc/ufw incluído via root_staging"
  else
    log "AVISO infra: /etc/ufw não disponível (root-only, sem root_staging) — regras de firewall fora do backup"
  fi

  dpkg --get-selections > "$tmp/inventory/dpkg-selections.txt" 2>/dev/null || true
  crontab -l > "$tmp/inventory/crontab-rod.txt" 2>/dev/null || true
  systemctl list-unit-files --state=enabled > "$tmp/inventory/systemd-enabled.txt" 2>&1 || true
  systemctl list-timers --all > "$tmp/inventory/systemd-timers.txt" 2>&1 || true

  tar -C "$tmp" -czf "$outfile" .
  rm -rf "$tmp"

  log "Pacote incluído: $(basename "$outfile")"
}

write_manifest() {
  {
    echo "run_id=$RUN_ID"
    echo "run_date=$RUN_DATE"
    echo "hostname=$(hostname -f 2>/dev/null || hostname)"
    echo "remote=$REMOTE"
    echo
    echo "[files]"
    find "$STAGING" -type f -printf '%s  %P\n' | sort
    echo
    echo "[checksums]"
    cd "$STAGING"
    find . -type f ! -path './manifests/*' -print0 | sort -z | xargs -0 sha256sum
  } > "$STAGING/manifests/manifest_${RUN_ID}.txt"
}

cleanup_local() {
  find "$BASE_DIR/staging" -mindepth 1 -maxdepth 1 -type d -mtime +"$LOCAL_RETENTION_DAYS" -exec rm -rf {} +
}

# Remove SÓ as pastas datadas (YYYY-MM-DD) mais antigas que a retenção — nunca o
# remote inteiro. Assim `_docs` e qualquer outra coisa fora do padrão ficam
# intactas. Nunca aborta o backup (a cópia já subiu); no máximo avisa no log.
prune_remote() {
  local cutoff dirs d
  cutoff="$(date -u -d "${RETENTION_DAYS} days ago" +%Y-%m-%d)"
  log "Removendo pastas datadas anteriores a $cutoff (retenção ${RETENTION_DAYS}d)"
  dirs="$(rclone lsf --dirs-only "$REMOTE" 2>>"$LOG_FILE")" \
    || { log "AVISO: rclone lsf falhou; pulando limpeza remota"; return 0; }
  while IFS= read -r d; do
    d="${d%/}"
    [[ "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue   # só pastas de data
    if [[ "$d" < "$cutoff" ]]; then
      rclone purge "$REMOTE/$d" --log-file "$LOG_FILE" --log-level INFO \
        || log "AVISO: falha ao purgar $REMOTE/$d"
    fi
  done <<< "$dirs"
}

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || { log "Backup do Google Drive já está em execução; saindo"; exit 0; }

  {
    log "Backup Google Drive iniciado: $RUN_ID"

    command -v rclone >/dev/null
    command -v pg_restore >/dev/null

    run_app_backup "sistema_orcamentos" /var/www/sistema_orcamentos/shared/scripts/backup_postgres.sh
    run_app_backup "sistema_financas" /var/www/sistema_financas/shared/scripts/backup_postgres.sh
    run_app_backup "sistema_vetorial" /var/www/sistema_vetorial/shared/scripts/backup_postgres.sh
    run_app_backup "sistema_arq" /var/www/sistema_arq/shared/scripts/backup_postgres.sh
    run_app_backup "sistema_questoes" /var/www/sistema_questoes/shared/scripts/backup_postgres.sh
    run_app_backup "sistema_trilhas" /var/www/sistema_trilhas/deploy/backup_postgres.sh
    run_app_backup "divisor_pdf" /var/www/divisor_pdf/deploy/backup_postgres.sh
    run_app_backup "dojo" /var/www/dojo/deploy/backup_postgres.sh
    run_app_backup "dracma" /var/www/dracma/deploy/backup_postgres.sh

    copy_latest_dump "sistema_orcamentos" "/var/www/sistema_orcamentos/shared/backups/postgres"
    copy_latest_dump "sistema_financas" "/var/www/sistema_financas/shared/backups/postgres"
    copy_latest_dump "sistema_vetorial" "/var/www/sistema_vetorial/shared/backups/postgres"
    copy_latest_dump "sistema_arq" "/var/www/sistema_arq/shared/backups/postgres"
    copy_latest_dump "sistema_questoes" "/var/www/sistema_questoes/shared/backups/postgres"
    copy_latest_dump "sistema_trilhas" "/home/rod/backups/sistema_trilhas/postgres"
    copy_latest_dump "divisor_pdf" "/home/rod/backups/divisor_pdf/postgres"
    copy_latest_dump "dojo" "/home/rod/backups/dojo/postgres"
    copy_latest_dump "dracma" "/home/rod/backups/dracma/postgres"

    make_tar "site_stolben" "/var/www/site_stolben"
    make_tar "sistema_orcamentos_current" "/var/www/sistema_orcamentos/current"
    make_tar "sistema_financas_current" "/var/www/sistema_financas/current"
    make_tar "sistema_vetorial_current" "/var/www/sistema_vetorial/current"
    make_tar "sistema_arq_current" "/var/www/sistema_arq/current"

    if [[ -d /var/www/sistema_orcamentos/shared/media ]]; then
      make_tar "sistema_orcamentos_media" "/var/www/sistema_orcamentos/shared/media"
    fi
    if [[ -d /var/www/sistema_financas/shared/media ]]; then
      make_tar "sistema_financas_media" "/var/www/sistema_financas/shared/media"
    fi
    if [[ -d /var/www/sistema_vetorial/shared/private_media ]]; then
      make_tar "sistema_vetorial_private_media" "/var/www/sistema_vetorial/shared/private_media"
    fi
    if [[ -d /var/www/sistema_arq/shared/media ]]; then
      make_tar "sistema_arq_media" "/var/www/sistema_arq/shared/media"
    fi

    # Código (lean): exclui media/sqlite/celerybeat para não duplicar o snapshot
    # de media (o questoes guarda media em current/media).
    make_tar "sistema_questoes_current" "/var/www/sistema_questoes/current" \
      --exclude='media'
    make_tar "sistema_trilhas_app" "/var/www/sistema_trilhas" \
      --exclude='media' --exclude='*.sqlite3' --exclude='celerybeat-schedule'
    make_tar "divisor_pdf_app" "/var/www/divisor_pdf" \
      --exclude='media' --exclude='*.sqlite3' --exclude='celerybeat-schedule'
    make_tar "dojo_app" "/var/www/dojo" \
      --exclude='*.sqlite3' --exclude='testes_navegador' --exclude='.pytest_cache'
    make_tar "dracma_app" "/var/www/dracma" \
      --exclude='media' --exclude='*.sqlite3' --exclude='celerybeat-schedule' \
      --exclude='.coverage' --exclude='.mypy_cache' --exclude='.ruff_cache'

    # Media (uploads) — snapshot diário datado, igual aos demais apps.
    if [[ -d /var/www/sistema_questoes/current/media ]]; then
      make_tar "sistema_questoes_media" "/var/www/sistema_questoes/current/media"
    fi
    if [[ -d /var/www/sistema_trilhas/media ]]; then
      make_tar "sistema_trilhas_media" "/var/www/sistema_trilhas/media"
    fi
    if [[ -d /var/www/divisor_pdf/media ]]; then
      make_tar "divisor_pdf_media" "/var/www/divisor_pdf/media"
    fi
    # dojo não tem upload de arquivo (sem MEDIA_ROOT) — nada a arquivar aqui.
    if [[ -d /var/www/dracma/media ]]; then
      make_tar "dracma_media" "/var/www/dracma/media"
    fi

    snapshot_infra

    write_manifest

    log "Enviando para $REMOTE/$RUN_DATE/"
    rclone copy "$STAGING" "$REMOTE/$RUN_DATE" \
      --create-empty-src-dirs \
      --transfers 4 \
      --checkers 8 \
      --retries 3 \
      --low-level-retries 10 \
      --log-file "$LOG_FILE" \
      --log-level INFO

    # Integridade: confere por checksum que tudo que está no staging chegou
    # íntegro ao Drive. --one-way ignora arquivos extras já presentes na pasta
    # do dia (ex.: uma execução anterior). Falhou = alerta e aborta.
    log "Verificando integridade do upload (checksum)"
    if ! rclone check "$STAGING" "$REMOTE/$RUN_DATE" --one-way --checksum \
         --log-file "$LOG_FILE" --log-level INFO; then
      alert "[BACKUP INTEGRIDADE] srv1553321 $(date -u +%F)" \
        "$(printf 'Verificação de integridade FALHOU: arquivos locais não conferem no Drive.\nRUN_ID=%s\n\nÚltimas linhas do log:\n%s' \
           "$RUN_ID" "$(tail -n 25 "$LOG_FILE" 2>/dev/null)")"
      exit 1
    fi

    prune_remote || true
    cleanup_local

    # Cópia parcial não é sucesso: a sentinela NÃO é gravada, então o
    # verifica_backup_recente.sh das 06:00 segue cobrando até o problema do app
    # ser resolvido. O que deu para subir já está no Drive.
    if (( ${#DEGRADADOS[@]} > 0 )); then
      log "Backup Google Drive PARCIAL: $RUN_ID (${#DEGRADADOS[@]} app(s) degradado(s))"
      alert "[BACKUP PARCIAL] srv1553321 $(date -u +%F)" \
        "$(printf 'O backup offsite subiu, mas %s app(s) estão degradados.\nRUN_ID=%s\n\n%s\n\nA sentinela de sucesso NÃO foi atualizada.' \
           "${#DEGRADADOS[@]}" "$RUN_ID" "$(printf ' - %s\n' "${DEGRADADOS[@]}")")"
      exit 1
    fi

    # Sentinela de sucesso: usada pela verificação diária de frescor do backup.
    printf 'run_id=%s\nfinished_at=%s\n' "$RUN_ID" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" > "$STATUS_FILE"
    log "Backup Google Drive concluído: $RUN_ID"
  } >> "$LOG_FILE" 2>&1
}

main "$@"
