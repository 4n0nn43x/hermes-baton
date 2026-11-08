# Baton (SPEC)

Authority order for this repo: SPEC > tests > code. Research behind every decision:
`../_research/R3-baton.md` (transports, Runs API, remote side, git proof, file:line at v0.23.0),
`../_research/R1-manuel-plugin.md` (packaging, verified skeleton, isolated test environments),
`../_research/analyse-2026-11-06/5-soumission-revue.md` (scanner rules, review reasons).
Target: Hermes Agent v0.23.0 (`requires_hermes: ">=0.23.0"`), Python >= 3.11.

## 1. Product in one paragraph

`/baton "fix the parser, done when pytest passes"` hands the current session and its repository to
your remote Hermes agent (Hermes Cloud, a VPS, or a Docker container). You close the laptop. The
remote agent works in a throwaway worktree, cannot declare victory until the agreed check is green,
and returns a tested branch. The next morning `hermes baton land <id>` fetches `baton/<id>` next to
your work: your working tree, index and stash are untouched. Principle: **code travels by git, the
order travels by the Hermes Runs API, state travels by git refs.** No private Hermes imports, no
Desktop dependency, every step resumable after a network cut (same id = same run).

## 2. Repository layout (repo root IS the plugin directory)

```
plugin.yaml, pyproject.toml, __init__.py (register: tools, hook, /baton, CLI, skills)   (BT-C)
ids.py          ULID-like sortable ids                                                 (BT-A)
gitutil.py      run_git (argv only, sanitized env, timeout), small helpers              (BT-A)
capture.py      ghost commit of the working tree without touching it                    (BT-A)
secretscan.py   blocking secret scan of the outgoing tree diff and file names           (BT-A)
manifest.py     BATON.json v1 build/verify, SHA256SUMS                                   (BT-A)
transcript.py   session export via `hermes sessions export --redact` + extra pass, cap   (BT-A)
runs.py         HTTP client for the remote gateway API server (urllib)                   (BT-A)
peers.py        resolve a peer: URL + key (reuse `hermes peer add` records)              (BT-A)
store.py        local baton records and the state machine                                (BT-A)
local.py        pass / status / land / list / cancel orchestration                       (BT-A)
remote.py       receive / check / return / work + pre_verify decision                    (BT-B)
skills/baton-protocol/SKILL.md   instructions for the REMOTE agent                       (BT-B)
skills/baton/SKILL.md            usage skill for the LOCAL agent                          (BT-B)
judge/          run-local.sh, run-docker.sh, run-agent.sh, sample/ (project with a red test), teardown.sh (BT-B)
tests/          pytest; core tests never import Hermes
README.md       catalog page
```

Every module except `__init__.py` must not import Hermes modules. Relative imports only. Hermes
imports the plugin as package `hermes_plugins.baton`; tests load it as `baton_plugin`.

## 3. Identifiers, refs, branches

- id: 26-char Crockford base32 ULID (time sortable), generated once per baton.
- On the shared git remote:
  - `refs/baton/<id>/in`: the outgoing commit (see 4).
  - `refs/baton/<id>/status`: commit whose tree has `status.json` (latest state written by whichever side moved it).
  - `refs/heads/baton/<id>`: the returned branch (remote agent's commits on top of the `in` commit's project parent).
  - `refs/baton/<id>/result`: commit whose tree has `result.json`.
- Local landing: `refs/heads/baton/<id>` in the user's repo (never checked out automatically).

## 4. Capture and package (BT-A)

`capture.ghost_commit(repo: Path) -> CaptureResult(project_commit, files, untracked_included)`:
copy the real index to a temp file; `GIT_INDEX_FILE=<tmp> git add -A` (respects .gitignore);
`git write-tree`; `git commit-tree -p HEAD` (or no parent on an unborn branch) with a fixed message
`baton: snapshot <id>`. HEAD, the real index, the working tree and the stash list must be byte-identical
before and after (tested). Submodules: refuse with `submodules_unsupported`. Not a git repo: `not_a_repo`.

Package commit = child of `project_commit` adding only `.baton/<id>/BATON.json`,
`.baton/<id>/transcript.md`, `.baton/<id>/SHA256SUMS` (tree built with a temp index; the user's tree is
never written). This child is pushed as `refs/baton/<id>/in`.

`BATON.json` (schema 1):
```json
{"schema": 1, "id": "...", "created_at": "RFC3339 UTC", "peer": "name",
 "repo": {"name": "...", "head": "<sha|null>", "branch": "<name|null>", "project_commit": "<sha>"},
 "task": "free text", "done_when": {"argv": ["python", "-m", "pytest", "-q"], "timeout_s": 900, "expect_exit": 0},
 "transcript": {"path": ".baton/<id>/transcript.md", "bytes": 0, "sha256": "..."},
 "redaction": {"cli_redact": true, "extra_hits": 0},
 "return": {"branch": "baton/<id>", "result_ref": "refs/baton/<id>/result"},
 "ttl_hours": 24}
```
`manifest.verify(tree_files) -> list[str]` returns problems (missing file, sha mismatch, schema).

Secret scan (`secretscan.scan(repo, project_commit) -> list[Finding]`), BLOCKING (never masking:
masking secrets in code breaks code): forbidden names (`.env`, `.env.*` except `.env.example`,
`auth.json`, `id_rsa`, `id_ed25519`, `*.pem`, `*.p12`, `credentials.json`); content patterns over
files changed or added versus HEAD plus untracked included files: private key blocks, `sk-` style API
keys (OpenAI/Anthropic/OpenRouter forms), `ghp_`/`github_pat_`, `AKIA[0-9A-Z]{16}`, `xox[abp]-`,
Telegram bot tokens `\d{8,10}:[A-Za-z0-9_-]{35}`, generic `(api|secret|token|password)\s*[:=]\s*['"][^'"]{16,}`
with entropy >= 3.5. Findings report file, line, rule, and a masked excerpt. `--allow-secret <path>`
is NOT offered.

Transcript (`transcript.export(session_id, hermes_argv) -> TranscriptResult(text, bytes, extra_hits)`):
subprocess `[*hermes_argv, "sessions", "export", <tmpfile>, "--session-id", id, "--format", "md", "--redact"]`
(hermes_argv defaults to `[sys.executable, "-m", "hermes_cli.main"]`); then an extra regex pass with
the secret patterns above (replace with `[REDACTED]`, count hits); keep the task, the last 20
user/assistant turns, tool outputs cut to 2 KB each; cap 64 KB. No session id (CLI pass): transcript
is just the task text.

## 5. Transport (BT-A)

Peers (`peers.resolve(name, config) -> Peer(name, url, key)`): read the `hermes peer add` record
(`bot_peers` in config.yaml, key `HERMES_PEER_<NAME>_KEY` from the environment/secret scope; verify
the exact structure in `hermes_cli/subcommands/peer.py` at v0.23.0) with a fallback to plugin settings
`peers: {name: {url, key_env}}`. Never log or store the key.

Runs client (`runs.RunsClient(url, key, timeout=30)`), urllib only:
- `capabilities() -> dict` (GET /v1/capabilities; require `runs_idempotency`).
- `create_session(title) -> session_id` (verify the exact endpoint and body in
  `gateway/platforms/api_server.py` and `website/docs/user-guide/features/api-server.md`).
- `start_run(prompt, idempotency_key, session_id=None) -> {"run_id", "replayed": bool}` (POST /v1/runs,
  header `Idempotency-Key`; HTTP 202 + `Idempotency-Replayed: true` means replay).
- `get_run(run_id) -> dict`, `resolve_unknown(run_id)`.
- Errors mapped to `BatonError(code)`: `peer_unreachable`, `auth_failed`, `idempotency_conflict`,
  `run_rejected`, `bad_response`. Bearer key sent only to the configured origin (no redirects across
  origins).

Run prompt (fixed template, English): tells the remote agent it received baton `<id>`, that it must
load the `baton:baton-protocol` skill, call `baton_receive`, work only inside the returned worktree,
call `baton_check` until green, and finish with `baton_return` (status failed with a reason if it
cannot). Contains the task text, never secrets.

Fallback mailbox mode (bonus, after MVP): no API reachable; the remote polls `refs/baton/*/in`
with `hermes baton work --pending` from a user-created cron job.

## 6. Local commands (BT-A logic, BT-C wiring)

`local.pass_baton(repo, task, peer, done_when_argv, remote_git, session_id=None, trigger=True, dry_run=False) -> BatonRecord`
steps, each recorded so `resume` restarts at the first undone step: PREPARED (capture, scan, transcript,
manifest, package commit) -> PUSHED (push `refs/baton/<id>/in` and status) -> TRIGGERED (create session +
start run with Idempotency-Key = id; skipped with `trigger=False`) . `dry_run` stops after PREPARED and
prints what would be sent (files, sizes, transcript bytes, secret findings).
`local.status(id|None)`: fetch status/result refs from the remote, print state; `local.land(id, worktree=False)`:
fetch `refs/heads/baton/<id>` into the user's repo, verify `result.json.head` equals the fetched sha and
that `project_commit` is an ancestor; refuse if the local branch exists with another sha (`branch_diverged`),
no-op if identical; optional `git worktree add ../<repo>-baton-<id>`. Never touches HEAD/index/working
tree (tested). `local.cancel(id)` writes status CANCELLED. `local.list()`.

Local states: PREPARED, PUSHED, TRIGGERED, then mirrored from remote: RECEIVED, RUNNING, VERIFIED,
RETURNED, FAILED, LANDED, CANCELLED, EXPIRED. Store: JSON records under the plugin data dir (path
injected by the glue; default `$HERMES_HOME/plugin-data/baton/` via `hermes_constants.get_hermes_home()`
resolved ONLY in `__init__.py`).

## 7. Remote side (BT-B)

Settings (read at call time): `role` (`local`, `remote`, `both`; default `both`), `remote_git` (URL or
path of the shared bare repo), `workspace` (dir for worktrees; default `<plugin data>/work`),
`allowed_checks` (argv a baton's check must match; default `[["python","-m","pytest"],["pytest"],["npm","test"],["make","test"],["cargo","test"],["go","test"]]`),
`check_timeout_s` (900), `sandbox` (`auto` or `off`; default `auto`).

Check policy (the remote owns it, the manifest cannot widen it): `done_when.argv` must equal an
`allowed_checks` entry followed only by safe flags (`-q -qq -v -vv -x -s --quiet --verbose --exitfirst`,
and `-p no:<plugin>` pairs) and relative paths that stay inside the worktree once symlinks are resolved;
anything else (`--basetemp=/x`, `-c file`, `-p plugin`, an absolute path, `make --eval=...`) is refused
with `check_not_allowed` before anything runs. The check gets a fresh, empty, throwaway HOME (XDG_*
unset so their defaults land in it), no HERMES_* variable and no provider key. With `sandbox: auto`
and a working bubblewrap, the check runs under `bwrap`: the Hermes homes (`~/.hermes`, `$HERMES_HOME`)
are masked by empty tmpfs, the workspace stays bound, and every process of the check dies with it
(own pid namespace); without bubblewrap (or with `sandbox: off`) it runs unsandboxed. The check result
says which in its `sandbox` field (`bwrap`, or `off: <reason>`).

Refusals with their own codes: `unsafe_snapshot` when the snapshot holds symlinks that resolve outside
the repository (refused at send by the capture, and again at receive, which removes the worktree);
`baton_cancelled` when receive, check or return runs on a baton whose sender cancelled it (the
CANCELLED status is never overwritten).

Tools (always registered; `check_fn` hides them when role is `local`):
- `baton_receive(id)`: fetch `refs/baton/<id>/in` from `remote_git` into a cache clone under the
  workspace, verify the manifest (SHA256SUMS, schema), create worktree `<workspace>/baton-<id>` on new
  branch `baton/<id>` at `project_commit` (the package files are NOT in the worktree), write status
  RECEIVED then RUNNING. Idempotent (same sha -> same worktree). Returns worktree path, task, done_when,
  transcript excerpt.
- `baton_check(id)`: run `done_when.argv` with cwd = worktree, no shell, minimal env (PATH, HOME,
  LANG, plus a fixed allowlist; no provider keys), timeout; refuse if argv does not start with one of
  `allowed_checks` (`check_not_allowed`). Returns exit code, pass/fail, last 4 KB of output, sha256 of
  the full log. Records the tree hash it checked.
- `baton_return(id, summary, status="verified"|"failed")`: commits any changes in the worktree
  (message `baton <id>: <summary first line>`), refuses `verified` unless the last green `baton_check`
  matches the current tree hash (`check_stale`); pushes `refs/heads/baton/<id>`, `refs/baton/<id>/result`
  (`result.json`: status, check {argv, exit, log_sha256, tail}, head, summary, finished_at) and status
  RETURNED or FAILED. Idempotent.
- `pre_verify` hook: if the session is working on a baton (state RUNNING recorded for this session,
  or a baton worktree among changed paths) and there is no green check for the current tree, return
  `{"action": "continue", "message": "Run baton_check for baton <id>; finish only when it passes, or call baton_return with status failed and the reason."}`;
  otherwise None. Bounded by Hermes (`agent.max_verify_nudges`).
- `remote.work(id)` (CLI `hermes baton work <id> [--no-agent]`): deterministic path used by judges and
  CI: receive -> check -> return (verified or failed) without any model.

## 8. Hermes wiring (BT-C)

`/baton <task> [--peer NAME] [--done-when "argv string"] [--dry-run]` (sync handler; gets
`session_id` from the dispatcher kwargs when declared; uses `ctx.session_cwd()` as the repo).
CLI `hermes baton pass|status|land|list|cancel|work|doctor` via `ctx.register_cli_command`.
`hermes baton doctor --peer NAME` checks reachability, auth, capabilities, git remote access.
Skills registered with `ctx.register_skill`. Extra redaction patterns via `ctx.register_redaction_patterns`.
Never writes Hermes config, never changes approvals, never uses yolo.

## 9. Judge path

`judge/run-local.sh`: creates `judge/.work/` with a bare repo, a sample project with a red test, a
"remote" isolated Hermes home (outside `~/.hermes`, private `HERMES_GATEWAY_LOCK_DIR` and
`XDG_STATE_HOME`, `GATEWAY_MULTIPLEX_PROFILES=false`) whose gateway runs the API server on
127.0.0.1:8652 with a random key, installs the plugin in both homes from the local checkout
(`file://...`), passes a baton with `--done-when "python -m pytest -q"`, runs `hermes baton work <id>`
in the remote home (no model), lands, and prints `git status` (unchanged) and `git log baton/<id>`.
`judge/run-docker.sh`: same with the remote in `nousresearch/hermes-agent:v0.23.0` (warn: 1.1 GB pull).
`judge/teardown.sh` removes everything it created. `judge/run-agent.sh` is the real-agent variant:
`BATON_JUDGE_AGENT_HOME` names an existing Hermes home whose default model works (never `~/.hermes`),
the plugin is installed there with role remote, the API server key reaches its gateway through the
process environment only, and the model does step 5 itself (receive, fix, check, return) under a
timeout (`BATON_JUDGE_AGENT_TIMEOUT`, 1200 s).

## 10. Tests (target >= 100, green, no network except loopback, no model calls)

capture (clean, dirty, untracked, ignored, unborn branch, submodule refused; byte-identical
HEAD/index/stash/worktree), secretscan (each rule true positive, known false positives such as
`.env.example`, placeholders), manifest (roundtrip, tamper detection), transcript (redaction pass,
cap, missing session), runs client against a fake aiohttp server (replay header, 401, 409 conflict,
unreachable, cross-origin redirect refused), peers (record parsing, missing key), store (transitions,
illegal transitions), local pass/resume/land (branch absent, identical, diverged, checked out),
remote receive/check/return (idempotency, allowlist, timeout, stale check refused, failed return),
pre_verify decisions, full loop with two temp repos and a bare remote, `work` deterministic path.

## 11. Definition of done

pytest green; `hermes plugins validate .` passes with 0 warnings, verdict safe; `doctor --ci` exit 0;
`judge/run-local.sh` succeeds end to end on this machine; README has Try it, Verify in 5 minutes,
Disclosures (git pushes to the configured remote, HTTP to the configured peer, subprocess `hermes
sessions export` and `git`, the allowlisted check command on the remote), What is not covered
(Hermes Cloud API reachability depends on the instance; Desktop surface not included).

Style: no em dash character (U+2014) anywhere in this repo (code, comments, strings, docs).
