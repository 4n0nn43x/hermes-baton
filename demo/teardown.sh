#!/usr/bin/env bash
# Remove what demo/run-local.sh and demo/run-docker.sh created, and only that:
#   - the demo gateway process (pid recorded in demo/.work/remote/gateway.pid, checked to really be
#     a Hermes process of the demo home before it is signalled),
#   - the docker container named baton-demo-remote (run-docker.sh), if any,
#   - the demo/.work/ directory.
# The pulled docker image, your ~/.hermes and your own gateway are left alone.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
WORK="$HERE/.work"

pidfile="$WORK/remote/gateway.pid"
# In agent mode the gateway runs on the agent home named in remote/hermes_home.
expected_home="$WORK/remote/hermes"
[ -f "$WORK/remote/hermes_home" ] && expected_home=$(cat "$WORK/remote/hermes_home")
if [ -f "$pidfile" ]; then
  pid=$(cat "$pidfile")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null \
     && tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qxF "HERMES_HOME=$expected_home"; then
    echo "teardown: stopping demo gateway pid $pid (SIGTERM)"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL -- "-$pid" 2>/dev/null || true; fi
  fi
fi

if command -v docker >/dev/null 2>&1 && docker ps -aq --filter name='^baton-demo-remote$' 2>/dev/null | grep -q .; then
  echo "teardown: removing docker container baton-demo-remote"
  docker rm -f baton-demo-remote >/dev/null
fi

if [ -e "$WORK" ]; then
  # Files written by the container belong to its user: let docker delete what we cannot.
  if ! rm -rf "$WORK" 2>/dev/null && command -v docker >/dev/null 2>&1; then
    docker run --rm -v "$HERE:/demo" -w /demo --entrypoint /bin/sh \
      "${BATON_DEMO_IMAGE:-nousresearch/hermes-agent:v0.23.0}" -c 'rm -rf .work' >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
  echo "teardown: removed demo/.work"
else
  echo "teardown: nothing to remove"
fi
