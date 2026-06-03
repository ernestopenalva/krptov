#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"

SOCIAL_INTERVAL_SECONDS="${SOCIAL_INTERVAL_SECONDS:-300}"
SOCIAL_MAX_CYCLES="${SOCIAL_MAX_CYCLES:-0}"

LOG_DIR="${PROJECT_ROOT}/logs"
SESSION_LOG="${LOG_DIR}/social_inference_loop_$(date +%F_%H%M%S).log"
LOCK_FILE="${PROJECT_ROOT}/data/social_inference_loop.lock"

mkdir -p "${LOG_DIR}" "${PROJECT_ROOT}/data"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python da virtualenv nao encontrado: ${PYTHON_BIN}" >&2
    exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Ja existe um loop da social_inference em execucao." >&2
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "${SESSION_LOG}"
}

run_cycle() {
    "${PYTHON_BIN}" -m src.modules.social_inference 2>&1 | tee -a "${SESSION_LOG}"
    return "${PIPESTATUS[0]}"
}

cd "${PROJECT_ROOT}"

log "=== KRPTO-V | Social Inference Loop ==="
log "Intervalo: ${SOCIAL_INTERVAL_SECONDS}s"
log "Max ciclos: ${SOCIAL_MAX_CYCLES} (0 = infinito)"
log "Log da sessao: ${SESSION_LOG}"

cycle=0
while true; do
    cycle=$((cycle + 1))
    log "Ciclo ${cycle}: iniciando social_inference"
    if ! run_cycle; then
        log "Ciclo ${cycle}: social_inference falhou; continuando no proximo intervalo"
    fi

    if ((SOCIAL_MAX_CYCLES > 0 && cycle >= SOCIAL_MAX_CYCLES)); then
        log "Loop concluido por SOCIAL_MAX_CYCLES."
        break
    fi

    log "Aguardando ${SOCIAL_INTERVAL_SECONDS}s"
    sleep "${SOCIAL_INTERVAL_SECONDS}"
done
