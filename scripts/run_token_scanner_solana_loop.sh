#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
source "${PROJECT_ROOT}/scripts/lib/system_wake_window.sh"

INTERVAL_SECONDS="${SOLANA_SCANNER_INTERVAL_SECONDS:-60}"
LOG_DIR="${PROJECT_ROOT}/logs"
SESSION_LOG="${LOG_DIR}/token_scanner_solana_runner_$(date +%F_%H%M%S).log"
LOCK_FILE="${PROJECT_ROOT}/data/token_scanner_solana_runner.lock"

mkdir -p "${LOG_DIR}" "${PROJECT_ROOT}/data"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python da virtualenv nao encontrado: ${PYTHON_BIN}" >&2
    exit 1
fi

if ! [[ "${INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "SOLANA_SCANNER_INTERVAL_SECONDS deve ser inteiro maior ou igual a zero." >&2
    exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Ja existe um Token Scanner Solana em execucao." >&2
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "${SESSION_LOG}"
}

cd "${PROJECT_ROOT}"
log "=== KRPTO-V | Token Scanner Solana Runner ==="
log "Intervalo apos cada ciclo: ${INTERVAL_SECONDS}s"
log "Log da sessao: ${SESSION_LOG}"

while true; do
    if ! system_wake_is_active; then
        wait_seconds="$(system_wake_seconds_until_open)"
        log "Fora da vigilia global; aguardando ${wait_seconds}s para reabrir"
        sleep "${wait_seconds}"
        continue
    fi

    log "Iniciando ciclo de descoberta Solana"
    set +e
    "${PYTHON_BIN}" -u -m src.modules.token_scanner_solana --run-forever 2>&1 | tee -a "${SESSION_LOG}"
    scanner_status="${PIPESTATUS[0]}"
    set -e

    if [[ "${scanner_status}" != "0" ]]; then
        log "Scanner encerrou com status ${scanner_status}; repetindo apos ${INTERVAL_SECONDS}s"
    else
        log "Scanner concluiu o ciclo; proximo ciclo apos ${INTERVAL_SECONDS}s"
    fi
    sleep "${INTERVAL_SECONDS}"
done
