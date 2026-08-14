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

chinamax_windows() {
  [ "${OS:-}" = Windows_NT ]
}

chinamax_macos() {
  [ "$(uname -s 2>/dev/null)" = Darwin ]
}

chinamax_shell_path() {
  local value="${1:-}"
  [ -n "${value}" ] || return 0
  if chinamax_windows && command -v cygpath >/dev/null 2>&1; then
    cygpath -u -- "${value}"
  else
    printf '%s\n' "${value}"
  fi
}

chinamax_absolute() {
  local value="${1:-}"
  if [ -z "${value}" ]; then
    return 1
  fi
  if chinamax_windows; then
    case "${value}" in
      [A-Za-z]:[\\/]*|[\\/][\\/]*) return 0 ;;
      /*) return 0 ;;
    esac
    return 1
  fi
  [ "${value#/}" != "${value}" ]
}

if [ -z "${CHINAMAX_HOST:-}" ]; then
  CHINAMAX_HOST="$(chinamax_host_marker || true)"
  [ -n "${CHINAMAX_HOST}" ] && export CHINAMAX_HOST
fi

# Data root = selected Host's plugin data, else the native per-Platform fallback
# (Windows LocalAppData, macOS Application Support, else XDG / ~/.local/state).
# Empty or relative values are unset.
chinamax_data_root() {
  if [ "${CHINAMAX_HOST:-}" = codex ] && chinamax_absolute "${PLUGIN_DATA:-}"; then
    chinamax_shell_path "${PLUGIN_DATA}"
  elif [ "${CHINAMAX_HOST:-}" != codex ] && chinamax_absolute "${CLAUDE_PLUGIN_DATA:-}"; then
    chinamax_shell_path "${CLAUDE_PLUGIN_DATA}"
  elif chinamax_windows; then
    local local_appdata userprofile
    local_appdata="$(chinamax_shell_path "${LOCALAPPDATA:-}")"
    userprofile="$(chinamax_shell_path "${USERPROFILE:-${HOME:-}}")"
    if [ -n "${local_appdata}" ]; then
      if [ "${CHINAMAX_HOST:-}" = codex ]; then
        printf '%s\n' "${local_appdata}/chinamax-codex"
      else
        printf '%s\n' "${local_appdata}/chinamax"
      fi
    elif [ "${CHINAMAX_HOST:-}" = codex ]; then
      printf '%s\n' "${userprofile}/AppData/Local/chinamax-codex"
    else
      printf '%s\n' "${userprofile}/AppData/Local/chinamax"
    fi
  elif chinamax_macos; then
    if [ "${CHINAMAX_HOST:-}" = codex ]; then
      printf '%s\n' "${HOME}/Library/Application Support/chinamax-codex"
    else
      printf '%s\n' "${HOME}/Library/Application Support/chinamax"
    fi
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
    recorded="$(chinamax_shell_path "${recorded}")"
    if chinamax_absolute "${recorded}" && [ -x "${recorded}" ]; then
      printf '%s\n' "${recorded}"
      return 0
    fi
  fi

  local explicit
  explicit="$(chinamax_shell_path "${CHINAMAX_PYTHON:-}")"
  if chinamax_absolute "${explicit}" && [ -x "${explicit}" ]; then
    printf '%s\n' "${explicit}"
    return 0
  fi

  local home
  home="$(chinamax_shell_path "${USERPROFILE:-${HOME:-}}")"
  if chinamax_windows; then
    if [ -x "${home}/miniconda3/envs/chinamax/python.exe" ]; then
      printf '%s\n' "${home}/miniconda3/envs/chinamax/python.exe"
      return 0
    fi
  else
    if [ -x "${home}/miniconda3/envs/chinamax/bin/python" ]; then
      printf '%s\n' "${home}/miniconda3/envs/chinamax/bin/python"
      return 0
    fi
  fi

  return 1
}

# macOS ships no usable Python 3 by default: /usr/bin/python3 is an Xcode Command
# Line Tools stub, not an interpreter, so the bare-python bootstrap rung can
# dead-end. Refuse rather than exec a stub; the doctor cannot diagnose the very
# interpreter it needs to start (ADR 0009/0015). Writes to stderr itself.
chinamax_report_no_python() {
  cat >&2 <<'EOF'
chinamax: no usable Python 3 found — cannot start setup.

macOS does not ship a real Python 3: /usr/bin/python3 is an Xcode Command Line
Tools stub, not an interpreter. Install a real Python 3, then re-run
/chinamax:setup. Any one of these:

  * Miniconda (one step; also satisfies chinamax's conda requirement):
      https://repo.anaconda.com/miniconda/ (Miniconda3-latest-MacOSX-arm64.sh)
  * Homebrew:   brew install python@3.12
  * python.org: the macOS universal2 installer

chinamax will not proceed until a real python3 is on PATH.
EOF
}

# Resolve the interpreter and exec `python -m <module> "$@"`. Requires
# CHINAMAX_SCRIPT_DIR to be the absolute path to this scripts/ directory.
chinamax_exec() {
  local module="$1"
  shift
  local plugin_root py home
  plugin_root="$(dirname "${CHINAMAX_SCRIPT_DIR}")"
  home="$(chinamax_shell_path "${USERPROFILE:-${HOME:-}}")"

  if py="$(chinamax_resolve_python)"; then
    exec "${py}" -m "${module}" "$@"
  fi

  local conda_cmd
  conda_cmd="$(command -v conda || true)"
  if chinamax_windows && [ -x "${home}/miniconda3/Scripts/conda.exe" ]; then
    conda_cmd="${home}/miniconda3/Scripts/conda.exe"
  fi
  if [ -n "${conda_cmd}" ] && "${conda_cmd}" run -n chinamax python -c '' >/dev/null 2>&1; then
    exec "${conda_cmd}" run -n chinamax python -m "${module}" "$@"
  fi

  local path_sep python_cmd bootstrap_python py_path
  if chinamax_windows; then
    path_sep=';'
    python_cmd=python
    bootstrap_python="${home}/miniconda3/python.exe"
  else
    path_sep=':'
    python_cmd=python3
    bootstrap_python="${home}/miniconda3/bin/python"
  fi
  if [ -x "${bootstrap_python}" ]; then
    exec env "PYTHONPATH=${plugin_root}/src${PYTHONPATH:+${path_sep}${PYTHONPATH}}" "${bootstrap_python}" -m "${module}" "$@"
  fi
  # On macOS the bare-python rung must not exec a missing or stub interpreter.
  # Detect GUI-safely — never EXECUTE python3, since running the CLT stub can pop
  # the installer — using command -v + xcode-select -p. A non-stub python3
  # resolves off /usr/bin/python3 and is accepted; the Apple stub at
  # /usr/bin/python3 works only with the CLT installed (xcode-select -p succeeds);
  # nothing resolvable is rejected (ADR 0009/0015). Linux/Windows are unaffected.
  if chinamax_macos; then
    py_path="$(command -v "${python_cmd}" 2>/dev/null || true)"
    if [ -z "${py_path}" ] || { [ "${py_path}" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; }; then
      chinamax_report_no_python
      exit 1
    fi
  fi
  exec env "PYTHONPATH=${plugin_root}/src${PYTHONPATH:+${path_sep}${PYTHONPATH}}" "${python_cmd}" -m "${module}" "$@"
}
