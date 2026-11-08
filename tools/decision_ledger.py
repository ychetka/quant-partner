#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Offline append-only ledger of partner-recommendation outcomes (issue #54).

This script is NOT part of the MCP server: it maintains a local
``decisions.jsonl`` file that records, one JSON entry per line, which
partner recommendations the orchestrator adopted, modified or rejected.
The ledger is the caller-managed raw material for retrospective reviews:
a slice of entries is pasted to ``comms-partner`` (the ``retro-review``
skill) through the ordinary ``context`` argument of the ask tools.

Entry contract (``schema_version`` 1, strict):

- ``decision_id``, ``project``, ``partner``, ``recommendation_summary``
  - non-empty strings;
- ``context_size`` (a non-negative integer) and/or ``context_hash``
  (``sha256:<64 lowercase hex>``) - each optional on its own, but at
  least one of the two is required: they fingerprint the context a
  recommendation was given without storing the context itself;
- ``orchestrator_action`` - exactly one of ``adopted``, ``modified`` or
  ``rejected``;
- ``created_at`` - an ISO-8601 UTC timestamp ending in ``Z``.

The validator enforces the exact key set (unknown keys are rejected),
rejects every type mismatch and reports failures as named errors with a
stable ``code`` plus a remediation hint.

Append-only guarantee: ``append`` writes one newline-terminated JSON
line through a single ``O_APPEND`` ``os.open`` plus exactly one
``os.write``, so concurrent appenders never interleave a line. The file
is forced to mode 0600 on every append. No code path updates, truncates
or deletes existing bytes: ``read``, ``list`` and ``validate`` never
write, and ``append`` validates the whole existing file first and
refuses to write onto a corrupt ledger - including a tail line left
unterminated by an interrupted write.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_LEDGER_PATH = "./decisions.jsonl"

ORCHESTRATOR_ACTIONS = ("adopted", "modified", "rejected")

_ENTRY_REQUIRED = {
    "decision_id",
    "project",
    "partner",
    "recommendation_summary",
    "orchestrator_action",
    "created_at",
    "schema_version",
}
# The only optional keys; at least one of them must still be present.
_ENTRY_OPTIONAL = {"context_size", "context_hash"}
_ENTRY_KEYS = _ENTRY_REQUIRED | _ENTRY_OPTIONAL

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


class LedgerError(Exception):
    """Human-readable ledger failure; ``code`` is the stable machine code."""

    code = "ledger_error"

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_text(self) -> str:
        text = f"ERROR {self.code}: {self.message}"
        if self.hint:
            text += f" Hint: {self.hint}"
        return text


class UsageError(LedgerError):
    """Bad CLI usage or missing local inputs (files, field arguments)."""

    code = "invalid_usage"


class EntrySchemaError(LedgerError):
    """A decision entry object failed the schema contract."""

    code = "entry_schema_invalid"


class UnsupportedSchemaVersion(EntrySchemaError):
    """The entry declares a schema_version this tool cannot read."""

    code = "unsupported_schema_version"


class LedgerReadError(LedgerError):
    """The ledger file could not be read as UTF-8 text."""

    code = "ledger_read_failed"


class LedgerCorruptionError(LedgerError):
    """A ledger line failed JSON parsing, termination or the contract.

    ``line_number`` is the 1-based physical line in the ledger file, so
    a truncated tail line is reported exactly like a corrupt middle line.
    """

    code = "ledger_corrupt"

    def __init__(self, line_number: int, message: str, hint: str = "") -> None:
        super().__init__(message, hint)
        self.line_number = line_number

    def to_text(self) -> str:
        text = f"ERROR {self.code}: line {self.line_number}: {self.message}"
        if self.hint:
            text += f" Hint: {self.hint}"
        return text


class LedgerWriteError(LedgerError):
    """The append write to the ledger file failed."""

    code = "ledger_write_failed"


# --- entry schema contract ---------------------------------------------------


def validate_entry(obj: Any) -> None:
    """Raise ``EntrySchemaError`` unless ``obj`` is a valid decision entry.

    The contract is strict: the exact key set (``context_size`` and
    ``context_hash`` are the only optional keys and at least one of them
    is required), an integer ``schema_version`` equal to
    ``SCHEMA_VERSION`` (anything else raises ``UnsupportedSchemaVersion``),
    non-empty id/project/partner/summary strings, an
    ``orchestrator_action`` from ``ORCHESTRATOR_ACTIONS``, a
    ``context_size`` that is a non-negative integer, a ``context_hash``
    shaped ``sha256:<64 lowercase hex>`` and an ISO-8601 UTC
    ``created_at`` ending in ``Z``.
    """
    if not isinstance(obj, dict):
        raise EntrySchemaError("entry must be a JSON object")
    unknown = sorted(set(obj) - _ENTRY_KEYS)
    if unknown:
        raise EntrySchemaError(f"unknown entry keys: {', '.join(unknown)}")
    version = obj.get("schema_version")
    if version is None:
        raise EntrySchemaError("schema_version is required")
    if isinstance(version, bool) or not isinstance(version, int):
        raise EntrySchemaError("schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"unsupported schema_version: {version}",
            f"this tool reads schema_version {SCHEMA_VERSION} entries only",
        )
    for key in sorted(_ENTRY_REQUIRED - {"schema_version"}):
        if key not in obj:
            raise EntrySchemaError(f"missing required key: {key}")

    for key in ("decision_id", "project", "partner", "recommendation_summary"):
        if not isinstance(obj[key], str) or not obj[key].strip():
            raise EntrySchemaError(f"{key} must be a non-empty string")

    action = obj["orchestrator_action"]
    if not isinstance(action, str) or action not in ORCHESTRATOR_ACTIONS:
        raise EntrySchemaError(f"orchestrator_action must be one of {list(ORCHESTRATOR_ACTIONS)}")

    has_size = "context_size" in obj
    has_hash = "context_hash" in obj
    if not has_size and not has_hash:
        raise EntrySchemaError(
            "at least one of context_size or context_hash is required",
            "context_size counts the context characters; context_hash is a "
            "sha256 digest of the context the recommendation was given",
        )
    if has_size:
        size = obj["context_size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise EntrySchemaError("context_size must be a non-negative integer")
    if has_hash:
        digest = obj["context_hash"]
        if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
            raise EntrySchemaError('context_hash must look like "sha256:<64 lowercase hex>"')

    created_at = obj["created_at"]
    if not isinstance(created_at, str) or _ISO_Z_RE.fullmatch(created_at) is None:
        raise EntrySchemaError("created_at must be an ISO-8601 UTC timestamp ending in Z")


# --- ledger file access ------------------------------------------------------


def read_ledger(path: Path) -> list[dict[str, Any]]:
    """Read and strictly validate every line of the ledger.

    Every physical line must be one newline-terminated, schema-valid
    JSON entry: a truncated or unterminated tail line, a blank line, bad
    JSON or a contract violation is reported as ``LedgerCorruptionError``
    naming the 1-based line number. An empty file reads as an empty
    ledger; a missing, unreadable or non-UTF-8 file is ``LedgerReadError``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise LedgerReadError(
            f"ledger file is not valid UTF-8: {path}",
            "the ledger is UTF-8 JSONL written by this tool; restore it from a backup",
        ) from e
    except OSError as e:
        raise LedgerReadError(f"cannot read ledger: {e}") from e
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the terminating newline of the final entry
    elif text:
        # A non-empty file whose last line lacks the newline terminator
        # is an interrupted append or a hand edit - corruption either
        # way, and appending after it would merge two entries into one.
        raise LedgerCorruptionError(
            len(lines),
            "last line is not newline-terminated (interrupted append or edited file)",
            "the ledger is append-only; restore it from a backup rather than editing lines",
        )
    entries = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise LedgerCorruptionError(line_number, "blank line in ledger")
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise LedgerCorruptionError(
                line_number,
                f"line is not valid JSON: {e.msg}",
                "the ledger is append-only; restore it from a backup rather than editing lines",
            ) from e
        try:
            validate_entry(obj)
        except EntrySchemaError as e:
            raise LedgerCorruptionError(line_number, e.message, e.hint) from e
        entries.append(obj)
    return entries


def append_entry(path: Path, entry: dict[str, Any]) -> None:
    """Validate ``entry`` then append it as one JSON line; never rewrites.

    The entry is serialized to a single newline-terminated line and
    written through one ``O_APPEND`` ``os.open`` followed by exactly one
    ``os.write`` of the whole line: on ``O_APPEND`` that write lands at
    the current end of file, so concurrent appenders can never
    interleave inside a line. A short write raises ``LedgerWriteError``
    rather than retrying in a loop, because a second write could
    interleave with another appender's line. The file mode is forced to
    0600 on the open descriptor: the ``os.open`` mode applies only to a
    newly created file, while an existing file keeps its mode. When the
    file already exists it is validated first - appending onto a
    corrupt ledger is refused so a bad line can never silently absorb
    a new entry.
    """
    validate_entry(entry)
    if path.exists():
        read_ledger(path)
    data = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # The O_CREAT mode applies only when the file is new; an
            # existing file keeps its mode, so force 0600 on the open
            # descriptor unconditionally.
            os.fchmod(fd, 0o600)
            # Entries are small single JSON lines: issue exactly one
            # write of the complete line, never a retry loop.
            written = os.write(fd, data)
            if written != len(data):
                raise LedgerWriteError(
                    f"short write to ledger: {written} of {len(data)} bytes",
                    "the ledger tail may be truncated; restore it from a backup",
                )
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise LedgerWriteError(
            f"cannot append to ledger: {e}",
            "create the parent directory or pass a writable --ledger path",
        ) from e


# --- CLI ---------------------------------------------------------------------


def _parse_field(pair: str) -> tuple[str, Any]:
    """Split a ``--field KEY=VALUE`` argument into key and value.

    The value parses as JSON when possible (``context_size=128`` gives
    the int 128, ``flag=true`` gives a bool); anything that is not valid
    JSON stays a raw string, so identifiers and timestamps need no
    quoting. A literal string that itself looks like JSON can be forced
    with JSON quoting: ``--field 'project="42"'``.
    """
    if "=" not in pair:
        raise UsageError(
            f"--field expects KEY=VALUE, got {pair!r}",
            "repeat one --field per entry key, for example --field project=demo",
        )
    key, _, raw = pair.partition("=")
    key = key.strip()
    if not key:
        raise UsageError(f"--field expects KEY=VALUE, got {pair!r}")
    raw = raw.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return key, value


def _entry_from_fields(fields: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    for pair in fields:
        key, value = _parse_field(pair)
        if key in entry:
            raise UsageError(f"duplicate --field key: {key}")
        entry[key] = value
    return entry


def _resolve_entry(args: argparse.Namespace) -> dict[str, Any] | None:
    """Build the candidate entry from ``--file`` or ``--field`` arguments."""
    if getattr(args, "file", None):
        path = Path(args.file).expanduser()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as e:
            raise UsageError(
                f"entry file is not valid UTF-8: {path}",
                "convert it to UTF-8 first or pass a UTF-8 file",
            ) from e
        except (OSError, json.JSONDecodeError) as e:
            raise UsageError(f"cannot read entry JSON: {e}") from e
    if getattr(args, "field", None):
        return _entry_from_fields(args.field)
    return None


def _add_ledger_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ledger",
        default=DEFAULT_LEDGER_PATH,
        help=f"ledger file path (default {DEFAULT_LEDGER_PATH})",
    )


def _add_entry_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--file",
        metavar="PATH",
        help="JSON file holding one decision entry object",
    )
    group.add_argument(
        "--field",
        metavar="KEY=VALUE",
        action="append",
        default=None,
        help=(
            "one entry field, repeatable; values parse as JSON when possible "
            "(128 -> int) and stay raw strings otherwise"
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decision_ledger",
        description=(
            "Offline append-only decisions.jsonl ledger of partner "
            "recommendations and the orchestrator action taken on each."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser(
        "append",
        help="validate and append one entry as a single JSON line",
    )
    _add_entry_args(append, required=True)
    _add_ledger_arg(append)

    read = sub.add_parser(
        "read",
        help="validate the whole ledger and print every entry as canonical JSONL",
    )
    _add_ledger_arg(read)

    listing = sub.add_parser(
        "list",
        help="validate the whole ledger and print a compact per-entry summary",
    )
    _add_ledger_arg(listing)

    check = sub.add_parser(
        "validate",
        help="validate the ledger file, or a single entry via --file/--field",
    )
    _add_entry_args(check, required=False)
    _add_ledger_arg(check)
    return parser


def _cmd_append(args: argparse.Namespace) -> int:
    entry = _resolve_entry(args)
    if entry is None:  # unreachable: the append entry group is required
        raise UsageError("append needs --file or --field")
    append_entry(Path(args.ledger).expanduser(), entry)
    print(f"appended {entry['decision_id']}")
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    entries = read_ledger(Path(args.ledger).expanduser())
    for entry in entries:
        sys.stdout.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    entries = read_ledger(Path(args.ledger).expanduser())
    for index, entry in enumerate(entries, start=1):
        print(
            f"{index}\t{entry['created_at']}\t{entry['partner']}\t"
            f"{entry['orchestrator_action']}\t{entry['decision_id']}"
        )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    entry = _resolve_entry(args)
    if entry is not None:
        validate_entry(entry)
        print(f"ok {entry['decision_id']}")
        return 0
    entries = read_ledger(Path(args.ledger).expanduser())
    print(f"ok {len(entries)} entries")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "append":
            return _cmd_append(args)
        if args.command == "read":
            return _cmd_read(args)
        if args.command == "list":
            return _cmd_list(args)
        if args.command == "validate":
            return _cmd_validate(args)
        raise UsageError(f"unknown command: {args.command}")
    except LedgerError as e:
        print(e.to_text(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
