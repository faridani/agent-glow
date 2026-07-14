"""Light references, per-state role resolution, and friendly-name matching.

A *role* is a per-state light list: ``thinking`` bulbs breathe while an agent
works, ``waiting`` bulbs take the wait color when an agent needs the user.
Role entries are light references — ``"hue:<uuid>"`` / ``"wiz:<mac>"``; a bare
id means ``hue:`` so pre-roles target ids work unchanged. An empty role falls
back to the full default pool, which is exactly the pre-roles behavior.

This module stays dependency-free (pure functions) so config validation, the
backends, and the CLI can all import it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_BACKENDS = ("hue", "wiz")
ROLE_NAMES = ("thinking", "waiting")


def parse_light_ref(ref: str) -> tuple[str, str]:
    """Split ``"backend:id"``; a bare id is a Hue id for backward compat."""
    backend, sep, light_id = ref.partition(":")
    if not sep:
        return "hue", ref
    return backend, light_id


def format_light_ref(backend: str, light_id: str) -> str:
    return f"{backend}:{light_id}"


def effective_role_ids(
    config, role: str, default_ids: list[str], backend: str = "hue"
) -> list[str]:
    """Backend-local ids for one role; empty config list ⇒ the default pool."""
    configured = getattr(config.roles, role)
    if not configured:
        return list(default_ids)
    ids = []
    for ref in configured:
        ref_backend, light_id = parse_light_ref(ref)
        if ref_backend == backend and light_id not in ids:
            ids.append(light_id)
    return ids


@dataclass
class LightInfo:
    """One light as shown to users: friendly name plus its stable reference."""

    ref: str
    backend: str
    id: str
    name: str
    supports_color: bool = False
    reachable: bool | None = None
    roles: list[str] = field(default_factory=list)


class MatchError(ValueError):
    """A name query matched no light, or more than one."""


def match_light(query: str, inventory: list[LightInfo]) -> LightInfo:
    """Resolve a user-supplied name (or raw ref/id) to exactly one light.

    Case-insensitive exact name match wins; otherwise a unique substring
    match; anything else raises with the candidates so the caller (or the
    agent driving the CLI) can retry with a fuller name.
    """
    q = query.strip()
    for info in inventory:
        if q == info.ref or q == info.id:
            return info
    lowered = q.lower()
    exact = [i for i in inventory if i.name.lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise MatchError(
            f"{query!r} names {len(exact)} lights; rename one or choose a ref "
            "locally with `hue-agent lights`"
        )
    partial = [i for i in inventory if lowered in i.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(i.name for i in partial)
        raise MatchError(f"{query!r} is ambiguous: matches {names}")
    known = ", ".join(sorted(i.name for i in inventory)) or "(none)"
    raise MatchError(f"no light matches {query!r}; known lights: {known}")
