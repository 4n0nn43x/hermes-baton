# Baton sample project

`python -m pytest -q` is red: `mean()` divides by `len(values) - 1`. The fix is one character.
Used by `judge/run-local.sh` and by the full-loop test of the plugin.
