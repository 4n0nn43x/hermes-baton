"""Resolve a peer (remote gateway URL + API key).

Primary source: the `hermes peer add` record (Hermes v0.23.0, hermes_cli/subcommands/peer.py:38-68):
`bot_peers: {name: {url, note?}}` in config.yaml and the key in `HERMES_PEER_<NAME>_KEY` (upper case,
`-` becomes `_`), read from the profile secret scope or the environment. Fallback: plugin settings
`peers: {name: {url, key_env}}`. The key is never logged nor stored.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .errors import BatonError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROFILE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Peer:
    name: str
    url: str
    key: str = field(repr=False)
    source: str = "bot_peers"


def key_env_name(name: str) -> str:
    return f"HERMES_PEER_{name.upper().replace('-', '_')}_KEY"


def _secret(env_name: str, env: Mapping[str, str], secret_getter: Callable[[str], str] | None) -> str:
    if secret_getter is not None:
        try:
            value = secret_getter(env_name) or ""
        except Exception:
            value = ""
        if value.strip():
            return value.strip()
    return (env.get(env_name) or "").strip()


def resolve(name: str, config: Mapping | None, settings: Mapping | None = None, *,
            env: Mapping[str, str] | None = None,
            secret_getter: Callable[[str], str] | None = None) -> Peer:
    """`name` may be `peer` or `peer/profile` (the `/p/<profile>` mirror, like `hermes peer`)."""
    env = os.environ if env is None else env
    raw = (name or "").strip()
    peer_name, _, profile = raw.partition("/")
    peer_name, profile = peer_name.strip().lower(), profile.strip()
    if not NAME_RE.match(peer_name):
        raise BatonError("peer_unknown", f"invalid peer name: {raw!r}")
    if profile and not PROFILE_RE.match(profile):
        raise BatonError("peer_unknown", f"invalid profile name: {profile!r}")

    bot_peers = (config or {}).get("bot_peers")
    record = bot_peers.get(peer_name) if isinstance(bot_peers, Mapping) else None
    url, key_env, source = "", key_env_name(peer_name), "bot_peers"
    if isinstance(record, Mapping) and record.get("url"):
        url = str(record["url"])
    else:
        plugin_peers = (settings or {}).get("peers")
        entry = plugin_peers.get(peer_name) if isinstance(plugin_peers, Mapping) else None
        if isinstance(entry, Mapping) and entry.get("url"):
            url, source = str(entry["url"]), "settings"
            key_env = str(entry.get("key_env") or key_env)
    if not url:
        raise BatonError("peer_unknown", f"no peer named '{peer_name}' (register it with: hermes peer add)")
    if not url.lower().startswith(("http://", "https://")):
        raise BatonError("peer_unknown", f"peer '{peer_name}' has a non-http(s) URL")
    key = _secret(key_env, env, secret_getter)
    if not key:
        raise BatonError("peer_key_missing", f"no API key for peer '{peer_name}' (set {key_env})")
    url = url.rstrip("/")
    if profile:
        url = f"{url}/p/{urllib.parse.quote(profile.lower(), safe='')}"
    return Peer(peer_name, url, key, source)
