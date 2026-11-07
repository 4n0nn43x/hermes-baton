#!/usr/bin/env bash
# Same judge path as judge/run-local.sh, with the REMOTE in the official Hermes image
# (nousresearch/hermes-agent:v0.23.0, about 1.1 GB to pull the first time; BATON_JUDGE_IMAGE overrides).
# The local side stays an isolated Hermes home on this machine. Clean up with judge/teardown.sh.
exec env BATON_JUDGE_REMOTE=docker "$(cd "$(dirname "$0")" && pwd)/run-local.sh" "$@"
