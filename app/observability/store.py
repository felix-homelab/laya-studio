"""A self-contained JSONL log store.

Why a file rather than a log service: the panel is deployed as *one container and
one PostgreSQL*, and the questions you actually ask while debugging one are "what
happened in the last few minutes" and "has this traceback happened before". A
rotated JSONL file answers both with ``grep``, with the read-only API in
:mod:`app.routers.observability`, and — because it is bind-mounted out of the
container — from a shell on the host. Nothing extra to run, nothing extra to
secure, and it survives a crash, which is precisely when you need it.

What it is not: a metrics pipeline. There is no aggregation, no shipping and no
sampling. Records are the unit, the window is bounded by rotation and retention,
and every query reports how much of the window it actually looked at
(``scanned``/``truncated``) so a "nothing matching" answer is never ambiguous.

Concurrency: one file per process (the pid is in the name), and an exclusive lock
around the append and the rotation. Two uvicorn workers therefore never interleave
a line, and neither does a ``laya-studio`` CLI command running alongside the
panel.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .records import LEVEL_RANK, normalize_timestamp, utc_now

#: How many lines a single query will look at before giving up and saying so.
DEFAULT_MAX_SCAN = 200_000

#: Tracebacks are the point of the store, but a 200 KB one helps nobody.
MAX_TRACEBACK_CHARS = 12_000

#: Rotation triggers a retention sweep, and so does every Nth write in between —
#: often enough that a stale file does not outlive its window, rare enough that it
#: never shows up in a profile.
PRUNE_EVERY = 500


def _jsonable(value: Any) -> Any:
    """Last-resort encoder for ``json.dumps``."""
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return repr(value)


def _mtime(item: Path) -> float:
    """A file's mtime, or 0 when it was rotated or pruned away mid-listing."""
    try:
        return item.stat().st_mtime
    except OSError:
        return 0.0


class LogStore:
    """Rotating JSONL writer plus the read side the API and MCP tools use."""

    def __init__(
        self,
        directory: Path | str,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        backup_count: int = 5,
        retention_days: int = 14,
        filename: str | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.backup_count = max(1, int(backup_count))
        self.retention_days = max(0, int(retention_days))
        self.filename = filename or f"laya-studio-{os.getpid()}.jsonl"
        self._lock = threading.Lock()
        self._writes = 0

    # -- writing ------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The file currently being appended to."""
        return self.directory / self.filename

    def ensure(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.path

    def write(self, record: dict[str, Any]) -> None:
        """Append one record. Never raises: logging must not break a request."""
        try:
            line = json.dumps(record, default=_jsonable, ensure_ascii=False)
        except Exception as error:  # noqa: BLE001 - a circular reference, a non-str key
            # Keep a trace that the record existed rather than raising into the
            # request that logged it.
            line = json.dumps(
                {
                    "ts": utc_now(),
                    "kind": "log",
                    "level": "ERROR",
                    "logger": "laya_studio.store",
                    "message": f"Dropped an unserializable record: {type(error).__name__}: {error}",
                },
                ensure_ascii=False,
            )
        prune = False
        try:
            with self._lock:
                self.ensure()
                # Rotate *before* appending rather than after: the live file then
                # always exists and is short, which is what a `tail -f` on the host
                # expects. Rotating afterwards leaves a moment with no live file.
                if self._over_budget():
                    self._rotate()
                    prune = True
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                self._writes += 1
                if self._writes % PRUNE_EVERY == 0:
                    prune = True
        except OSError:
            # A full disk or a read-only mount must not take the panel down. The
            # stderr handler is unaffected, so the line is not lost outright.
            return
        if prune:
            self.prune()

    def _over_budget(self) -> bool:
        try:
            return self.path.stat().st_size >= self.max_bytes
        except OSError:
            return False

    def _rotate(self) -> None:
        """Shift the backup chain. Called with the lock held."""
        oldest = self.path.with_name(f"{self.filename}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.filename}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.filename}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.filename}.1"))

    def prune(self) -> int:
        """Delete rotated files older than the retention window."""
        if self.retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        removed = 0
        # Under the same lock as the rotation: renaming a backup keeps its mtime,
        # so an unserialized sweep could delete a file `_rotate` is shifting.
        with self._lock:
            for candidate in self.directory.glob("*.jsonl*"):
                if candidate == self.path:
                    continue
                try:
                    modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if modified < cutoff:
                    try:
                        candidate.unlink()
                        removed += 1
                    except OSError:  # pragma: no cover - raced with another process
                        continue
        return removed

    # -- reading ------------------------------------------------------------

    def files(self, *, newest_first: bool = True) -> list[Path]:
        """Every log file for this directory, the live one included."""
        try:
            candidates = [item for item in self.directory.glob("*.jsonl*") if item.is_file()]
        except OSError:  # pragma: no cover - unreadable directory
            return []
        candidates.sort(key=_mtime)
        return list(reversed(candidates)) if newest_first else candidates

    def query(
        self,
        *,
        kind: str | None = None,
        level: str | None = None,
        logger: str | None = None,
        search: str | None = None,
        request_id: str | None = None,
        path: str | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        has_exception: bool | None = None,
        fingerprint: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        order: str = "newest",
        include_traceback: bool = False,
        max_scan: int = DEFAULT_MAX_SCAN,
    ) -> dict[str, Any]:
        """Filter the retained records.

        ``level`` is a *floor* (``"WARNING"`` matches WARNING, ERROR and CRITICAL),
        which is what you want when reading a log; substring matching on the level
        is never useful. ``search`` is a case-insensitive substring over the
        message.
        """
        since = normalize_timestamp(since)
        until = normalize_timestamp(until)
        needle = search.lower() if search else None
        lower_logger = logger.lower() if logger else None
        min_rank = LEVEL_RANK.get((level or "").upper())
        limit = max(1, min(int(limit), 1000))

        matches: deque[dict[str, Any]] = deque(maxlen=limit)
        scanned = 0
        truncated = False

        for file_path in self.files(newest_first=order != "oldest"):
            if scanned >= max_scan:
                truncated = True
                break
            # `newest` reads the newest file first, but each file top to bottom:
            # collect one file's matches on their own (a deque keeps its newest
            # `room`), then put them *behind* the newer ones already collected.
            file_matches: deque[dict[str, Any]] = deque(maxlen=max(1, limit - len(matches)))
            for record in self._iter_records(file_path, max_scan - scanned):
                scanned += 1
                if not self._keep(
                    record,
                    kind=kind,
                    min_rank=min_rank,
                    logger=lower_logger,
                    needle=needle,
                    request_id=request_id,
                    path=path,
                    status_min=status_min,
                    status_max=status_max,
                    has_exception=has_exception,
                    fingerprint=fingerprint,
                    since=since,
                    until=until,
                ):
                    continue
                if order == "oldest":
                    # The oldest file is read first, so the first `limit` matches
                    # are the ones we want; stop early.
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(record)
                else:
                    file_matches.append(record)
            if order == "oldest":
                if len(matches) >= limit:
                    break
                continue
            matches.extendleft(reversed(file_matches))
            if len(matches) >= limit:
                # Every file left is older than what is already collected.
                truncated = True
                break

        records = [self._shrink(record, include_traceback) for record in matches]
        records.sort(key=lambda item: item.get("ts") or "")
        if order == "newest":
            records.reverse()

        return {
            "records": records,
            "count": len(records),
            "scanned": scanned,
            "truncated": len(records) >= limit or truncated,
            "files": [str(item.name) for item in self.files()],
            "level_floor": (level or "").upper() or None,
        }

    def tail(self, lines: int = 50, *, include_traceback: bool = True) -> list[dict[str, Any]]:
        """The most recent records, whatever they are."""
        return self.query(limit=max(1, min(int(lines), 1000)), order="newest",
                          include_traceback=include_traceback)["records"]

    def error_groups(
        self,
        *,
        since: str | None = None,
        limit: int = 50,
        include_traceback: bool = False,
        max_scan: int = DEFAULT_MAX_SCAN,
    ) -> dict[str, Any]:
        """Exceptions grouped by :func:`app.observability.records.fingerprint`.

        The grouping is what makes a day-old log readable: 4 000 failed rows that
        are all one ``ValidationError`` should read as one entry with a count.
        """
        result = self.query(since=since, limit=1000, order="newest", max_scan=max_scan,
                            include_traceback=True)
        groups: dict[str, dict[str, Any]] = {}
        for record in result["records"]:
            exc = record.get("exc")
            if not exc:
                continue
            key = exc.get("fingerprint") or "unknown"
            group = groups.get(key)
            if group is None:
                groups[key] = {
                    "fingerprint": key,
                    "type": exc.get("type"),
                    "location": exc.get("location"),
                    "message": exc.get("message"),
                    "level": record.get("level"),
                    "logger": record.get("logger"),
                    "kind": record.get("kind"),
                    "count": 1,
                    "first_seen": record.get("ts"),
                    "last_seen": record.get("ts"),
                    "loggers": [record.get("logger")],
                    "paths": [record.get("path")] if record.get("path") else [],
                    "last_request_id": record.get("request_id"),
                    "traceback": exc.get("traceback"),
                }
                continue
            group["count"] += 1
            first = group["first_seen"] or record.get("ts")
            last = group["last_seen"] or record.get("ts")
            group["first_seen"] = min(first, record.get("ts") or first)
            group["last_seen"] = max(last, record.get("ts") or last)
            if record.get("logger") not in group["loggers"]:
                group["loggers"].append(record.get("logger"))
            if record.get("path") and record["path"] not in group["paths"]:
                group["paths"].append(record["path"])
            group["last_request_id"] = record.get("request_id") or group["last_request_id"]

        ordered = sorted(groups.values(), key=lambda item: item["last_seen"] or "", reverse=True)
        if not include_traceback:
            for group in ordered:
                group.pop("traceback", None)
        return {
            "groups": ordered[: max(1, min(int(limit), 200))],
            "count": len(ordered),
            "scanned": result["scanned"],
            "since": since,
            "truncated": result["truncated"],
        }

    def stats(self, *, max_scan: int = 50_000) -> dict[str, Any]:
        """Sizes, retention settings and counts — enough to know if the log is worth reading."""
        files: list[dict[str, Any]] = []
        total_bytes = 0
        for item in self.files():
            try:
                stat = item.stat()
            except OSError:  # pragma: no cover
                continue
            total_bytes += stat.st_size
            files.append(
                {
                    "name": item.name,
                    "bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                }
            )

        counts: dict[str, int] = {name: 0 for name in LEVEL_RANK}
        kinds: dict[str, int] = {}
        scanned = 0
        oldest: str | None = None
        newest: str | None = None
        for file_path in self.files():
            for record in self._iter_records(file_path, max_scan - scanned):
                scanned += 1
                level = record.get("level") or "INFO"
                counts[level] = counts.get(level, 0) + 1
                kind = record.get("kind") or "log"
                kinds[kind] = kinds.get(kind, 0) + 1
                ts = record.get("ts")
                if ts:
                    oldest = ts if oldest is None else min(oldest, ts)
                    newest = ts if newest is None else max(newest, ts)
                if scanned >= max_scan:
                    break
            if scanned >= max_scan:
                break

        return {
            "log_dir": str(self.directory),
            "current_file": str(self.path),
            "max_bytes": self.max_bytes,
            "backup_count": self.backup_count,
            "retention_days": self.retention_days,
            "files": files,
            "total_bytes": total_bytes,
            "scanned": scanned,
            "counts_by_level": counts,
            "counts_by_kind": kinds,
            "oldest_ts": oldest,
            "newest_ts": newest,
        }

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _iter_records(file_path: Path, budget: int) -> Iterator[dict[str, Any]]:
        """Yield parsed records from one file, cheapest-first on mistakes."""
        if budget <= 0:
            return
        try:
            handle = file_path.open("r", encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - raced with a rotation
            return
        try:
            fallback_ts = datetime.fromtimestamp(
                file_path.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="milliseconds")
        except OSError:  # pragma: no cover
            fallback_ts = ""
        seen = 0
        last_ts: str | None = None
        with handle:
            for line in handle:
                if seen >= budget:
                    return
                seen += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # A half-written line from a killed process, or a plain-text
                    # line that predates the store. Keep it: the raw text is
                    # usually the interesting part.
                    record = {"kind": "log", "level": "INFO",
                              "logger": "laya_studio.unparsed", "message": line}
                if not isinstance(record, dict):
                    continue
                if not record.get("ts"):
                    # A line with no timestamp of its own — unparseable, or written
                    # by something that is not the handler. Anchor it to the record
                    # before it so ordering and time filters still say something
                    # true, and mark it: nothing should look like it was parsed.
                    record["ts"] = last_ts or fallback_ts
                    record["ts_estimated"] = True
                last_ts = record.get("ts") or last_ts
                yield record

    @staticmethod
    def _keep(record: dict[str, Any], **filters: Any) -> bool:
        if filters["kind"] and (record.get("kind") or "log") != filters["kind"]:
            return False
        min_rank = filters["min_rank"]
        if min_rank is not None and LEVEL_RANK.get(record.get("level", "INFO"), 0) < min_rank:
            return False
        logger = filters["logger"]
        if logger and logger not in (record.get("logger") or "").lower():
            return False
        needle = filters["needle"]
        if needle:
            haystack = " ".join(
                str(record.get(key) or "")
                for key in ("message", "logger", "mcp_tool", "path")
            ).lower()
            if needle not in haystack:
                return False
        if filters["request_id"] and (record.get("request_id") or "") != filters["request_id"]:
            return False
        if filters["path"] and filters["path"] not in (record.get("path") or ""):
            return False
        status_min = filters["status_min"]
        if status_min is not None and (record.get("status") is None or record["status"] < status_min):
            return False
        status_max = filters["status_max"]
        if status_max is not None and (record.get("status") is None or record["status"] > status_max):
            return False
        has_exception = filters["has_exception"]
        if has_exception is not None and bool(record.get("exc")) is not has_exception:
            return False
        fingerprint = filters["fingerprint"]
        if fingerprint and (record.get("exc") or {}).get("fingerprint") != fingerprint:
            return False
        ts = record.get("ts") or ""
        if filters["since"] and ts < filters["since"]:
            return False
        if filters["until"] and ts > filters["until"]:
            return False
        return True

    @staticmethod
    def _shrink(record: dict[str, Any], include_traceback: bool) -> dict[str, Any]:
        """Trim a record for the wire: tracebacks are opt-in, and always capped."""
        exc = record.get("exc")
        if not exc:
            return record
        exc = dict(exc)
        traceback_text = exc.get("traceback") or ""
        if not include_traceback:
            exc.pop("traceback", None)
            exc["traceback_chars"] = len(traceback_text)
        elif len(traceback_text) > MAX_TRACEBACK_CHARS:
            exc["traceback"] = traceback_text[-MAX_TRACEBACK_CHARS:]
            exc["traceback_truncated"] = True
        record = dict(record)
        record["exc"] = exc
        return record


def open_store(
    directory: Path | str,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
    retention_days: int | None = None,
) -> LogStore:
    """Build a store with explicit overrides, for tools and tests."""
    kwargs: dict[str, Any] = {}
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    if backup_count is not None:
        kwargs["backup_count"] = backup_count
    if retention_days is not None:
        kwargs["retention_days"] = retention_days
    return LogStore(directory, **kwargs)


def read_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Read records straight out of files, for callers holding paths not a store."""
    for path in paths:
        yield from LogStore._iter_records(path, DEFAULT_MAX_SCAN)
