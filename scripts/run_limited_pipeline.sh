#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"

TOTAL_CYCLES="${TOTAL_CYCLES:-30}"
SCANNER_INTERVAL_SECONDS="${SCANNER_INTERVAL_SECONDS:-60}"
SOCIAL_EVERY_CYCLES="${SOCIAL_EVERY_CYCLES:-5}"

LOG_DIR="${PROJECT_ROOT}/logs"
SESSION_LOG="${LOG_DIR}/limited_pipeline_$(date +%F_%H%M%S).log"
LOCK_FILE="${PROJECT_ROOT}/data/limited_pipeline.lock"

mkdir -p "${LOG_DIR}" "${PROJECT_ROOT}/data"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python da virtualenv nao encontrado: ${PYTHON_BIN}" >&2
    echo "Crie a .venv e instale requirements.txt antes de executar este script." >&2
    exit 1
fi

if ! [[ "${TOTAL_CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "TOTAL_CYCLES deve ser um inteiro positivo." >&2
    exit 1
fi

if ! [[ "${SCANNER_INTERVAL_SECONDS}" =~ ^[0-9]+$ ]]; then
    echo "SCANNER_INTERVAL_SECONDS deve ser um inteiro maior ou igual a zero." >&2
    exit 1
fi

if ! [[ "${SOCIAL_EVERY_CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "SOCIAL_EVERY_CYCLES deve ser um inteiro positivo." >&2
    exit 1
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Ja existe uma sessao limitada do pipeline em execucao." >&2
    exit 1
fi

log() {
    printf '[%s] %s\n' "$(date --iso-8601=seconds)" "$*" | tee -a "${SESSION_LOG}"
}

run_module() {
    local module="$1"
    "${PYTHON_BIN}" -m "${module}" 2>&1 | tee -a "${SESSION_LOG}"
}

cd "${PROJECT_ROOT}"

log "=== KRPTO-V | Pipeline limitado ==="
log "Rodadas do token scanner: ${TOTAL_CYCLES}"
log "Intervalo entre rodadas: ${SCANNER_INTERVAL_SECONDS}s"
log "Inferencia social: a cada ${SOCIAL_EVERY_CYCLES} rodadas"
log "Log da sessao: ${SESSION_LOG}"

for ((cycle = 1; cycle <= TOTAL_CYCLES; cycle++)); do
    log "Rodada ${cycle}/${TOTAL_CYCLES}: iniciando token scanner"
    run_module "src.modules.token_scanner"

    if ((cycle % SOCIAL_EVERY_CYCLES == 0)); then
        log "Rodada ${cycle}/${TOTAL_CYCLES}: iniciando inferencia social"
        run_module "src.modules.social_inference"
    fi

    if ((cycle < TOTAL_CYCLES)); then
        log "Aguardando ${SCANNER_INTERVAL_SECONDS}s"
        sleep "${SCANNER_INTERVAL_SECONDS}"
    fi
done

log "Pipeline limitado concluido."
