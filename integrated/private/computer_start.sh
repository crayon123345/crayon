#!/bin/sh
# Fixed progress markers only. Never print environment values or credentials.
umask 077
STATE="$HOME/.config/crayon"
mkdir -p "$STATE" || exit 1
LOG="$STATE/boot-supervisor.log"
exec >>"$LOG" 2>&1
mark() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1"; }
mark 'script_entered'
trap 'rc=$?; mark "script_exit rc=$rc"' EXIT
cd "$(dirname "$0")" || exit 1
mark 'cwd_ready'
printf 'env_bridge_present=%s env_bridge_length=%s env_autostart_present=%s env_autostart_length=%s\n' "${CRAYON_BRIDGE_TOKEN:+yes}" "${#CRAYON_BRIDGE_TOKEN}" "${CRAYON_COMPUTER_AUTOSTART:+yes}" "${#CRAYON_COMPUTER_AUTOSTART}"
mark 'python_path_check'
command -v python3 || exit 1
python3 --version || exit 1
mark 'supervisor_import_attempt'
python3 -c 'import computer_boot; print("supervisor_import_ok", flush=True)' || exit 1
mark 'existing_supervisor_check'
if pgrep -f '^python3 -u computer_boot.py$' >/dev/null; then
    mark 'existing_supervisor_found'
    exit 0
fi
mark 'detached_launch_attempt'
setsid nohup python3 -u computer_boot.py </dev/null >>"$LOG" 2>&1 &
child=$!
mark "detached_launch_pid=$child"
sleep 1
if kill -0 "$child" 2>/dev/null; then
    mark 'detached_process_present'
else
    wait "$child"; rc=$?
    mark "detached_process_exit rc=$rc"
fi
