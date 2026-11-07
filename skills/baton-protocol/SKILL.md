---
name: baton-protocol
description: Use when a run hands you a baton. Work, check, return.
---

# Baton protocol (remote agent)

You received a **baton**: a coding task handed over from another machine, with its repository
snapshot and a short transcript. The sender is offline. Your job is to return a branch whose agreed
check passes, or an honest failure.

## Steps

1. **Receive.** Call `baton_receive` with the baton id from the run prompt. It returns:
   `worktree` (an absolute path), `task`, `done_when` (the check argv) and `transcript_excerpt`.
   Read the task and the excerpt before touching code.
2. **Work only inside `worktree`.** Never edit other directories, never clone elsewhere, never push
   by hand. Do not run `git checkout`, `git reset --hard` or `git rebase` in the worktree: the
   returned branch must keep the received snapshot as an ancestor.
3. **Check.** Call `baton_check` with the id. It runs the agreed command in the worktree (no shell,
   minimal environment, timeout) and returns `passed`, `exit`, the last 4 KB of output (`tail`) and
   the sha256 of the full log. Fix, then check again, until `passed` is true.
4. **Return.** Call `baton_return` with the id, a short `summary` of what you changed and why, and
   `status: "verified"`. It commits your changes, refuses with `check_stale` if files changed after
   the last green check (run `baton_check` again), and pushes the branch and the result.
5. **Cannot finish?** Call `baton_return` with `status: "failed"` and a summary that states the
   blocker (missing dependency, ambiguous task, check needs network...). A clear failure is a valid
   outcome; a fake success is not.

## Rules

- The check command comes from the sender and is filtered by this machine's `allowed_checks`. If
  `baton_check` answers `check_not_allowed`, return `failed` with that reason; do not run the
  command another way.
- Never print, copy or commit secrets. Do not add credentials to make a test pass.
- Approvals still apply to your other tools. If a command is refused, find another way or fail
  honestly.
- Keep the diff focused on the task. Do not reformat unrelated files.
- Before you end your turn, the last call must be `baton_return`. If you try to finish earlier,
  you will be reminded to run `baton_check`.

## Errors you may see

| code | meaning | what to do |
|---|---|---|
| `baton_cancelled` | the sender cancelled | stop, nothing to return |
| `manifest_invalid` | package tampered or corrupt | stop, report it in your final message |
| `check_failed` / `check_missing` / `check_stale` | no fresh green check | run `baton_check` |
| `already_returned` | result already pushed | nothing more to do |
