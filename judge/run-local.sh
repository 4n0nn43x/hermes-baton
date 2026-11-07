#!/usr/bin/env bash
# Baton judge path on ONE machine, with TWO real Hermes v0.23.0 instances:
#   "local"  : a Hermes home with the plugin, CLI only (the laptop that passes the baton)
#   "remote" : a Hermes home with the plugin AND a running gateway whose API server listens on
#              127.0.0.1:$BATON_JUDGE_PORT (the agent that receives the baton)
# Code travels through a shared bare git repo, the order through the remote's Runs API (/v1/runs).
#
# Everything lives under judge/.work/ (removed by judge/teardown.sh). Both homes are fresh and
# isolated (own HERMES_HOME, HOME, gateway lock dir and XDG dirs, GATEWAY_MULTIPLEX_PROFILES=false),
# every Hermes process starts from `env -i` (no provider key is inherited), and no model is
# configured: nothing paid can run. Your own ~/.hermes and your own gateway are never touched.
#
# Overrides: HERMES_AGENT_DIR (Hermes checkout, default ~/.hermes/hermes-agent), HERMES_PYTHON
# (python with Hermes' dependencies, default: discovered under ~/.hermes/installs),
# BATON_JUDGE_PORT (default 8652), BATON_JUDGE_UV_CACHE (uv cache kept across runs).
# BATON_JUDGE_REMOTE=docker (what judge/run-docker.sh sets) runs the remote in the official image
# instead (BATON_JUDGE_IMAGE, default nousresearch/hermes-agent:v0.23.0, about 1.1 GB to pull).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
WORK="$HERE/.work"
PORT=${BATON_JUDGE_PORT:-8652}
HERMES_AGENT_DIR=${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}
SYS_PATH=/usr/local/bin:/usr/bin:/bin
GIT_ID=(-c user.name=judge -c user.email=judge@localhost -c commit.gpgsign=false -c init.defaultBranch=main)
FIX_FROM='len(values) - 1'
FIX_TO='len(values)'
MODE=${BATON_JUDGE_REMOTE:-local}
IMAGE=${BATON_JUDGE_IMAGE:-nousresearch/hermes-agent:v0.23.0}
CNAME=baton-judge-remote
if [ "$MODE" = docker ]; then   # paths as the remote sees them, and the host path of its workspace
  R_GIT=/srv/shared.git; R_WS=/opt/data/baton-work; R_WS_HOST="$WORK/remote/data/baton-work"
else
  R_GIT="$WORK/shared.git"; R_WS="$WORK/remote/workspace"; R_WS_HOST="$R_WS"
fi

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }
run() { printf '$ %s\n' "$*"; "$@"; }
die() { printf 'JUDGE PATH FAILED: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------------------------------
step "0. Preflight"
[ -d "$HERMES_AGENT_DIR/hermes_cli" ] || die "no Hermes checkout at $HERMES_AGENT_DIR (set HERMES_AGENT_DIR)"
can_run_hermes() { env -i PATH="$SYS_PATH" PYTHONPATH="$HERMES_AGENT_DIR" PYTHONDONTWRITEBYTECODE=1 \
  "$1" -c 'import hermes_cli.main' >/dev/null 2>&1; }
if [ -z "${HERMES_PYTHON:-}" ]; then
  # The install's current environment first (installs/*/facts.json), then any other candidate.
  current=$(sed -n 's/.*"environment": "\([^"]*\)".*/\1/p' "$HOME"/.hermes/installs/*/facts.json 2>/dev/null | head -n 1)
  for py in ${current:+"$current/bin/python"} "$HOME"/.hermes/installs/*/environments/*/venv/bin/python \
            "$HERMES_AGENT_DIR"/venv/bin/python "$HERMES_AGENT_DIR"/.venv/bin/python; do
    if [ -x "$py" ] && can_run_hermes "$py"; then HERMES_PYTHON=$py; break; fi
  done
fi
[ -n "${HERMES_PYTHON:-}" ] && can_run_hermes "$HERMES_PYTHON" \
  || die "no python able to import Hermes found (set HERMES_PYTHON)"
note "Hermes: $(git -C "$HERMES_AGENT_DIR" describe --tags 2>/dev/null || echo unknown) at $HERMES_AGENT_DIR"
note "python: $HERMES_PYTHON"
for tool in git curl ss setsid sha256sum; do command -v "$tool" >/dev/null || die "missing tool: $tool"; done
if [ "$MODE" = docker ]; then
  command -v docker >/dev/null || die "docker is not installed"
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    note "pulling $IMAGE (about 1.1 GB, once)"; docker pull -q "$IMAGE" >/dev/null || die "cannot pull $IMAGE"
  fi
  note "remote: docker image $IMAGE"
fi
if [ -e "$WORK" ]; then note "previous run found: running teardown first"; "$HERE/teardown.sh"; fi
if ss -ltnH "sport = :$PORT" | grep -q .; then die "port $PORT is busy (set BATON_JUDGE_PORT to 8653..8655)"; fi
note "port $PORT is free"

mkdir -p "$WORK/bin"
umask 077
KEY=$("$HERMES_PYTHON" -c 'import secrets; print(secrets.token_hex(24))')   # throwaway test key

# One launcher per side: env -i, private HOME / HERMES_HOME / lock dir / XDG dirs.
for side in local remote; do
  mkdir -p "$WORK/$side/home" "$WORK/$side/hermes" "$WORK/$side/locks" "$WORK/$side/xdg/state" \
           "$WORK/$side/xdg/config" "$WORK/$side/xdg/cache" "$WORK/$side/xdg/data"
  cat > "$WORK/bin/hermes-$side" <<EOF
#!/bin/sh
# Hermes in the isolated judge home "$side" (written by judge/run-local.sh, removed by teardown.sh).
exec env -i HOME="$WORK/$side/home" USER="\${USER:-judge}" LANG=C.UTF-8 TERM=dumb NO_COLOR=1 \\
  PATH="$WORK/checkenv/bin:$SYS_PATH" \\
  HERMES_HOME="$WORK/$side/hermes" HERMES_GATEWAY_LOCK_DIR="$WORK/$side/locks" \\
  XDG_STATE_HOME="$WORK/$side/xdg/state" XDG_CONFIG_HOME="$WORK/$side/xdg/config" \\
  XDG_CACHE_HOME="$WORK/$side/xdg/cache" XDG_DATA_HOME="$WORK/$side/xdg/data" \\
  GATEWAY_MULTIPLEX_PROFILES=false HERMES_DISABLE_LAZY_INSTALLS=1 PYTHONDONTWRITEBYTECODE=1 \\
  PYTHONPATH="$HERMES_AGENT_DIR" "$HERMES_PYTHON" -m hermes_cli.main "\$@"
EOF
  chmod 700 "$WORK/bin/hermes-$side"
done
if [ "$MODE" = docker ]; then
  cat > "$WORK/bin/hermes-remote" <<EOF
#!/bin/sh
# Hermes inside the judge container $CNAME (written by judge/run-local.sh in docker mode).
exec docker exec -i -u hermes -e PATH=/opt/data/checkenv/bin:/opt/hermes/bin:/opt/hermes/.venv/bin:$SYS_PATH \\
  $CNAME hermes "\$@"
EOF
fi
HL="$WORK/bin/hermes-local"
HR="$WORK/bin/hermes-remote"
umask 022

# The agreed check is `python -m pytest -q`: the remote needs `python` with pytest on its PATH.
if env -i PATH="$SYS_PATH" python -m pytest --version >/dev/null 2>&1; then
  mkdir -p "$WORK/checkenv/bin"   # system python already has pytest
else
  note "creating judge/.work/checkenv (venv with pytest) for the check command"
  if command -v uv >/dev/null; then
    uv venv --quiet "$WORK/checkenv" >/dev/null 2>&1 && uv pip install --quiet --python "$WORK/checkenv/bin/python" pytest
  else
    python3 -m venv "$WORK/checkenv" && "$WORK/checkenv/bin/python" -m pip install --quiet pytest
  fi
fi
env -i PATH="$WORK/checkenv/bin:$SYS_PATH" python -m pytest --version >/dev/null 2>&1 \
  || die "cannot provide python -m pytest for the check"

# ---------------------------------------------------------------------------------------------------
step "1. Shared bare repo, plugin snapshot, sample project"
run git init --quiet --bare "$WORK/shared.git"
# Throwaway git snapshot of the plugin working tree (tracked + untracked, .gitignore respected).
SNAP="$WORK/plugin/baton"
mkdir -p "$SNAP"
(cd "$REPO" && git ls-files -z --cached --others --exclude-standard) | while IFS= read -r -d '' f; do
  if [ -f "$REPO/$f" ]; then mkdir -p "$SNAP/$(dirname "$f")"; cp -p "$REPO/$f" "$SNAP/$f"; fi
done
git -C "$SNAP" "${GIT_ID[@]}" init --quiet
git -C "$SNAP" add -A
git -C "$SNAP" "${GIT_ID[@]}" commit --quiet -m "judge snapshot of the plugin working tree"
note "plugin snapshot: $(git -C "$SNAP" ls-files | wc -l) files at $(git -C "$SNAP" rev-parse --short HEAD)"

PROJ="$WORK/project"
cp -R "$HERE/sample" "$PROJ"
git -C "$PROJ" "${GIT_ID[@]}" init --quiet
git -C "$PROJ" add -A
git -C "$PROJ" "${GIT_ID[@]}" commit --quiet -m "sample: calculator with a red test"
# Make the local repo look like real work in progress: a stash, a staged and an unstaged change,
# an untracked file. All of it must survive the whole baton untouched.
printf '\n# experiment kept in the stash\n' >> "$PROJ/calc.py"
git -C "$PROJ" "${GIT_ID[@]}" stash push --quiet -m "wip: experiment"
printf '\nLocal note: staged, not committed.\n' >> "$PROJ/README.md"
git -C "$PROJ" add README.md
cat >> "$PROJ/tests/test_calc.py" <<'EOF'


def test_mean_single():  # unstaged local change: travels with the baton
    assert mean([5]) == 5
EOF
printf 'todo: hand the parser bug to the remote tonight\n' > "$PROJ/NOTES.md"
run git -C "$PROJ" --no-optional-locks status --short
note "local test run (red on purpose):"
(cd "$PROJ" && env -i PATH="$WORK/checkenv/bin:$SYS_PATH" PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider 2>&1 | tail -3) || true

# ---------------------------------------------------------------------------------------------------
if [ "$MODE" = docker ]; then
  step "1b. Start the remote container (gateway + API server on 127.0.0.1:$PORT)"
  mkdir -p "$WORK/remote/data"
  umask 077   # the throwaway key goes through an env file, never on a command line
  printf 'API_SERVER_ENABLED=true\nAPI_SERVER_HOST=0.0.0.0\nAPI_SERVER_KEY=%s\nHERMES_UID=%s\nHERMES_GID=%s\n' \
    "$KEY" "$(id -u)" "$(id -g)" > "$WORK/remote/docker.env"
  umask 022
  run docker run -d --name "$CNAME" --env-file "$WORK/remote/docker.env" -v "$WORK/remote/data:/opt/data" \
    -v "$WORK/shared.git:/srv/shared.git" -v "$WORK/plugin:/srv/plugin:ro" -p "127.0.0.1:$PORT:8642" \
    "$IMAGE" gateway run >/dev/null
  docker exec -u hermes "$CNAME" sh -c 'python -m pytest --version >/dev/null 2>&1 ||
    { python -m venv /opt/data/checkenv && /opt/data/checkenv/bin/python -m pip install -q pytest; }' \
    || die "cannot provide python -m pytest inside the container"
fi

step "2. Install the plugin in both homes (file:// snapshot) and configure them"
# A fresh Hermes home prepares its own dependency environment when a plugin is enabled (a full
# download the first time). Both judge homes share one uv cache, so only the first one downloads;
# BATON_JUDGE_UV_CACHE=<dir> keeps that cache across runs (teardown.sh never deletes it).
UV_SHARED=${BATON_JUDGE_UV_CACHE:-$WORK/uv-cache}
mkdir -p "$UV_SHARED"
for side in local remote; do
  mkdir -p "$WORK/$side/hermes/cache"
  ln -sfn "$UV_SHARED" "$WORK/$side/hermes/cache/uv"
done
plugin_enabled() { "$1" plugins list --enabled --plain 2>/dev/null | grep -qw baton; }
for side in local remote; do
  H="$WORK/bin/hermes-$side"
  t0=$SECONDS
  for attempt in 1 2 3; do
    force=(); [ "$attempt" = 1 ] || force=(--force)
    url="file://$SNAP"; [ "$MODE/$side" = docker/remote ] && url=file:///srv/plugin/baton
    run "$H" plugins install "$url" --enable --yes-deps "${force[@]}" > "$WORK/$side/install.log" 2>&1 || true
    plugin_enabled "$H" && break
    note "attempt $attempt: plugin not enabled in the $side home (last lines below), retrying"
    tail -n 4 "$WORK/$side/install.log" | sed 's/^/     /'
  done
  plugin_enabled "$H" || { tail -n 40 "$WORK/$side/install.log"; die "plugin not enabled in the $side home"; }
  note "$side home: plugin baton installed from file:// and enabled in $((SECONDS - t0))s"
  "$H" config set plugins.entries.baton.settings.role "$side" >/dev/null
  git_url="$WORK/shared.git"; [ "$side" = remote ] && git_url="$R_GIT"
  "$H" config set plugins.entries.baton.settings.remote_git "$git_url" >/dev/null
done
"$HR" config set plugins.entries.baton.settings.workspace "$R_WS" >/dev/null
note "settings: local role=local, remote role=remote, both on the shared bare repo judge/.work/shared.git"
if [ "$MODE" = local ]; then
  # The remote's API server: key, host and port in its own .env (throwaway key, never printed).
  umask 077
  printf 'API_SERVER_KEY=%s\nAPI_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT=%s\n' "$KEY" "$PORT" >> "$WORK/remote/hermes/.env"
  umask 022
  # A fresh home's environment has no extras; the API server needs aiohttp (the `messaging` extra).
  t0=$SECONDS
  "$HR" pm install --extra messaging > "$WORK/remote/pm-extra.log" 2>&1 \
    || { tail -n 20 "$WORK/remote/pm-extra.log"; die "cannot add the messaging extra to the remote home"; }
  note "remote home: hermes pm install --extra messaging (API server dependencies) in $((SECONDS - t0))s"
fi
# Register the remote as a peer on the local side, exactly like a user would (`hermes peer add`).
"$HL" peer add judge --url "http://127.0.0.1:$PORT" --key "$KEY" >/dev/null
note "local: hermes peer add judge --url http://127.0.0.1:$PORT --key <generated, not shown>"
grep -q "^HERMES_PEER_JUDGE_KEY=" "$WORK/local/hermes/.env" || die "peer key not stored in the local home"

# ---------------------------------------------------------------------------------------------------
GW_PID=""
stop_gateway() {
  if [ "$MODE" = docker ] && docker ps -aq --filter "name=^$CNAME\$" | grep -q .; then
    docker logs "$CNAME" > "$WORK/remote/gateway.log" 2>&1 || true
    docker stop -t 30 "$CNAME" >/dev/null 2>&1 || true   # SIGTERM, then SIGKILL after 30 s
    docker rm "$CNAME" >/dev/null 2>&1 || true
  fi
  [ -n "$GW_PID" ] || return 0
  if kill -0 "$GW_PID" 2>/dev/null; then
    kill -TERM -- "-$GW_PID" 2>/dev/null || kill -TERM "$GW_PID" 2>/dev/null || true
    for _ in $(seq 1 60); do kill -0 "$GW_PID" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$GW_PID" 2>/dev/null; then kill -KILL -- "-$GW_PID" 2>/dev/null || true; fi
  fi
  GW_PID=""
  rm -f "$WORK/remote/gateway.pid"
}
trap stop_gateway EXIT
trap 'exit 130' INT TERM

step "3. Start the remote gateway (API server on 127.0.0.1:$PORT)"
if [ "$MODE" = docker ]; then
  run docker restart "$CNAME" >/dev/null   # the gateway loads the freshly installed plugin
else
  setsid "$HR" gateway run -v > "$WORK/remote/gateway.log" 2>&1 < /dev/null &
  GW_PID=$!
  echo "$GW_PID" > "$WORK/remote/gateway.pid"
fi
for _ in $(seq 1 240); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  if [ -n "$GW_PID" ] && ! kill -0 "$GW_PID" 2>/dev/null; then
    tail -n 40 "$WORK/remote/gateway.log"; die "remote gateway exited"
  fi
  sleep 0.5
done
if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  [ "$MODE" = docker ] && docker logs "$CNAME" > "$WORK/remote/gateway.log" 2>&1
  tail -n 40 "$WORK/remote/gateway.log"; die "API server not up"
fi
who="container $CNAME"; [ -n "$GW_PID" ] && who="pid $GW_PID"
note "gateway $who; $(ss -ltnpH "sport = :$PORT" | awk '{print $4}') listening"
note "GET /health without key -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health")"
note "GET /v1/capabilities without key -> HTTP $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/v1/capabilities")"
run "$HL" baton doctor --peer judge

# ---------------------------------------------------------------------------------------------------
fingerprint() {  # everything Baton promises not to touch in the local repo
  ( cd "$PROJ"
    echo "HEAD   $(git rev-parse HEAD) ($(git symbolic-ref -q HEAD))"
    echo "index  $(sha256sum .git/index | cut -d' ' -f1)"
    echo "stash  $(git rev-parse -q --verify refs/stash) $(git stash list | tr '\n' ' ')"
    echo "status $(git --no-optional-locks status --porcelain=v1 --untracked-files=all | sha256sum | cut -c1-16)"
    echo "tree   $(find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)"
  )
}
fingerprint > "$WORK/fingerprint.before"

step "4. GREEN path: pass a baton from the local home (real Runs API trigger)"
TASK="Fix mean() in calc.py so the test suite passes"
(cd "$PROJ" && run "$HL" baton pass "$TASK" --peer judge --done-when "python -m pytest -q") | tee "$WORK/pass1.out"
ID=$(sed -n 's/^baton \([0-9A-HJKMNP-TV-Z]\{26\}\): .*/\1/p' "$WORK/pass1.out" | head -n 1)
[ -n "$ID" ] || die "no baton id in the pass output"
grep -q "TRIGGERED" "$WORK/pass1.out" || die "the pass did not reach TRIGGERED"
RUN_ID=$(sed -n 's/^  remote run: \([^ ]*\).*/\1/p' "$WORK/pass1.out")
note "on the shared remote:"
git -C "$WORK/shared.git" for-each-ref --format='   %(refname)' "refs/baton/$ID"

note "resend after a simulated network cut: same Idempotency-Key = same remote run"
BATON_KEY="$KEY" "$HERMES_PYTHON" - "$WORK/plugin" "$PORT" "$ID" "$TASK" "$RUN_ID" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from baton.runs import RunsClient, build_prompt
port, bid, task, first = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
c = RunsClient(f"http://127.0.0.1:{port}", os.environ["BATON_KEY"])
r = c.start_run(build_prompt(bid, task, ["python", "-m", "pytest", "-q"]), idempotency_key=bid,
                session_id=f"baton-{bid.lower()}")
print(f"   POST /v1/runs again -> run_id {r['run_id']} replayed={r['replayed']} (first run {first})")
if r["run_id"] != first or not r["replayed"]:
    sys.exit("idempotent replay not observed")
EOF
note "remote run as seen through the Runs API (no model is configured in the judge homes):"
run "$HL" baton status "$ID"

step "5. Remote: receive, simulated agent fix, check, return (hermes baton work, no model)"
run "$HR" baton work "$ID" --receive-only
WT="$R_WS_HOST/baton-$ID"
[ -d "$WT" ] || die "no remote worktree at $WT"
note "check before any fix (expected RED):"
rc=0; run "$HR" baton work "$ID" --check-only | tee "$WORK/check1.out" || rc=$?
[ "$rc" = 3 ] || die "expected a red check (exit 3), got $rc"
grep -q "failed" "$WORK/check1.out" || die "the check did not run the tests (see its output above)"
note "simulated agent fix (what the remote model would write): $FIX_FROM -> $FIX_TO in calc.py"
sed -i "s/$FIX_FROM/$FIX_TO/" "$WT/calc.py"
git -C "$WT" --no-pager diff --stat
run "$HR" baton work "$ID" --summary "mean() divided by len-1; divide by len"

step "6. Local: status, land, and proof that the local repo was not touched"
run "$HL" baton status "$ID"
run "$HL" baton land "$ID"
run git -C "$PROJ" --no-optional-locks status --short
run git -C "$PROJ" --no-pager log --oneline --decorate "baton/$ID"
run git -C "$PROJ" --no-pager diff --stat HEAD "baton/$ID"
note "the landed branch passes the check locally too:"
git -C "$PROJ" worktree add --quiet "$WORK/landed" "baton/$ID"
(cd "$WORK/landed" && env -i PATH="$WORK/checkenv/bin:$SYS_PATH" PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider 2>&1 | tail -1)
git -C "$PROJ" worktree remove --force "$WORK/landed"

step "7. RED path: nobody fixes it, the remote returns FAILED and land refuses"
(cd "$PROJ" && run "$HL" baton pass "Make the suite pass (red path: no fix is applied)" --peer judge \
   --done-when "python -m pytest -q" --no-trigger) | tee "$WORK/pass2.out"
ID2=$(sed -n 's/^baton \([0-9A-HJKMNP-TV-Z]\{26\}\): .*/\1/p' "$WORK/pass2.out" | head -n 1)
[ -n "$ID2" ] || die "no id for the red baton"
rc=0; run "$HR" baton work "$ID2" || rc=$?
[ "$rc" = 3 ] || die "expected work to return FAILED (exit 3), got $rc"
rc=0; run "$HL" baton land "$ID2" || rc=$?
[ "$rc" = 1 ] || die "land of a failed baton must be refused (exit 1), got $rc"
git -C "$PROJ" show-ref --verify -q "refs/heads/baton/$ID2" && die "a failed baton was landed"
note "no local branch baton/$ID2 was created"

step "8. ALLOWLIST: the sender cannot make the remote run an arbitrary command"
(cd "$PROJ" && run "$HL" baton pass "Run my command" --peer judge --done-when "sh -c 'echo pwned'" --no-trigger) \
  | tee "$WORK/pass3.out"
ID3=$(sed -n 's/^baton \([0-9A-HJKMNP-TV-Z]\{26\}\): .*/\1/p' "$WORK/pass3.out" | head -n 1)
rc=0; run "$HR" baton work "$ID3" || rc=$?
[ "$rc" = 1 ] || die "a non-allowlisted check must be refused (exit 1), got $rc"

step "9. Byte-level comparison of the local repo, before and after"
fingerprint > "$WORK/fingerprint.after"
sed 's/^/   /' "$WORK/fingerprint.after"
if diff -u "$WORK/fingerprint.before" "$WORK/fingerprint.after"; then
  note "HEAD, index, stash, git status and every file of the working tree: IDENTICAL"
else
  die "the local repo changed during the baton"
fi
run git -C "$PROJ" branch --list "baton/*"
run "$HL" baton list

step "10. Stop the remote gateway (SIGTERM)"
stop_gateway
if ss -ltnH "sport = :$PORT" | grep -q .; then die "port $PORT still open"; fi
note "port $PORT closed, gateway stopped; last lines of its log:"
grep -E "Exiting|disconnected|shutdown" "$WORK/remote/gateway.log" | tail -n 3 | sed 's/^/   /' || true

printf '\nJUDGE PATH OK: baton %s landed verified, red baton %s refused, allowlist enforced, local repo untouched.\n' "$ID" "$ID2"
printf 'Inspect: judge/.work/project (git log baton/%s), judge/.work/bin/hermes-{local,remote}. Clean up: judge/teardown.sh\n' "$ID"
