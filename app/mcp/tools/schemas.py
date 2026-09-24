"""Schema definitions: the question bundles every other module reads from."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import DESTRUCTIVE, READ, WRITE
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def schemas_list(
        include_templates: bool = True, search: str | None = None, tag: str | None = None
    ) -> Any:
        """Every schema, newest first. Each row has `id`, `key`, `primitive` (the
        dominant question type) and `current_revision`. The four built-ins are
        templates (`is_template: true`) and cannot be deleted — fork them.
        Returns `{count, items}`."""
        return await panel.get_collection(
            "/api/schemas",
            tool="schemas_list",
            params={"include_templates": include_templates, "search": search, "tag": tag},
        )

    @server.tool(annotations=READ)
    async def schema_get(schema_id: int) -> Any:
        """One schema with its full `definition` — the questions, their types and
        their criteria. Read this before `evaluate` to get the question names right."""
        return await panel.get(f"/api/schemas/{schema_id}", tool="schema_get")

    @server.tool(annotations=WRITE)
    async def schema_create(
        name: str,
        questions: dict[str, Any],
        description: str = "",
        tags: list[str] | None = None,
        key: str | None = None,
    ) -> Any:
        """Create a schema. `questions` is `{name: {type, instructions, criteria}}`
        with type choice/score/noul; for `choice`, `criteria` maps each label to its
        description; for `score`, it is the ordered list of levels. `key` is derived
        from the name when omitted. Run `lint_questions` first — the linter catches
        what the request validator cannot."""
        return await panel.post(
            "/api/schemas",
            tool="schema_create",
            json={
                "name": name,
                "description": description,
                "questions": questions,
                "tags": tags or [],
                "key": key,
            },
        )

    @server.tool(annotations=WRITE)
    async def schema_update(
        schema_id: int,
        name: str | None = None,
        description: str | None = None,
        questions: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        note: str = "",
    ) -> Any:
        """Update a schema. Changing `questions` mints a new revision, which is what
        an A/B test compares against — pass `note` to say why."""
        return await panel.put(
            f"/api/schemas/{schema_id}",
            tool="schema_update",
            json={
                "name": name,
                "description": description,
                "questions": questions,
                "tags": tags,
                "note": note,
            },
        )

    @server.tool(annotations=DESTRUCTIVE)
    async def schema_delete(schema_id: int) -> Any:
        """Delete a schema. Refuses 409 on a built-in template; fork it instead."""
        return await panel.delete(f"/api/schemas/{schema_id}", tool="schema_delete")

    @server.tool(annotations=WRITE)
    async def schema_fork(schema_id: int, name: str) -> Any:
        """Copy a schema — including a built-in template — into an editable one."""
        return await panel.post(
            f"/api/schemas/{schema_id}/fork", tool="schema_fork", params={"name": name}
        )

    @server.tool(annotations=READ)
    async def schema_lint(schema_id: int) -> Any:
        """Lint a stored schema's questions, with the same findings the editor shows."""
        return await panel.get(f"/api/schemas/{schema_id}/lint", tool="schema_lint")

    @server.tool(annotations=READ)
    async def schema_revisions(schema_id: int) -> Any:
        """The revision history. `revision_a_id`/`revision_b_id` for an A/B test come
        from here. Returns `{count, items}`."""
        return await panel.get_collection(
            f"/api/schemas/{schema_id}/revisions", tool="schema_revisions"
        )
