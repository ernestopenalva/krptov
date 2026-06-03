#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"

MARKET_INTERVAL_SECONDS="${MARKET_INTERVAL_SECONDS:-60}"
MARKET_MAX_CYCLES="${MARKET_MAX_CYCLES:-0}"
MARKET_DRY_RUN="${MARKET_DRY_RUN:-false}"

LOG_DIR="${PROJECT_ROOT}/logs"
SESSION_LOG="${LOG_DIR}/market_ranker_loop_$(date +%F_%H%M%S).log"
LOCK_FILE="${PROJECT_ROOT}/data/market_ranker_loop.lock"

mkdir -p "${LOG_DIR}" "${PROJECT_ROOT}/data"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python da virtualenv nao encontrado: ${PYTHON_BIN}" >&2
    exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Ja existe um loop do market_ranker em execucao." >&2
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "${SESSION_LOG}"
}

run_cycle() {
    local args=()
    if [[ "${MARKET_DRY_RUN}" == "true" ]]; then
        args+=(--dry-run)
    fi

    "${PYTHON_BIN}" -u -m src.modules.market_ranker "${args[@]}" 2>&1 | tee -a "${SESSION_LOG}"
    return "${PIPESTATUS[0]}"
}

cd "${PROJECT_ROOT}"

log "=== KRPTO-V | Market Ranker Loop ==="
log "Intervalo: ${MARKET_INTERVAL_SECONDS}s"
log "Max ciclos: ${MARKET_MAX_CYCLES} (0 = infinito)"
log "Dry-run: ${MARKET_DRY_RUN}"
log "Log da sessao: ${SESSION_LOG}"

cycle=0
while true; do
    cycle=$((cycle + 1))
    log "Ciclo ${cycle}: iniciando market_ranker"
    if ! run_cycle; then
        log "Ciclo ${cycle}: market_ranker falhou; continuando no proximo intervalo"
    fi

    if ((MARKET_MAX_CYCLES > 0 && cycle >= MARKET_MAX_CYCLES)); then
        log "Loop concluido por MARKET_MAX_CYCLES."
        break
    fi

    log "Aguardando ${MARKET_INTERVAL_SECONDS}s"
    sleep "${MARKET_INTERVAL_SECONDS}"
done
