#!/usr/bin/env bash
# Baton judge path with a REAL remote agent: the remote side is an existing Hermes home whose
# default model works (BATON_JUDGE_AGENT_HOME, never ~/.hermes). The model receives the baton,
# fixes the sample project, runs the agreed check (pre_verify blocks an early finish) and returns
# a verified branch; the local side lands it. Everything else is judge/run-local.sh (see its header).
# Usage: BATON_JUDGE_AGENT_HOME=/path/to/hermes-home bash judge/run-agent.sh
# Model calls are billed to that home's provider; a free model keeps the run at $0.
exec env BATON_JUDGE_REMOTE=agent "$(cd "$(dirname "$0")" && pwd)/run-local.sh" "$@"
