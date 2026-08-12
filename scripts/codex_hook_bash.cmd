@echo off
setlocal
rem Resolve Git Bash: the default Git for Windows install roots FIRST — a true
rem mirror of doctor._git_for_windows_roots (each root read via a quoted `set`, and
rem SKIPPED when its var is unset, exactly like Python's `if base:`) and
rem _GIT_FOR_WINDOWS_EXES["bash"] — then PATH as a fallback. Root-first keeps a
rem stray WSL bash on PATH from shadowing the real Git Bash. Block-free `if` lines
rem (no `for ( … )` IN-set, no `( )` blocks) keep the literal `(x86)` parens out of
rem every parenthesized construct — a cmd.exe parse hazard. Ambient CHINAMAX_BASH is
rem cleared so it cannot silently steer the shell. tests/test_hooks_registration.py
rem pins this mirror + order.
set "CHINAMAX_BASH="
set "R=%ProgramW6432%"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Git\bin\bash.exe" set "CHINAMAX_BASH=%R%\Git\bin\bash.exe"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Git\usr\bin\bash.exe" set "CHINAMAX_BASH=%R%\Git\usr\bin\bash.exe"
set "R=%ProgramFiles%"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Git\bin\bash.exe" set "CHINAMAX_BASH=%R%\Git\bin\bash.exe"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Git\usr\bin\bash.exe" set "CHINAMAX_BASH=%R%\Git\usr\bin\bash.exe"
set "R=%ProgramFiles(x86)%"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Git\bin\bash.exe" set "CHINAMAX_BASH=%R%\Git\bin\bash.exe"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Git\usr\bin\bash.exe" set "CHINAMAX_BASH=%R%\Git\usr\bin\bash.exe"
set "R=%LOCALAPPDATA%"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Programs\Git\bin\bash.exe" set "CHINAMAX_BASH=%R%\Programs\Git\bin\bash.exe"
if not defined CHINAMAX_BASH if defined R if exist "%R%\Programs\Git\usr\bin\bash.exe" set "CHINAMAX_BASH=%R%\Programs\Git\usr\bin\bash.exe"
if not defined CHINAMAX_BASH where bash >nul 2>nul && set "CHINAMAX_BASH=bash"
if not defined CHINAMAX_BASH echo chinamax: Git Bash not found in the default Git for Windows install roots or on PATH; install Git for Windows from https://git-scm.com/download/win 1>&2
if not defined CHINAMAX_BASH exit /b 127
"%CHINAMAX_BASH%" -lc 'root="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"; cp=$(command -v cygpath); if [ -n "$cp" ]; then root=$(cygpath -u -- "$root"); fi; exec "$root/scripts/%~1"'
