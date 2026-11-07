# Baton

Hand your session and your repository to your remote Hermes agent (Hermes Cloud, a VPS or a Docker
container), close the laptop, and land a tested branch the next morning.

```
/baton "fix the parser, done when pytest passes" --done-when "python -m pytest -q"
```

The remote agent works in a throwaway worktree, cannot return `verified` until the agreed check is
green, and pushes back a branch `baton/<id>`. `hermes baton land <id>` fetches that branch next to
your work: your HEAD, index, working tree and stash are not touched.

**Code travels by git, the order travels by the Hermes Runs API, state travels by git refs.** No
private Hermes import, no Desktop dependency, and every step can be resumed after a network cut (same
baton id, same `Idempotency-Key`, same remote run).

```
 LOCAL (your laptop)                                   REMOTE (VPS / Docker / Hermes Cloud)
 /baton or hermes baton pass
   snapshot of the work tree (temp index)
   blocking secret scan of the outgoing changes
   BATON.json + transcript + SHA256SUMS
   push refs/baton/<id>/in  ----------------->  shared bare git repo  <------  baton_receive (worktree)
   POST /v1/runs, Idempotency-Key = <id>  --->  gateway API server             baton_check (allowlisted)
                                                                               pre_verify: no "done" while red
 hermes baton land <id>  <-------------------  refs/heads/baton/<id>  <------  baton_return
```

## Try it

Requirements: Hermes Agent v0.23.0 on both machines, git, and a bare git repository both machines can
push to and fetch from (an SSH URL, or a path on a shared volume).

1. Install the plugin on both machines:
   ```
   hermes plugins install https://github.com/4n0nn43x/hermes-baton --enable --yes-deps
   ```
2. On the remote, give the gateway an API server and the remote role:
   ```
   # in the remote's ~/.hermes/.env (key: at least 16 characters, e.g. openssl rand -hex 24)
   API_SERVER_KEY=<key>
   API_SERVER_HOST=127.0.0.1        # or 0.0.0.0 behind a tunnel / reverse proxy
   API_SERVER_PORT=8642

   hermes pm install --extra messaging    # only if the gateway logs "API Server: aiohttp not installed"
   hermes config set plugins.entries.baton.settings.role remote
   hermes config set plugins.entries.baton.settings.remote_git <shared bare repo>
   ```
   Then restart the remote gateway the way you normally run it.
3. On your laptop, register the remote and the shared repo:
   ```
   hermes peer add box --url https://box.example:8642 --key <key>
   hermes config set plugins.entries.baton.settings.role local
   hermes config set plugins.entries.baton.settings.remote_git <shared bare repo>
   hermes baton doctor --peer box        # reachability, auth, runs_idempotency, git remote
   ```
4. Pass a baton, from a session or from the shell:
   ```
   /baton fix the parser --done-when "python -m pytest -q" --dry-run   # shows what would be sent
   /baton fix the parser --done-when "python -m pytest -q"
   hermes baton pass "fix the parser" --peer box --done-when "python -m pytest -q"
   ```
5. The next morning:
   ```
   hermes baton status            # state from the status/result refs and the Runs API
   hermes baton land <id>         # creates the local branch baton/<id>, nothing else
   git log baton/<id>
   ```

No API port reachable (the likely case for a Hermes Cloud instance): pass with `--no-trigger`; the
baton waits on the shared repo, and the remote picks it up with `hermes baton work <id>` (no model,
deterministic) or from an agent session.

## Verify in 5 minutes

One command runs the whole path on one machine with **two real, isolated Hermes v0.23.0 instances**
and no model (nothing paid can run):

```
judge/run-local.sh      # then: judge/teardown.sh
```

What it does, all under `judge/.work/`:

- creates two fresh Hermes homes, "local" and "remote", each with its own `HERMES_HOME`, `HOME`,
  gateway lock dir and XDG dirs, `GATEWAY_MULTIPLEX_PROFILES=false`, every process started from
  `env -i` (no provider key inherited). Your `~/.hermes` and your own gateway are never touched;
- installs the plugin in both homes with `hermes plugins install file://<git snapshot of this tree>
  --enable --yes-deps`, registers the remote with `hermes peer add`, and starts the remote gateway
  with its API server on `127.0.0.1:8652` and a random key;
- copies `judge/sample/` (a calculator whose `mean()` divides by `len - 1`) into a git repo with a
  stash entry, a staged change, an unstaged change and an untracked file;
- **green path**: `hermes baton pass ... --done-when "python -m pytest -q"` (real `POST /v1/runs`,
  then the same request again to show `replayed=True` with the same run id), remote `hermes baton work`
  (receive, red check, a scripted one-line fix printed as "simulated agent fix", green check, return
  `verified`), local `hermes baton land`;
- **red path**: a second baton nobody fixes, returned `FAILED`, and `land` refuses it;
- **allowlist**: a baton whose check is `sh -c 'echo pwned'` is refused by the remote
  (`check_not_allowed`);
- compares HEAD, the index bytes, the stash, `git status` and the sha256 of every file of the local
  repo before and after (must be identical), stops the gateway with SIGTERM and checks the port is
  closed.

Measured on the author's machine (Linux, slow link): a fresh Hermes home prepares its own
dependency environment when the plugin is enabled, which took 376 s for the first home on an empty
cache and 48 s for the second (both homes share one uv cache). With that cache kept between runs
(`BATON_JUDGE_UV_CACHE=<dir>`), the whole script took 2 min 16 s. Variables: `HERMES_AGENT_DIR`, `HERMES_PYTHON`, `BATON_JUDGE_PORT`
(8652 to 8655), `BATON_JUDGE_UV_CACHE`.

`judge/run-docker.sh` runs the same scenario with the remote in `nousresearch/hermes-agent:v0.23.0`
(about 1.1 GB to pull). **Not tested**: the pull did not finish within 10 minutes on the author's link.

Run `judge/teardown.sh` before `hermes plugins validate .`: the validator scans everything under the
plugin directory, including the judge's throwaway venv.

Tests: `uv run --no-project --with pytest --with pytest-asyncio --with aiohttp pytest -q`
(195 passed, no network except loopback, no model call).

## Disclosures

- **git pushes** to the shared remote you configure (`remote_git`): `refs/baton/<id>/*` from your
  laptop, `refs/heads/baton/<id>` and the result/status refs from the remote. Nothing is pushed to
  your project's own remotes.
- **HTTP** to the peer you registered (`hermes peer add`): `/health`, `/v1/capabilities`,
  `/api/sessions`, `/v1/runs`. The bearer key goes only to that origin (a redirect to another origin
  is refused); it is never logged or stored by Baton.
- **Subprocesses**: `git` (argv only, sanitized environment, timeouts) and
  `hermes sessions export --redact` for the transcript of the session you pass.
- **The check runs on the remote machine with the rights of the Hermes user**: the code of the
  repository you send (conftest.py, Makefile, npm scripts) runs as is. Baton gives it an empty
  throwaway HOME, no HERMES_* variable and no key, only runs argv that the remote's `allowed_checks`
  allows (followed by safe flags and relative paths), and, when bubblewrap works on that machine,
  hides the Hermes home and kills every process of the check at the end. Without bubblewrap, that
  code can read `~/.hermes` by absolute path: only accept batons from your own machines.
- The outgoing snapshot is scanned for secrets and the pass is **blocked** (never masked) on a
  finding; symlinks that point outside the repository are refused too (`unsafe_snapshot`).

## What is not covered

- Hermes Cloud: whether an instance's API port is reachable depends on the instance; without it,
  use `--no-trigger` and `hermes baton work` on the remote.
- No Desktop surface (CLI, slash command, tools and skills only).
- The check executes the code you send with the remote user's rights: Baton is made for your own
  machines, not for running other people's batons.
- The real-agent variant (the remote model writes the fix itself) needs a model configured in the
  remote home; the judge homes have none, so the judge path scripts the fix instead.

## Team

- TODO(user): name, role, background, past projects or hackathons, country.
- TODO(user): GitHub, X and LinkedIn links.
