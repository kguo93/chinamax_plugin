# shellcheck shell=bash
# Shared interpreter resolution for the chinamax plugin shims. Sourced, never run.
#
# THE interpreter-discovery order lives ONLY here, so the launcher, the commands
# and the hooks never drift onto three different pythons. In order, taking the
# FIRST that is an absolute path to an executable:
#
#   1. the path /chinamax:setup records at <data root>/python-path
#   2. $CHINAMAX_PYTHON
#   3. ~/miniconda3/envs/chinamax/bin/python
#   4. conda run -n chinamax python              (last resort — not absolute)
#   5. system python3 with the plugin's src/ on PYTHONPATH  (bootstrap rung)
#
# Rung 5 is what breaks setup's bootstrap circularity: on a fresh machine with no
# chinamax env — precisely what the doctor exists to diagnose — every conda rung
# fails, so without it /chinamax:setup could never start at all.

# Codex exposes the Claude-compatible plugin variables too, so native PLUGIN_*
# evidence must win before the Claude family. The shims export this marker before
# Python starts; the Runtime resolves the same precedence again from full input.
chinamax_host_marker() {
  if [ -n "${CHINAMAX_HOST:-}" ]; then
    printf '%s\n' "${CHINAMAX_HOST}"
  elif [ -n "${PLUGIN_ROOT:-}" ] || [ -n "${PLUGIN_DATA:-}" ]; then
    printf '%s\n' codex
  elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    printf '%s\n' claude
  fi
}

if [ -z "${CHINAMAX_HOST:-}" ]; then
  CHINAMAX_HOST="$(chinamax_host_marker || true)"
  [ -n "${CHINAMAX_HOST}" ] && export CHINAMAX_HOST
fi

# Data root = selected Host's plugin data, else the Host-specific XDG fallback.
# Empty or relative values are unset.
chinamax_data_root() {
  if [ "${CHINAMAX_HOST:-}" = codex ] && [ -n "${PLUGIN_DATA:-}" ] && [ "${PLUGIN_DATA#/}" != "${PLUGIN_DATA}" ]; then
    printf '%s\n' "${PLUGIN_DATA}"
  elif [ "${CHINAMAX_HOST:-}" != codex ] && [ -n "${CLAUDE_PLUGIN_DATA:-}" ] && [ "${CLAUDE_PLUGIN_DATA#/}" != "${CLAUDE_PLUGIN_DATA}" ]; then
    printf '%s\n' "${CLAUDE_PLUGIN_DATA}"
  elif [ -n "${XDG_STATE_HOME:-}" ] && [ "${XDG_STATE_HOME#/}" != "${XDG_STATE_HOME}" ]; then
    if [ "${CHINAMAX_HOST:-}" = codex ]; then
      printf '%s\n' "${XDG_STATE_HOME}/chinamax-codex"
    else
      printf '%s\n' "${XDG_STATE_HOME}/chinamax"
    fi
  else
    if [ "${CHINAMAX_HOST:-}" = codex ]; then
      printf '%s\n' "${HOME}/.local/state/chinamax-codex"
    else
      printf '%s\n' "${HOME}/.local/state/chinamax"
    fi
  fi
}

# Print an absolute env python (rungs 1-3), or return 1 to signal the caller to
# fall through to conda run / the bootstrap rung.
chinamax_resolve_python() {
  local recorded_file recorded

  recorded_file="$(chinamax_data_root)/python-path"
  if [ -f "${recorded_file}" ]; then
    recorded="$(head -n1 "${recorded_file}" 2>/dev/null | tr -d '\r\n' || true)"
    if [ -n "${recorded}" ] && [ "${recorded#/}" != "${recorded}" ] && [ -x "${recorded}" ]; then
      printf '%s\n' "${recorded}"
      return 0
    fi
  fi

  if [ -n "${CHINAMAX_PYTHON:-}" ] && [ "${CHINAMAX_PYTHON#/}" != "${CHINAMAX_PYTHON}" ] && [ -x "${CHINAMAX_PYTHON}" ]; then
    printf '%s\n' "${CHINAMAX_PYTHON}"
    return 0
  fi

  if [ -x "${HOME}/miniconda3/envs/chinamax/bin/python" ]; then
    printf '%s\n' "${HOME}/miniconda3/envs/chinamax/bin/python"
    return 0
  fi

  return 1
}

# Resolve the interpreter and exec `python -m <module> "$@"`. Requires
# CHINAMAX_SCRIPT_DIR to be the absolute path to this scripts/ directory.
chinamax_exec() {
  local module="$1"
  shift
  local plugin_root py
  plugin_root="$(dirname "${CHINAMAX_SCRIPT_DIR}")"

  if py="$(chinamax_resolve_python)"; then
    exec "${py}" -m "${module}" "$@"
  fi

  if command -v conda >/dev/null 2>&1 && conda run -n chinamax python -c '' >/dev/null 2>&1; then
    exec conda run -n chinamax python -m "${module}" "$@"
  fi

  exec env "PYTHONPATH=${plugin_root}/src${PYTHONPATH:+:${PYTHONPATH}}" python3 -m "${module}" "$@"
}
