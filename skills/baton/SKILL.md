---
name: baton
description: Use when the user wants to hand work to their remote Hermes.
---

# Baton (local usage)

Baton hands the current session and repository to the user's remote Hermes agent (Hermes Cloud, a
VPS or Docker). The remote works in a throwaway worktree, must pass an agreed check, and returns a
branch `baton/<id>`. Landing it never touches the user's working tree, index or stash.

## When to use it

The user says "take over overnight", "finish this on my server", "hand this to the VPS", or wants
to close the laptop while a long fix runs. Do not use it for quick edits you can do here.

## How

- In chat: `/baton <task> [--peer NAME] [--done-when "python -m pytest -q"] [--dry-run]`.
  Always state the check that defines done. Suggest `--dry-run` first when the user is unsure: it
  shows the files, sizes, transcript size and any secret findings, and sends nothing.
- CLI: `hermes baton pass|status|land|list|cancel|doctor`.
  - `hermes baton status [<id>]` reads the remote state from git refs.
  - `hermes baton land <id> [--worktree]` creates the local branch `baton/<id>` (and optionally a
    worktree next to the repo). It refuses if a different `baton/<id>` already exists.
  - `hermes baton pass --resume <id>` restarts an interrupted pass; the same id is the same remote
    run (idempotent), so retrying is safe.
  - `hermes baton doctor --peer NAME` checks reachability, auth and the shared git remote.

## What to tell the user

- Uncommitted changes and untracked (not ignored) files travel; ignored files do not.
- A secret in the outgoing changes **blocks** the pass (it is never masked). Ask the user to remove
  it or add the file to `.gitignore`; there is no override.
- The peer is the one registered with `hermes peer add`; the key is never printed or stored.
- After landing: `git log baton/<id>`, then merge or cherry-pick as the user prefers. Baton never
  merges by itself.
