"""Read-only SQL against the panel's database.

The HTTP API answers "what does the panel think", and sometimes the question is
one step lower: which rows exist, what a skill's stored definition actually says,
why a count disagrees with the UI. This tool is the answer to that, and it is
built so that "read-only" is a property of the *runtime* rather than of a keyword
check:

* the statement runs inside ``SET TRANSACTION READ ONLY``, which PostgreSQL
  enforces — a write that slips past the text scan still fails;
* ``statement_timeout`` and ``lock_timeout`` bound it, so a bad plan cannot pin a
  connection;
* the text scan (:func:`validate_sql`) rejects more than one statement and any
  data-modifying keyword, after stripping string literals and comments so that a
  value like ``'delete me'`` does not trip it;
* results are wrapped in a ``LIMIT`` and fetched with ``fetchmany``, so a query
  over a big table returns rows rather than filling memory.

What it is not: a SQL console to write through. If a fix needs a write, it belongs
in a migration (``prisma/``) or in the API.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ
from ..panel import Panel

#: The tap on the shoulder: how long one query may take, and how long it may wait
#: for a lock it should never need.
STATEMENT_TIMEOUT_MS = 10_000
LOCK_TIMEOUT_MS = 3_000

#: Statements that only read. ``explain`` is included so a plan can be inspected;
#: ``show`` and ``table`` are PostgreSQL conveniences for a quick look around.
ALLOWED_OPENERS = ("select", "with", "table", "values", "show", "explain")

#: Anything that modifies data or configuration, matched on word boundaries after
#: literals and comments are stripped — plus the server-side functions that read
#: the database host's filesystem or open a second connection, which the read-only
#: transaction would not cover. ``comment`` and ``security`` are deliberately not
#: here: they only ever begin a statement the opener check already refuses, and
#: both are ordinary column names.
FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "merge", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "vacuum", "reindex", "cluster", "call",
    "do", "set", "reset", "begin", "commit", "rollback", "savepoint", "lock",
    "listen", "notify", "refresh", "import",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_ls_logdir", "pg_ls_waldir",
    "pg_stat_file", "lo_import", "lo_export", "dblink", "dblink_exec",
)

_FORBIDDEN_PATTERN = re.compile(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)

#: A dollar-quote delimiter: ``$$`` or ``$tag$``, the tag being an identifier
#: without ``$`` (so ``$a1$`` is one as well).
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")


def _is_identifier_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or char in "_$")


def strip_literals_and_comments(sql: str) -> str:
    """Blank out string/identifier literals, ``--`` comments and ``/* */`` blocks.

    So that ``WHERE note = 'delete later'`` is not mistaken for a DELETE, and so
    that a commented-out statement cannot hide behind a ``--``.
    """
    out: list[str] = []
    index = 0
    length = len(sql)
    # What executes is the original text, so this has to agree with PostgreSQL's
    # lexer on where each literal and comment ends: ending one early or late can
    # hide a `;` or a keyword that the server then runs.
    while index < length:
        char = sql[index]
        previous = sql[index - 1] if index else ""
        if char == "'" or char == '"':
            quote = char
            # E'...' honours backslash escapes: in `E'\''` the `\'` is a quote, not
            # the end of the literal.
            escapes = (
                quote == "'"
                and previous in ("e", "E")
                and not _is_identifier_char(sql[index - 2] if index > 1 else "")
            )
            out.append(quote * 2)
            index += 1
            while index < length:
                if escapes and sql[index] == "\\":
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        # A dollar-quoted string: $$...$$ or $tag$...$tag$. Not inside a name,
        # though: `foo$x$` is an identifier, and its `$x$` opens nothing.
        if char == "$" and not _is_identifier_char(previous):
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                tag = match.group(0)
                end = sql.find(tag, index + len(tag))
                out.append("''")
                index = length if end == -1 else end + len(tag)
                continue
            out.append(char)
            index += 1
            continue
        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if char == "/" and sql.startswith("/*", index):
            # Block comments nest in PostgreSQL: `/* /* */ */` is one comment.
            depth = 0
            while index < length:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                    if depth == 0:
                        break
                else:
                    index += 1
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def validate_sql(sql: str, *, limit: int = 100) -> str:
    """Return the statement to run, or raise :class:`ToolError` explaining why not."""
    text = (sql or "").strip()
    if not text:
        raise ToolError("Provide a SQL statement to run.")

    # The checks run against a copy with literals and comments blanked out, so that
    # `WHERE note = 'delete later'` is not mistaken for a DELETE. What *executes* is
    # the original statement — literals intact.
    checked = strip_literals_and_comments(text).strip()
    if not checked:
        raise ToolError("That is only a comment; there is nothing to run.")

    body = checked.rstrip(";").rstrip()
    if ";" in body:
        raise ToolError(
            "One statement per call: the string contains more than one SQL statement. "
            "Run them separately."
        )

    opener = re.match(r"[A-Za-z]+", body)
    if not opener or opener.group(0).lower() not in ALLOWED_OPENERS:
        raise ToolError(
            f"This tool only runs read-only queries (starting with "
            f"{', '.join(ALLOWED_OPENERS)}). Got: {body[:60]!r}."
        )

    forbidden = _FORBIDDEN_PATTERN.search(body)
    if forbidden:
        raise ToolError(
            f"{forbidden.group(0).upper()} is not allowed here: this tool reads. "
            "Schema changes belong in a Prisma migration (see prisma/schema.prisma), "
            "and data changes belong in the API."
        )

    statement = text.rstrip().rstrip(";").rstrip()
    if body.lower().startswith(("table ", "show ", "explain ")):
        return statement
    # Wrap in a subquery so the row budget is the database's problem, not the
    # client's: a five-million-row table then costs one extra row. The limit is
    # interpolated rather than bound because this SQL goes to the driver verbatim,
    # and it is an int this module has already clamped — there is nothing to inject.
    return f"SELECT * FROM ({statement}) AS mcp_query LIMIT {int(limit) + 1}"


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def db_query(sql: str, limit: int = 100) -> Any:
        """Run one read-only SQL statement against the panel's PostgreSQL.

        Only SELECT / WITH / TABLE / VALUES / SHOW / EXPLAIN reach the database, one
        statement at a time. The statement executes inside a read-only transaction
        with a 10s statement timeout, and rows are capped by `limit` (the response
        says whether it truncated).

        Useful targets: `evaluations`, `eval_schemas`, `bulk_jobs`,
        `workflow_runs`, `action_logs`, `review_items`, `calibration_snapshots` —
        see `docs/data-model.md`. Discover columns from `information_schema` rather
        than guessing them.
        """
        capped = max(1, min(int(limit), 1000))
        statement = validate_sql(sql, limit=capped)
        # The driver call blocks for up to the statement timeout; keep it off the
        # event loop every other tool call and request shares.
        return await asyncio.to_thread(_execute, statement, limit=capped)


def _execute(statement: str, *, limit: int) -> dict[str, Any]:
    """Run the validated statement on a fresh connection and shape the result."""
    from ...config import get_settings, redact_dsn
    from ...db import get_engine

    engine = get_engine()
    started = perf_counter()
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            connection.exec_driver_sql(f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}")

            result = connection.exec_driver_sql(statement)
            columns = list(result.keys())
            rows = result.mappings().fetchmany(limit + 1)
            truncated = len(rows) > limit
    except Exception as exc:  # noqa: BLE001 - any DB error is reported, not raised raw
        lines = str(exc).splitlines()
        detail = lines[0][:300] if lines else "no message"
        raise ToolError(f"The query failed: {type(exc).__name__}: {detail}") from exc

    return {
        "columns": columns,
        "rows": [{key: _jsonable(value) for key, value in row.items()} for row in rows[:limit]],
        "row_count": min(len(rows), limit),
        "truncated": truncated,
        "limit": limit,
        "duration_ms": round((perf_counter() - started) * 1000, 2),
        "database": redact_dsn(get_settings().database_url),
    }


def _jsonable(value: Any) -> Any:
    """Make a column value safe to hand to JSON."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time, timedelta, UUID, Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)

