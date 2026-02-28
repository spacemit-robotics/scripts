#!/usr/bin/env bash
#
# Copyright (C) 2026 SpacemiT (Hangzhou) Technology Co. Ltd.
# SPDX-License-Identifier: Apache-2.0
#

set -euo pipefail

# Run Python lint (PEP 8 + quality checks) on tracked Python source files via ruff.
#
# Usage:
#   bash scripts/lint/pep8.sh               # lint all tracked Python files
#   bash scripts/lint/pep8.sh <path>        # lint tracked Python files under <path>
#   bash scripts/lint/pep8.sh <file.py>     # lint a single tracked file
#
# Optional env:
#   RUFF_ARGS="--select E,W,F --ignore E203,W503" bash scripts/lint/pep8.sh
#   RUFF_CONFIG=scripts/lint/.ruff.toml bash scripts/lint/pep8.sh
# Backward compatibility:
#   PEP8_ARGS is still accepted and forwarded to ruff.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v ruff >/dev/null 2>&1; then
  echo "[pep8] ERROR: ruff not found."
  echo "[pep8] Install: python3 -m pip install --user ruff"
  echo "[pep8] (On Ubuntu with PEP 668, prefer: pipx install ruff)"
  exit 127
fi

CONFIG_FILE="${RUFF_CONFIG:-scripts/lint/.ruff.toml}"
CONFIG_ARGS=()
if [[ -f "${CONFIG_FILE}" ]]; then
  CONFIG_ARGS+=(--config="${CONFIG_FILE}")
else
  echo "[pep8] WARN: config file not found: ${CONFIG_FILE} (using ruff defaults)"
fi

filter_py_files() {
  # Reads NUL-delimited paths from stdin and outputs NUL-delimited Python files.
  while IFS= read -r -d '' f; do
    case "${f}" in
      output/*|target/*|components/thirdparty/*|build/*|install/*|log/*) continue ;;
      *.py) printf '%s\0' "${f}" ;;
    esac
  done
}

is_git_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1
}

list_py_files_find() {
  # Args: <path or empty>
  # Output: NUL-delimited list of files
  local base="${1:-.}"

  if [[ -f "${base}" ]]; then
    case "${base}" in
      output/*|target/*|components/thirdparty/*|build/*|install/*|log/*) return 0 ;;
      *.py) printf '%s\0' "${base}" ;;
    esac
    return 0
  fi

  find "${base}" -type f -name '*.py' \
    -not -path '*/output/*' \
    -not -path '*/target/*' \
    -not -path '*/components/thirdparty/*' \
    -not -path '*/build/*' \
    -not -path '*/install/*' \
    -not -path '*/log/*' \
    -not -path '*/.venv/*' \
    -not -path '*/venv/*' \
    -not -path '*/__pycache__/*' \
    -print0
}

# Positional arguments:
#   [path] [ruff args...]
# Examples:
#   pep8.sh components/foo --fix
#   pep8.sh --fix --unsafe-fixes components/foo   (path optional; if omitted, lint all)
TARGET_PATH=""
EXTRA_ARGS=()
if [[ $# -gt 0 ]]; then
  if [[ "${1}" == -* ]]; then
    EXTRA_ARGS=("$@")
  else
    TARGET_PATH="${1}"
    shift
    EXTRA_ARGS=("$@")
  fi
fi
if [[ -z "${TARGET_PATH}" ]]; then
  if is_git_repo; then
    mapfile -d '' FILES < <(git ls-files -z | filter_py_files)
  else
    mapfile -d '' FILES < <(list_py_files_find ".")
  fi
else
  if [[ "${TARGET_PATH}" = /* ]]; then
    if command -v realpath >/dev/null 2>&1; then
      ABS_TARGET="$(realpath -m "${TARGET_PATH}")"
      ABS_ROOT="$(realpath -m "${ROOT_DIR}")"
      case "${ABS_TARGET}" in
        "${ABS_ROOT}"/*) TARGET_PATH="${ABS_TARGET#"${ABS_ROOT}"/}" ;;
      esac
    fi
  fi

  if [[ -e "${TARGET_PATH}" ]]; then
    if is_git_repo; then
      mapfile -d '' FILES < <(git ls-files -z -- "${TARGET_PATH}" | filter_py_files)
      if [[ ${#FILES[@]} -eq 0 ]]; then
        mapfile -d '' FILES < <(list_py_files_find "${TARGET_PATH}")
      fi
    else
      mapfile -d '' FILES < <(list_py_files_find "${TARGET_PATH}")
    fi
  else
    echo "[pep8] ERROR: path not found: ${TARGET_PATH}"
    exit 2
  fi
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  if [[ -z "${TARGET_PATH}" ]]; then
    echo "[pep8] No matching tracked Python files found."
  else
    echo "[pep8] No matching tracked Python files found under: ${TARGET_PATH}"
  fi
  exit 0
fi

if [[ -z "${TARGET_PATH}" ]]; then
  echo "[pep8] Linting ${#FILES[@]} files..."
else
  echo "[pep8] Linting ${#FILES[@]} files under: ${TARGET_PATH}"
fi

had_error=0
# Backward compatibility: allow users still passing PEP8_ARGS.
EFFECTIVE_ARGS=()
if [[ -n "${RUFF_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  EFFECTIVE_ARGS+=( ${RUFF_ARGS} )
elif [[ -n "${PEP8_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  EFFECTIVE_ARGS+=( ${PEP8_ARGS} )
fi
EFFECTIVE_ARGS+=("${EXTRA_ARGS[@]}")

ruff check --output-format=concise --color=never "${CONFIG_ARGS[@]}" "${EFFECTIVE_ARGS[@]}" "${FILES[@]}" || had_error=1

exit "${had_error}"

