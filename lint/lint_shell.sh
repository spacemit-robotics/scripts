#!/usr/bin/env bash
#
# Copyright (C) 2026 SpacemiT (Hangzhou) Technology Co. Ltd.
# SPDX-License-Identifier: Apache-2.0
#

set -euo pipefail

# Run shellcheck on tracked shell scripts.
#
# Usage:
#   bash scripts/lint/lint_shell.sh              # lint all tracked shell files
#   bash scripts/lint/lint_shell.sh <path>       # lint tracked shell files under <path>
#   bash scripts/lint/lint_shell.sh <file.sh>    # lint a single tracked file
#
# Optional env:
#   SHELLCHECK_ARGS="-e SC1091" bash scripts/lint/lint_shell.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v shellcheck >/dev/null 2>&1; then
  echo "[shellcheck] ERROR: shellcheck not found."
  echo "[shellcheck] Install: sudo apt-get install shellcheck"
  exit 127
fi

filter_shell_files() {
  # Reads NUL-delimited paths from stdin and outputs NUL-delimited shell files.
  while IFS= read -r -d '' f; do
    case "${f}" in
      output/*|target/*|components/thirdparty/*) continue ;;
    esac
    case "${f}" in
      *.sh) printf '%s\0' "${f}" ;;
    esac
  done
}

is_git_repo() {
  git rev-parse --is-inside-work-tree >/dev/null 2>&1
}

list_shell_files_find() {
  # Args: <path or empty>
  # Output: NUL-delimited list of files
  local base="${1:-.}"

  if [[ -f "${base}" ]]; then
    case "${base}" in
      output/*|target/*|components/thirdparty/*) return 0 ;;
      *.sh) printf '%s\0' "${base}" ;;
    esac
    return 0
  fi

  find "${base}" -type f -name '*.sh' \
    -not -path '*/output/*' \
    -not -path '*/target/*' \
    -not -path '*/components/thirdparty/*' \
    -print0
}

TARGET_PATH="${1:-}"
if [[ -z "${TARGET_PATH}" ]]; then
  if is_git_repo; then
    mapfile -d '' FILES < <(git ls-files -z | filter_shell_files)
  else
    mapfile -d '' FILES < <(list_shell_files_find ".")
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
      mapfile -d '' FILES < <(git ls-files -z -- "${TARGET_PATH}" | filter_shell_files)
      if [[ ${#FILES[@]} -eq 0 ]]; then
        mapfile -d '' FILES < <(list_shell_files_find "${TARGET_PATH}")
      fi
    else
      mapfile -d '' FILES < <(list_shell_files_find "${TARGET_PATH}")
    fi
  else
    echo "[shellcheck] ERROR: path not found: ${TARGET_PATH}"
    exit 2
  fi
fi

if [[ ${#FILES[@]} -eq 0 ]]; then
  if [[ -z "${TARGET_PATH}" ]]; then
    echo "[shellcheck] No matching tracked shell files found."
  else
    echo "[shellcheck] No matching tracked shell files found under: ${TARGET_PATH}"
  fi
  exit 0
fi

if [[ -z "${TARGET_PATH}" ]]; then
  echo "[shellcheck] Linting ${#FILES[@]} files..."
else
  echo "[shellcheck] Linting ${#FILES[@]} files under: ${TARGET_PATH}"
fi

# shellcheck disable=SC2086
shellcheck -e SC1091 ${SHELLCHECK_ARGS:-} "${FILES[@]}"
