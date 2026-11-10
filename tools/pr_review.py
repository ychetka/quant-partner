#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
"""Build a GitHub PR-review payload from the ``auto-reviewer`` partner.

The tool reads a unified diff (``--diff-file`` or stdin), runs one
advisory ``review_code`` call through the ``auto-reviewer`` partner
instance (Gemini backend, ``review`` toolset) and writes a
``review.json`` payload shaped for
``POST /repos/{owner}/{repo}/pulls/{n}/reviews``.

The published review is advisory only: ``event`` is always ``COMMENT``
and the body opens with a machine-readable ``AUTO_REVIEW_VERDICT:``
line (``APPROVE``, ``REQUEST_CHANGES``, ``ERROR`` or ``DRY_RUN``).
Findings that anchor to RIGHT-side diff lines become inline comments;
everything else is folded into the body so no finding is lost to a
position error.

Failure policy: the tool never prints diff or secret material to
stderr. Partner or parsing failures still produce a valid review.json
describing the failure and exit ``0`` (soft-fail); ``--strict`` turns
them into a non-zero exit instead. ``--dry-run`` skips the partner
call entirely and emits a stub skeleton, which is useful for offline
pipeline checks.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_PARTNERS_DIR = str(ROOT / "partners")
if _PARTNERS_DIR not in sys.path:
    sys.path.insert(0, _PARTNERS_DIR)

from review.diffmap import parse_diff  # noqa: E402

PARTNER_ID = "auto-reviewer"
DEFAULT_MODEL_ALIAS = "gemini"
VERDICT_HEADER = "AUTO_REVIEW_VERDICT"

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")

_DEFAULT_OPTIONS: dict[str, Any] = {
    "max_diff_chars": 200_000,
    "max_context_chars": 50_000,
    "max_findings": 20,
    "allowed_models": {},
}

_SEVERITY_LABELS = {
    "blocker": "[CRITICAL]",
    "major": "[WARNING]",
    "minor": "[MINOR]",
    "nit": "[MINOR]",
}

_MAX_ERROR_CHARS = 300
_MAX_INLINE_BODY_CHARS = 4000

_USAGE_KEYS = ("prompt_tokens", "candidates_tokens", "total_tokens")

# Approximate USD-per-million-token prices used for the est_cost_usd
# estimate only: these are rough figures as of 2026-11-10 and must be
# verified against current Google pricing before being relied on. The
# AI Studio quota page stays the source of truth.
_PRICE_PER_MTOK_USD = {
    "gemini-3.5-flash": {"input": 0.30, "output": 2.50},
}


def load_review_options() -> dict[str, Any]:
    """Return ``options.review`` of the auto-reviewer partner.

    Any configuration failure falls back to built-in defaults so the
    tool still bounds the diff and picks a model alias offline.
    """
    options = dict(_DEFAULT_OPTIONS)
    try:
        from core.config import load_partner_config

        cfg = load_partner_config(PARTNER_ID)
    except Exception:
        return options
    raw = cfg.options.get("review") if isinstance(cfg.options, dict) else None
    if not isinstance(raw, dict):
        return options
    for key in ("max_diff_chars", "max_context_chars", "max_findings"):
        value = raw.get(key)
        if type(value) is int and value > 0:
            options[key] = value
    allowed = raw.get("allowed_models")
    if isinstance(allowed, dict):
        options["allowed_models"] = allowed
    return options


def call_review(arguments: dict[str, Any]) -> str:
    """Run one ``review_code`` call; returns the serialized JSON artifact."""
    from core.server import PartnerMCPServer

    server = PartnerMCPServer(PARTNER_ID)
    return server.call_tool("review_code", arguments)


def _truncate_diff(diff: str, max_chars: int) -> tuple[str, bool]:
    """Bound the diff to ``max_chars``, cutting on a line boundary."""
    if len(diff) <= max_chars:
        return diff, False
    cut = diff[:max_chars].rsplit("\n", 1)[0]
    return cut + "\n", True


def _model_alias(options: dict[str, Any], explicit: str) -> str:
    """Pick the allowlisted model alias for ``review_code``."""
    if explicit:
        return explicit
    allowed = options.get("allowed_models")
    if isinstance(allowed, dict) and allowed:
        return sorted(str(alias) for alias in allowed)[0]
    return DEFAULT_MODEL_ALIAS


def _severity_label(value: Any) -> str:
    return _SEVERITY_LABELS.get(str(value), "[MINOR]")


def _one_line(text: Any, max_chars: int) -> str:
    """Collapse whitespace and bound a string for single-line rendering."""
    cleaned = " ".join(str(text).split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 3].rstrip() + "..."
    return cleaned


def _finding_comment_body(finding: dict[str, Any]) -> str:
    """Severity-prefixed inline comment text, bounded for the API."""
    label = _severity_label(finding.get("severity"))
    title = str(finding.get("title") or "").strip()
    body = str(finding.get("body") or "").strip()
    text = f"{label} {title}\n\n{body}" if body else f"{label} {title or 'finding'}"
    if len(text) > _MAX_INLINE_BODY_CHARS:
        text = text[: _MAX_INLINE_BODY_CHARS - 3].rstrip() + "..."
    return text


def _finding_summary_line(finding: dict[str, Any]) -> str:
    """One-line body rendering of a finding for the review body."""
    label = _severity_label(finding.get("severity"))
    path = str(finding.get("path") or "?")
    line = finding.get("line")
    anchor = f"`{path}:{line}`" if type(line) is int else f"`{path}`"
    title = _one_line(finding.get("title") or "", 120)
    body = _one_line(finding.get("body") or "", 300)
    detail = f"{title} - {body}" if title and body else (title or body or "finding")
    return f"- {label} {anchor} - {detail}"


def _payload(commit_id: str, verdict: str, sections: list[str]) -> dict[str, Any]:
    body = "\n\n".join([f"{VERDICT_HEADER}: {verdict}", *sections])
    return {"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": []}


def _failure_payload(commit_id: str, reason: str) -> dict[str, Any]:
    """Advisory payload emitted when no usable artifact exists."""
    sections = [
        f"Automated review did not complete: {_one_line(reason, _MAX_ERROR_CHARS)}",
        "This review is advisory only; a missing review never blocks the merge.",
    ]
    return _payload(commit_id, "ERROR", sections)


def _dry_run_payload(commit_id: str) -> dict[str, Any]:
    sections = [
        "Dry run: the partner call was skipped and this stub verdict "
        "is not a real review. Run without `--dry-run` for a reviewed result."
    ]
    return _payload(commit_id, "DRY_RUN", sections)


def _empty_diff_payload(commit_id: str) -> dict[str, Any]:
    sections = ["The supplied diff is empty; there is nothing to review."]
    return _payload(commit_id, "APPROVE", sections)


def _artifact_error_reason(artifact: dict[str, Any]) -> str:
    """Flatten artifact error entries into one bounded, safe reason line."""
    parts = []
    errors = artifact.get("errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict):
                code = str(entry.get("code") or "error")
                message = str(entry.get("message") or "")
                parts.append(f"{code}: {message}" if message else code)
    if parts:
        return "; ".join(parts)
    status = artifact.get("status")
    return f"review pipeline returned status {status!r}"


def _artifact_payload(
    commit_id: str,
    artifact: dict[str, Any],
    diff_lines_map: Any,
    notes: list[str],
) -> dict[str, Any]:
    """Translate a ``status=ok`` advisory artifact into a review payload."""
    verdict = "REQUEST_CHANGES" if artifact.get("verdict") == "needs_changes" else "APPROVE"
    comments: list[dict[str, Any]] = []
    anchored: list[str] = []
    relocated: list[str] = []
    findings = artifact.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            path = finding.get("path")
            line = finding.get("line")
            end_line = finding.get("end_line", line)
            anchorable = (
                isinstance(path, str)
                and type(line) is int
                and type(end_line) is int
                and diff_lines_map.contains(path, line, end_line)
            )
            if anchorable:
                comment: dict[str, Any] = {
                    "path": path,
                    "line": end_line,
                    "side": "RIGHT",
                    "body": _finding_comment_body(finding),
                }
                if end_line > line:
                    comment["start_line"] = line
                    comment["start_side"] = "RIGHT"
                comments.append(comment)
                anchored.append(_finding_summary_line(finding))
            else:
                relocated.append(_finding_summary_line(finding))

    sections: list[str] = []
    summary = str(artifact.get("summary") or "").strip()
    if summary:
        sections.append(summary)
    if anchored:
        sections.append("## Findings\n\n" + "\n".join(anchored))
    if relocated:
        sections.append(
            "## Findings outside the diff\n\n"
            "These could not be anchored to a changed line, so they are "
            "listed here instead of as inline comments:\n\n" + "\n".join(relocated)
        )

    note_lines = list(notes)
    dropped = artifact.get("dropped_findings")
    if isinstance(dropped, list) and dropped:
        note_lines.append(f"{len(dropped)} model finding(s) failed validation and were dropped")
    unreviewed = artifact.get("unreviewed")
    if isinstance(unreviewed, list) and unreviewed:
        paths = ", ".join(f"`{_one_line(p, 200)}`" for p in unreviewed[:20])
        note_lines.append(f"Unreviewed paths (binary, generated or unanchorable): {paths}")
    warnings = artifact.get("warnings")
    if isinstance(warnings, list) and warnings:
        joined = "; ".join(_one_line(w, 200) for w in warnings[:10])
        note_lines.append(f"Reviewer warnings: {joined}")
    if note_lines:
        sections.append("## Notes\n\n" + "\n".join(f"- {line}" for line in note_lines))

    payload = _payload(commit_id, verdict, sections)
    payload["comments"] = comments
    return payload


def _usage_counts(usage: Any) -> dict[str, int] | None:
    """Int token counters from an artifact ``usage`` object; ``None`` when absent."""
    if not isinstance(usage, dict):
        return None
    counts = {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if type(value) is int and value >= 0:
            counts[key] = value
    return counts or None


def _artifact_model(artifact: dict[str, Any]) -> str:
    """Provider-served model id for pricing, falling back to the requested one."""
    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict):
        return ""
    for key in ("model_used", "model_requested"):
        value = provenance.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _estimate_cost_usd(model: Any, counts: dict[str, int]) -> float | None:
    """Rough USD estimate for one run; ``None`` when the model is not priced."""
    if not isinstance(model, str) or not model:
        return None
    price = _PRICE_PER_MTOK_USD.get(model)
    if price is None:
        return None
    return (
        counts.get("prompt_tokens", 0) * price["input"]
        + counts.get("candidates_tokens", 0) * price["output"]
    ) / 1_000_000


def _usage_summary_markdown(review: dict[str, Any]) -> str:
    """GitHub step-summary markdown table for a review.json usage block."""
    usage = review.get("usage")
    counts = _usage_counts(usage)
    lines = [
        "### Auto-review usage",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    if counts is None:
        lines.append("| Usage | not reported by the provider |")
        return "\n".join(lines) + "\n"
    model = usage.get("model") if isinstance(usage, dict) else None
    if isinstance(model, str) and model:
        lines.append(f"| Model | `{model}` |")
    cost = _estimate_cost_usd(model, counts)
    lines.append(f"| Input tokens | {counts.get('prompt_tokens', 0)} |")
    lines.append(f"| Output tokens | {counts.get('candidates_tokens', 0)} |")
    lines.append(f"| Total tokens | {counts.get('total_tokens', 0)} |")
    if cost is None:
        lines.append("| Est. cost | unknown (model not in price table) |")
    else:
        lines.append(f"| Est. cost | ${cost:.4f} USD |")
    return "\n".join(lines) + "\n"


def _usage_summary_main(path: str) -> int:
    """Print the step-summary markdown for a review.json file; always exits 0."""
    try:
        review = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        review = {}
    sys.stdout.write(_usage_summary_markdown(review if isinstance(review, dict) else {}))
    return 0


def _read_diff(args: argparse.Namespace) -> str | None:
    """Read the unified diff from --diff-file or stdin; None on failure."""
    try:
        if args.diff_file:
            return Path(args.diff_file).read_text(encoding="utf-8", errors="replace")
        return sys.stdin.read()
    except OSError:
        return None


def _write_payload(path: str, payload: dict[str, Any]) -> bool:
    try:
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return True
    except OSError:
        return False


def _stderr_line(reason: str) -> None:
    """One sanitized stderr line; never carries diff or secret material."""
    print(f"auto-review: {_one_line(reason, _MAX_ERROR_CHARS)}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the auto-reviewer partner over a unified diff and write a "
            "GitHub PR review payload (event COMMENT, AUTO_REVIEW_VERDICT header)."
        )
    )
    parser.add_argument(
        "--diff-file",
        default="",
        help="Unified diff file to review; stdin is used when omitted",
    )
    parser.add_argument(
        "--commit-id",
        default="",
        help=(
            "PR head commit SHA (40 hex), written into review.json as commit_id; "
            "required unless --dry-run"
        ),
    )
    parser.add_argument(
        "--base-sha",
        default="",
        help="PR base commit SHA (40 hex); defaults to --commit-id",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=0,
        help="Optional pull request number, passed to the partner as context",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Allowlisted model alias; defaults to the single configured alias",
    )
    parser.add_argument(
        "--skill",
        default="",
        help="Optional installed review skill name (default: configured default_skill)",
    )
    parser.add_argument(
        "--out",
        default="review.json",
        help="Output path for the review payload (default: review.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the partner call and emit a stub review.json skeleton",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on partner failure (default: soft-fail with exit 0)",
    )
    parser.add_argument(
        "--usage-summary",
        default="",
        metavar="REVIEW_JSON",
        help=(
            "Read a review.json and print a GitHub step-summary markdown "
            "usage/cost table to stdout, then exit 0"
        ),
    )
    args = parser.parse_args(argv)

    if args.usage_summary:
        return _usage_summary_main(args.usage_summary)

    commit_id = args.commit_id
    if not commit_id:
        if not args.dry_run:
            _stderr_line("--commit-id is required (only --dry-run may omit it)")
            return 2
        commit_id = "0" * 40
    elif not _SHA_RE.fullmatch(commit_id):
        _stderr_line("--commit-id must be a 40-hex SHA")
        return 2
    base_sha = args.base_sha or commit_id
    if not _SHA_RE.fullmatch(base_sha):
        _stderr_line("--base-sha must be a 40-hex SHA")
        return 2

    diff = _read_diff(args)
    if diff is None:
        _stderr_line("cannot read the diff input")
        return 2

    options = load_review_options()
    sent_diff, truncated = _truncate_diff(diff, int(options["max_diff_chars"]))
    notes: list[str] = []
    if truncated:
        notes.append(
            f"Diff truncated to {len(sent_diff)} of {len(diff)} characters "
            "(max_diff_chars); findings may cover only the shown portion."
        )

    if args.dry_run:
        payload = _dry_run_payload(commit_id)
    elif not sent_diff.strip():
        payload = _empty_diff_payload(commit_id)
    else:
        context = "Automated pull request review"
        if args.pr_number:
            context += f" of PR #{args.pr_number}"
        arguments: dict[str, Any] = {
            "head_sha": commit_id.lower(),
            "base_sha": base_sha.lower(),
            "diff": sent_diff,
            "model": _model_alias(options, args.model),
            "context": context + ".",
        }
        if args.skill:
            arguments["skill"] = args.skill

        artifact: dict[str, Any] | None = None
        failure_reason = ""
        try:
            raw_text = call_review(arguments)
        except Exception as e:
            failure_reason = f"partner call failed ({type(e).__name__})"
        else:
            try:
                parsed = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError):
                failure_reason = "partner returned an unparseable artifact"
            else:
                if isinstance(parsed, dict):
                    artifact = parsed
                else:
                    failure_reason = "partner returned a non-object artifact"

        if artifact is not None and artifact.get("status") == "ok":
            payload = _artifact_payload(commit_id, artifact, parse_diff(sent_diff), notes)
            # Provider-reported token counters only - never diff or
            # prompt text. The review.json usage block mirrors the
            # artifact counters and adds the resolved model so the
            # workflow summary can price the run; unknown extra keys are
            # ignored by the GitHub reviews API (and the body-only 422
            # retry below covers the paranoid case).
            counts = _usage_counts(artifact.get("usage"))
            model = _artifact_model(artifact)
            if counts is not None:
                payload["usage"] = dict(counts)
                if model:
                    payload["usage"]["model"] = model
                payload["body"] += (
                    f"\n\nUsage: in {counts.get('prompt_tokens', 0)} / "
                    f"out {counts.get('candidates_tokens', 0)} / "
                    f"total {counts.get('total_tokens', 0)} tokens"
                )
            report = counts or {}
            # est_cost_usd stays "unknown" whenever the provider reported
            # no usage or the served model is absent from the price table.
            cost = _estimate_cost_usd(model, report) if counts is not None else None
            est = f"{cost:.4f}" if cost is not None else "unknown"
            print(
                "auto_review_usage "
                f"prompt_tokens={report.get('prompt_tokens', 0)} "
                f"candidates_tokens={report.get('candidates_tokens', 0)} "
                f"total_tokens={report.get('total_tokens', 0)} "
                f"est_cost_usd={est}",
                file=sys.stderr,
            )
        else:
            if artifact is not None:
                failure_reason = _artifact_error_reason(artifact)
            if notes:
                failure_reason = failure_reason + "; " + " ".join(notes)
            payload = _failure_payload(commit_id, failure_reason or "unknown failure")
            _stderr_line(failure_reason or "review pipeline failed")
            if not _write_payload(args.out, payload):
                _stderr_line(f"cannot write {args.out}")
                return 2
            return 1 if args.strict else 0

    if not _write_payload(args.out, payload):
        _stderr_line(f"cannot write {args.out}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
