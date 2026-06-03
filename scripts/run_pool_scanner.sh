#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
POOL_DRY_RUN="${POOL_DRY_RUN:-false}"

LOG_DIR="${PROJECT_ROOT}/logs"
SESSION_LOG="${LOG_DIR}/pool_scanner_runner_$(date +%F_%H%M%S).log"

mkdir -p "${LOG_DIR}" "${PROJECT_ROOT}/data"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Python da virtualenv nao encontrado: ${PYTHON_BIN}" >&2
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

"${PYTHON_BIN}" -m src.modules.pool_scanner "${args[@]}" 2>&1 | tee -a "${SESSION_LOG}"
