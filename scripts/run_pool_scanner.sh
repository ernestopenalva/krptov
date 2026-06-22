#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
source "${PROJECT_ROOT}/scripts/lib/system_wake_window.sh"
POOL_DRY_RUN="${POOL_DRY_RUN:-false}"

LOG_DIR="${PROJECT_ROOT}/logs"
SESSION_LOG="${LOG_DIR}/pool_scanner_runner_$(date +%F_%H%M%S).log"
LOCK_FILE="${PROJECT_ROOT}/data/pool_scanner_runner.lock"

mkdir -p "${LOG_DIR}" "${PROJECT_ROOT}/data"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python da virtualenv nao encontrado: ${PYTHON_BIN}" >&2
    exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Ja existe um pool scanner runner em execucao." >&2
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "${SESSION_LOG}"
}

cd "${PROJECT_ROOT}"

args=()
if [[ "${POOL_DRY_RUN}" == "true" ]]; then
    args+=(--dry-run)
fi

log "=== KRPTO-V | Pool Scanner Runner ==="
log "Dry-run: ${POOL_DRY_RUN}"
log "Log da sessao: ${SESSION_LOG}"

while true; do
    if ! system_wake_is_active; then
        wait_seconds="$(system_wake_seconds_until_open)"
        log "Fora da vigilia global; aguardando ${wait_seconds}s para reabrir"
        sleep "${wait_seconds}"
        continue
    fi

    run_seconds="$(system_wake_seconds_until_close)"
    log "Iniciando pool scanner; vigilia fecha em ${run_seconds}s"
    set +e
    timeout --foreground --signal=TERM --kill-after=30s "${run_seconds}s" \
        "${PYTHON_BIN}" -u -m src.modules.pool_scanner "${args[@]}" 2>&1 | tee -a "${SESSION_LOG}"
    scanner_status="${PIPESTATUS[0]}"
    set -e

    if [[ "${scanner_status}" == "124" ]]; then
        log "Pool scanner encerrado pelo fechamento da vigilia"
    elif [[ "${scanner_status}" != "0" ]]; then
        log "Pool scanner encerrou com status ${scanner_status}; nova tentativa em 10s se a vigilia continuar"
        sleep 10
    else
        log "Pool scanner encerrou normalmente durante a vigilia; nova tentativa em 10s"
        sleep 10
    fi
done
