#!/usr/bin/env bash
# Runs an agent CLI as the unprivileged $WORKSHEET_AGENT_USER so it cannot read the service user's secrets.
# Usage (as the first words of every command in deploy/oracle/runners.toml): run-as-agent.sh <view-dir> <command...>
set -uo pipefail

agent="${WORKSHEET_AGENT_USER:?WORKSHEET_AGENT_USER is not set}"
view="$1"
shift

restore() { sudo -n chown -R "$(id -un)" "$view"; }
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

sudo -n chown -R "$agent" "$view" || exit 1
# sudo resets the environment, so pass the engine's time zone through explicitly.
if [[ -n "${TZ:-}" ]]; then
    sudo -n -u "$agent" -H -- env -C "$view" TZ="$TZ" "$@"
else
    sudo -n -u "$agent" -H -- env -C "$view" "$@"
fi
exit $?
