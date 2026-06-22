#!/usr/bin/env bash

SYSTEM_WAKE_MODULE="src.modules.system_wake_window"
SYSTEM_WAKE_CONFIG="${PROJECT_ROOT}/config/config.yaml"

system_wake_is_active() {
    "${PYTHON_BIN}" -m "${SYSTEM_WAKE_MODULE}" --config "${SYSTEM_WAKE_CONFIG}" --is-active
}

system_wake_seconds_until_open() {
    "${PYTHON_BIN}" -m "${SYSTEM_WAKE_MODULE}" --config "${SYSTEM_WAKE_CONFIG}" --seconds-until-open
}

system_wake_seconds_until_close() {
    "${PYTHON_BIN}" -m "${SYSTEM_WAKE_MODULE}" --config "${SYSTEM_WAKE_CONFIG}" --seconds-until-close
}
