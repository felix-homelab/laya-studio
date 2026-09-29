"""Tool annotations, so a client can tell a read from a delete.

Announced over the protocol rather than described in prose: a client that gates
destructive calls on a confirmation prompt reads ``destructive_hint``, and one that
runs read-only tools in parallel reads ``read_only_hint``. Getting these right is
the difference between an agent that can be given the whole tool surface and one
that has to be handed a subset.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

#: A plain read. Safe to call at any time, safe to run concurrently.
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)

#: A write that can be repeated without extra damage (a re-run, a refit, an update).
#: ``destructive_hint`` is spelled out: the protocol defaults it to true.
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)

#: A write that cannot be undone: deletes, and actions with external side effects.
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
