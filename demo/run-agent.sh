#!/usr/bin/env bash
# Baton demo with a REAL remote agent: the remote side is an existing Hermes home whose
# default model works (BATON_DEMO_AGENT_HOME, never ~/.hermes). The model receives the baton,
# fixes the sample project, runs the agreed check (pre_verify blocks an early finish) and returns
# a verified branch; the local side lands it. Everything else is demo/run-local.sh (see its header).
# Usage: BATON_DEMO_AGENT_HOME=/path/to/hermes-home bash demo/run-agent.sh
# Model calls are billed to that home's provider; a free model keeps the run at $0.
exec env BATON_DEMO_REMOTE=agent "$(cd "$(dirname "$0")" && pwd)/run-local.sh" "$@"
