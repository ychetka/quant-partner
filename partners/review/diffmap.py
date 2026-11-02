#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Parse unified diffs into per-file RIGHT-side line maps.

Only RIGHT-side (new file) line numbers are tracked, because findings
anchor to the head revision. Hunk body counts are validated against the
``@@ -a,b +c,d @@`` declarations; a file whose counts do not add up is
treated as unanchorable so findings can never bind to a misparsed diff.

Generated outputs are unanchorable too: reviewing machine-written code
line-by-line wastes findings, so sections carrying an explicit
generated-code marker or a clearly generated file name are marked
``is_generated`` and surface in ``unreviewed``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TOKEN_RE = re.compile(r'"((?:\\.|[^"\\])*)"|(\S+)')
_C_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "a": "\a",
}
_MAX_SPAN = 10_000

_EXTENDED_HEADER_PREFIXES = (
    "index ",
    "old mode",
    "new mode",
    "new file mode",
    "deleted file mode",
    "similarity index",
    "dissimilarity index",
)

# Explicit generated-code markers searched (case-insensitively) inside
# hunk body lines. The list covers the common generator banners: Go's
# "Code generated ... DO NOT EDIT.", protobuf, GraphQL codegen, and
# generic auto-generated notices.
_GENERATED_MARKERS = (
    "code generated",
    "do not edit",
    "@generated",
    "auto-generated",
    "automatically generated",
)

# File names that conventionally carry generated output; basenames are
# compared lowercased.
_GENERATED_BASENAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.lock",
        "go.sum",
    }
)


def _generated_name(path: str) -> bool:
    """True for file names that conventionally carry generated output."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in _GENERATED_BASENAMES:
        return True
    if ".generated." in name or ".pb." in name:
        return True
    return name.endswith(".min.js")


def _generated_marker(line_content: str) -> bool:
    """True when a hunk body line carries an explicit generated marker."""
    lowered = line_content.lower()
    return any(marker in lowered for marker in _GENERATED_MARKERS)


def _c_unescape(text: str) -> str:
    """Decode the C-style escapes git uses inside quoted diff paths."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            out.append(char)
            index += 1
            continue
        nxt = text[index + 1]
        if nxt in "01234567":
            end = index + 1
            while end < len(text) and end < index + 4 and text[end] in "01234567":
                end += 1
            out.append(chr(int(text[index + 1 : end], 8)))
            index = end
            continue
        out.append(_C_ESCAPES.get(nxt, nxt))
        index += 2
    return "".join(out)


def _split_tokens(text: str) -> list[str]:
    """Split a ``diff --git`` tail into path tokens honoring quoted strings."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        if match.group(1) is not None:
            tokens.append(_c_unescape(match.group(1)))
        else:
            tokens.append(match.group(2))
    return tokens


def _strip_prefix(path: str) -> str:
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _header_path(rest: str) -> str:
    """Extract the path token from a ``---``/``+++``/``rename`` header line."""
    rest = rest.strip()
    if not rest or rest == "/dev/null":
        return rest
    if rest.startswith('"'):
        match = _TOKEN_RE.match(rest)
        if match is not None and match.group(1) is not None:
            return _c_unescape(match.group(1))
        return rest
    return rest.split("\t")[0].rstrip()


@dataclass
class FileDiff:
    """One parsed file section of a unified diff."""

    new_path: str = ""
    old_path: str = ""
    is_new: bool = False
    is_deleted: bool = False
    is_rename: bool = False
    is_binary: bool = False
    is_generated: bool = False
    valid: bool = True
    saw_hunks: bool = False
    saw_plus: bool = False
    added: set[int] = field(default_factory=set)
    context: set[int] = field(default_factory=set)

    @property
    def reported_path(self) -> str:
        """Path used in ``unreviewed`` and finding anchors."""
        return self.old_path if self.is_deleted else self.new_path

    @property
    def right_lines(self) -> set[int]:
        return self.added | self.context

    @property
    def anchorable(self) -> bool:
        """True when RIGHT-side lines can safely anchor findings."""
        return (
            self.valid
            and not self.is_deleted
            and not self.is_binary
            and not self.is_generated
            and bool(self.right_lines)
        )


class DiffMap:
    """Mapping of diff paths to their RIGHT-side line sets."""

    def __init__(self) -> None:
        self.files: dict[str, FileDiff] = {}

    def add(self, file_diff: FileDiff) -> None:
        path = file_diff.reported_path
        if not path:
            return
        if path in self.files:
            # Duplicate sections for one path are ambiguous: refuse anchoring.
            self.files[path].valid = False
            return
        self.files[path] = file_diff

    def get(self, path: str) -> FileDiff | None:
        return self.files.get(path)

    def anchorable(self, path: str) -> bool:
        file_diff = self.files.get(path)
        return file_diff is not None and file_diff.anchorable

    def right_lines(self, path: str) -> set[int]:
        file_diff = self.files.get(path)
        if file_diff is None or not file_diff.anchorable:
            return set()
        return file_diff.right_lines

    def contains(self, path: str, line: int, end_line: int) -> bool:
        """True when every RIGHT-side line in ``[line, end_line]`` is in the diff."""
        if end_line < line or end_line - line > _MAX_SPAN:
            return False
        lines = self.right_lines(path)
        if not lines:
            return False
        return all(number in lines for number in range(line, end_line + 1))

    def unreviewed(self) -> list[str]:
        """Sorted paths present in the diff that cannot anchor findings."""
        return sorted(path for path, file_diff in self.files.items() if not file_diff.anchorable)


def _diff_lines(text: str) -> list[str]:
    """Split on literal ``\\n`` only, stripping one trailing ``\\r``.

    Unlike ``str.splitlines``, Unicode line separators (U+2028, U+2029,
    U+0085, ...) stay inside the line content, so they cannot fabricate
    extra hunk body lines and corrupt the declared hunk counts. A single
    trailing ``\\r`` is removed so CRLF input parses like LF input.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":
        # A terminating newline ends the last line; it is not an extra one.
        lines.pop()
    return lines


def parse_diff(text: str) -> DiffMap:
    """Parse a unified diff string into a :class:`DiffMap`."""
    result = DiffMap()
    if not isinstance(text, str) or not text.strip():
        return result

    current: FileDiff | None = None
    in_hunk = False
    expected_left = expected_right = seen_left = seen_right = 0
    left_no = right_no = 0

    def hunk_done() -> bool:
        return seen_left >= expected_left and seen_right >= expected_right

    def close_hunk() -> None:
        nonlocal in_hunk
        if in_hunk and current is not None:
            # Exact counts are the only valid ending: under-fed hunks are
            # incomplete and over-fed hunks prove the parse drifted.
            if seen_left != expected_left or seen_right != expected_right:
                current.valid = False
        in_hunk = False

    def close_file() -> None:
        nonlocal current
        close_hunk()
        if current is not None:
            if _generated_name(current.reported_path):
                current.is_generated = True
            result.add(current)
        current = None

    for line in _diff_lines(text):
        if line.startswith("diff --git "):
            close_file()
            current = FileDiff()
            tokens = _split_tokens(line[len("diff --git ") :])
            if len(tokens) >= 2:
                current.old_path = _strip_prefix(tokens[0])
                current.new_path = _strip_prefix(tokens[1])
            elif len(tokens) == 1:
                current.new_path = _strip_prefix(tokens[0])
            else:
                current.valid = False
            continue

        if current is None:
            if line.startswith("--- "):
                current = FileDiff(old_path=_strip_prefix(_header_path(line[4:])))
            continue

        if in_hunk and hunk_done():
            # Declared counts are satisfied, so the hunk ends here no
            # matter what the line looks like. Header-looking lines bound
            # files or hunks below (non-git diffs chain files with
            # "--- "/"+++ " pairs); a body line past the counts is stray
            # content outside a hunk and marks the file invalid instead
            # of fabricating RIGHT lines.
            close_hunk()

        if in_hunk:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file" marker consumes nothing
            if line and _generated_marker(line[1:]):
                current.is_generated = True
            if line == "":
                # Some producers drop the leading space on empty context lines.
                current.context.add(right_no)
                right_no += 1
                seen_left += 1
                seen_right += 1
                continue
            marker = line[0]
            if marker == " ":
                current.context.add(right_no)
                right_no += 1
                seen_left += 1
                seen_right += 1
            elif marker == "+":
                current.added.add(right_no)
                right_no += 1
                seen_right += 1
            elif marker == "-":
                left_no += 1
                seen_left += 1
            else:
                current.valid = False
            continue

        if line.startswith("@@ "):
            match = _HUNK_RE.match(line)
            if match is None:
                current.valid = False
                continue
            left_no = int(match.group(1))
            expected_left = int(match.group(2)) if match.group(2) else 1
            right_no = int(match.group(3))
            expected_right = int(match.group(4)) if match.group(4) else 1
            seen_left = seen_right = 0
            in_hunk = True
            current.saw_hunks = True
            continue
        if line.startswith("--- "):
            if current.saw_plus or current.saw_hunks:
                close_file()
                current = FileDiff()
            old_path = _header_path(line[4:])
            if old_path == "/dev/null":
                current.is_new = True
            else:
                current.old_path = _strip_prefix(old_path)
            continue
        if line.startswith("+++ ") and not current.saw_hunks:
            current.saw_plus = True
            new_path = _header_path(line[4:])
            if new_path == "/dev/null":
                current.is_deleted = True
            else:
                current.new_path = _strip_prefix(new_path)
            continue
        if line.startswith("new file mode"):
            current.is_new = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if line.startswith("rename from "):
            current.is_rename = True
            current.old_path = _header_path(line[len("rename from ") :])
            continue
        if line.startswith("rename to "):
            current.is_rename = True
            current.new_path = _header_path(line[len("rename to ") :])
            continue
        if line.startswith("copy to "):
            current.new_path = _header_path(line[len("copy to ") :])
            continue
        if line.startswith(("Binary files ", "GIT binary patch")):
            current.is_binary = True
            continue
        if line.startswith(_EXTENDED_HEADER_PREFIXES) or line.startswith("copy from "):
            continue
        if line == "" or line.startswith("\\"):
            continue
        if line[0] in " +-" and current.saw_hunks:
            # Stray hunk body content outside a declared hunk: refuse anchoring.
            current.valid = False
            continue
        # Unknown extended-header lines are ignored conservatively.

    close_file()
    return result
