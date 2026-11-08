#!/usr/bin/env bash
# Same demo as demo/run-local.sh, with the REMOTE in the official Hermes image
# (nousresearch/hermes-agent:v0.23.0, about 1.1 GB to pull the first time; BATON_DEMO_IMAGE overrides).
# The local side stays an isolated Hermes home on this machine. Clean up with demo/teardown.sh.
exec env BATON_DEMO_REMOTE=docker "$(cd "$(dirname "$0")" && pwd)/run-local.sh" "$@"
