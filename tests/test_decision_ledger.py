#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DECISION_LEDGER = ROOT / "tools" / "decision_ledger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("decision_ledger", DECISION_LEDGER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {DECISION_LEDGER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dl = load_module()

VALID_HASH = "sha256:" + "a" * 64

# A Latin-1 byte sequence that is invalid UTF-8 (0xe9 is 'é' there).
LATIN1 = b"Caf\xe9\n"


def _entry(**overrides):
    entry = {
        "decision_id": "d-001",
        "project": "quant-partner",
        "partner": "strategy-partner",
        "context_size": 128,
        "context_hash": VALID_HASH,
        "recommendation_summary": "prefer local ASR over hosted transcription",
        "orchestrator_action": "adopted",
        "created_at": "2026-11-08T10:30:00Z",
        "schema_version": 1,
    }
    entry.update(overrides)
    return entry


def _without(entry, *keys):
    return {k: v for k, v in entry.items() if k not in keys}


def _line(entry):
    return json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"


class EntryValidationTest(unittest.TestCase):
    def test_valid_entry_passes(self):
        self.assertIsNone(dl.validate_entry(_entry()))

    def test_context_size_or_hash_at_least_one_required(self):
        # Each fingerprint alone satisfies the contract; both together
        # are fine; neither is rejected.
        self.assertIsNone(dl.validate_entry(_without(_entry(), "context_hash")))
        self.assertIsNone(dl.validate_entry(_without(_entry(), "context_size")))
        self.assertIsNone(dl.validate_entry(_entry()))
        with self.assertRaises(dl.EntrySchemaError) as ctx:
            dl.validate_entry(_without(_entry(), "context_size", "context_hash"))
        self.assertEqual(ctx.exception.code, "entry_schema_invalid")

    def test_context_size_contract(self):
        for bad in (-1, True, "128", 12.5, None):
            with self.subTest(context_size=bad):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_entry(context_size=bad))
        self.assertIsNone(dl.validate_entry(_entry(context_size=0)))

    def test_context_hash_contract(self):
        for bad in ("deadbeef", "sha256:" + "A" * 64, "sha256:xyz", 42, None):
            with self.subTest(context_hash=bad):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_entry(context_hash=bad))

    def test_missing_required_keys_rejected(self):
        for key in (
            "decision_id",
            "project",
            "partner",
            "recommendation_summary",
            "orchestrator_action",
            "created_at",
            "schema_version",
        ):
            with self.subTest(missing=key):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_without(_entry(), key))

    def test_unknown_keys_rejected(self):
        for extra in ("outcome", "notes", "updated_at"):
            with self.subTest(extra=extra):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_entry(**{extra: "x"}))

    def test_non_object_rejected(self):
        for bad in ("not a dict", ["d-001"], 7, None):
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(bad)

    def test_empty_and_typed_string_fields_rejected(self):
        for key in ("decision_id", "project", "partner", "recommendation_summary"):
            for bad in ("", "   ", 7, None):
                with self.subTest(key=key, bad=repr(bad)):
                    with self.assertRaises(dl.EntrySchemaError):
                        dl.validate_entry(_entry(**{key: bad}))

    def test_orchestrator_action_enum_rejected(self):
        for bad in ("taken", "ADOPTED", "", 3, None, ["adopted"]):
            with self.subTest(action=repr(bad)):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_entry(orchestrator_action=bad))
        for good in dl.ORCHESTRATOR_ACTIONS:
            self.assertIsNone(dl.validate_entry(_entry(orchestrator_action=good)))

    def test_created_at_contract(self):
        for bad in (
            "2026-11-08 10:30:00",
            "2026-11-08T10:30:00+00:00",
            "2026-11-08",
            0,
            None,
        ):
            with self.subTest(created_at=bad):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_entry(created_at=bad))
        self.assertIsNone(dl.validate_entry(_entry(created_at="2026-11-08T10:30:00.5Z")))

    def test_incompatible_schema_version_rejected(self):
        for bad in (2, 0, -1, 99):
            with self.subTest(version=bad):
                with self.assertRaises(dl.UnsupportedSchemaVersion) as ctx:
                    dl.validate_entry(_entry(schema_version=bad))
                self.assertEqual(ctx.exception.code, "unsupported_schema_version")

    def test_non_integer_schema_version_rejected(self):
        for bad in ("1", 1.0, True, None):
            with self.subTest(version=bad):
                with self.assertRaises(dl.EntrySchemaError):
                    dl.validate_entry(_entry(schema_version=bad))

    def test_error_hierarchy(self):
        self.assertTrue(issubclass(dl.UnsupportedSchemaVersion, dl.EntrySchemaError))
        for cls in (
            dl.UsageError,
            dl.EntrySchemaError,
            dl.LedgerReadError,
            dl.LedgerCorruptionError,
            dl.LedgerWriteError,
        ):
            self.assertTrue(issubclass(cls, dl.LedgerError))


class LedgerAppendReadTest(unittest.TestCase):
    def test_gwt1_valid_append_writes_one_valid_line_and_read_returns_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            dl.append_entry(path, _entry())
            raw = path.read_text(encoding="utf-8")
            # Exactly one newline-terminated JSON line was appended.
            self.assertEqual(raw.count("\n"), 1)
            self.assertTrue(raw.endswith("\n"))
            stored = json.loads(raw)
            dl.validate_entry(stored)
            entries = dl.read_ledger(path)
            self.assertEqual(entries, [_entry()])

    def test_new_ledger_created_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            dl.append_entry(path, _entry())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_multi_append_read_returns_all_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            first = _entry()
            second = _entry(
                decision_id="d-002",
                partner="comms-partner",
                orchestrator_action="rejected",
                created_at="2026-11-09T09:00:00Z",
            )
            third = _without(
                _entry(decision_id="d-003", orchestrator_action="modified"),
                "context_hash",
            )
            for entry in (first, second, third):
                dl.append_entry(path, entry)
            self.assertEqual(dl.read_ledger(path), [first, second, third])
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                _line(first) + _line(second) + _line(third),
            )

    def test_gwt2_invalid_entry_fails_named_and_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            dl.append_entry(path, _entry())
            before = path.read_bytes()
            for bad in (
                _without(_entry(decision_id="d-002"), "orchestrator_action"),
                _entry(decision_id="d-002", orchestrator_action="taken"),
            ):
                with self.subTest(bad=repr(bad)[:60]):
                    with self.assertRaises(dl.EntrySchemaError):
                        dl.append_entry(path, bad)
            # A failed append is a true no-op: existing bytes untouched.
            self.assertEqual(path.read_bytes(), before)

    def test_failed_append_to_new_ledger_creates_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            with self.assertRaises(dl.EntrySchemaError):
                dl.append_entry(path, _without(_entry(), "orchestrator_action"))
            self.assertFalse(path.exists())

    def test_empty_ledger_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text("", encoding="utf-8")
            self.assertEqual(dl.read_ledger(path), [])

    def test_gwt3_truncated_last_line_reports_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            dl.append_entry(path, _entry())
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"decision_id": "d-002", "proj')
            with self.assertRaises(dl.LedgerCorruptionError) as ctx:
                dl.read_ledger(path)
            self.assertEqual(ctx.exception.code, "ledger_corrupt")
            self.assertEqual(ctx.exception.line_number, 2)
            self.assertIn("line 2", ctx.exception.to_text())

    def test_unterminated_last_line_is_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            dl.append_entry(path, _entry())
            dl.append_entry(path, _entry(decision_id="d-002"))
            # Valid JSON but no newline terminator: an interrupted write.
            raw = path.read_bytes()
            path.write_bytes(raw[:-1])
            with self.assertRaises(dl.LedgerCorruptionError) as ctx:
                dl.read_ledger(path)
            self.assertEqual(ctx.exception.line_number, 2)
            self.assertIn("not newline-terminated", str(ctx.exception))

    def test_corrupt_middle_line_reports_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                _line(_entry()) + "{corrupt\n" + _line(_entry(decision_id="d-003")),
                encoding="utf-8",
            )
            with self.assertRaises(dl.LedgerCorruptionError) as ctx:
                dl.read_ledger(path)
            self.assertEqual(ctx.exception.line_number, 2)
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_schema_invalid_line_reports_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            bad_line = json.dumps(_without(_entry(decision_id="d-002"), "project"))
            path.write_text(_line(_entry()) + bad_line + "\n", encoding="utf-8")
            with self.assertRaises(dl.LedgerCorruptionError) as ctx:
                dl.read_ledger(path)
            self.assertEqual(ctx.exception.line_number, 2)
            self.assertIn("project", str(ctx.exception))

    def test_blank_line_is_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(_line(_entry()) + "\n" + _line(_entry()), encoding="utf-8")
            with self.assertRaises(dl.LedgerCorruptionError) as ctx:
                dl.read_ledger(path)
            self.assertEqual(ctx.exception.line_number, 2)

    def test_missing_and_non_utf8_ledger_named_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "decisions.jsonl"
            with self.assertRaises(dl.LedgerReadError) as ctx:
                dl.read_ledger(missing)
            self.assertEqual(ctx.exception.code, "ledger_read_failed")
            bad = Path(tmp) / "latin1.jsonl"
            bad.write_bytes(LATIN1)
            with self.assertRaises(dl.LedgerReadError):
                dl.read_ledger(bad)

    def test_append_refused_onto_corrupt_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(_line(_entry()) + "{corrupt\n", encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(dl.LedgerCorruptionError):
                dl.append_entry(path, _entry(decision_id="d-002"))
            self.assertEqual(path.read_bytes(), before)

    def test_no_update_path_in_module_source(self):
        # The append-only guarantee is structural: no code path opens the
        # ledger in a mode that can rewrite existing bytes.
        source = DECISION_LEDGER.read_text(encoding="utf-8")
        for forbidden in ("O_TRUNC", "O_RDWR", '"r+"', "'r+'", '"w"', "'w'"):
            self.assertNotIn(forbidden, source)


class FieldParsingTest(unittest.TestCase):
    def test_field_values_parse_as_json_then_raw_string(self):
        entry = dl._entry_from_fields(
            [
                "decision_id=d-001",
                "project=quant-partner",
                "partner=strategy-partner",
                "context_size=128",
                f"context_hash={VALID_HASH}",
                "recommendation_summary=prefer local ASR",
                "orchestrator_action=adopted",
                "created_at=2026-11-08T10:30:00Z",
                "schema_version=1",
            ]
        )
        self.assertEqual(entry["context_size"], 128)
        self.assertIsInstance(entry["context_size"], int)
        self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(entry["context_hash"], VALID_HASH)
        self.assertEqual(entry["decision_id"], "d-001")
        dl.validate_entry(entry)

    def test_field_malformed_and_duplicate_rejected(self):
        with self.assertRaises(dl.UsageError):
            dl._parse_field("no-equals-sign")
        with self.assertRaises(dl.UsageError):
            dl._entry_from_fields(["project=a", "project=b"])

    def test_field_json_quoted_string(self):
        key, value = dl._parse_field('project="42"')
        self.assertEqual((key, value), ("project", "42"))


class CliTest(unittest.TestCase):
    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", err),
        ):
            code = dl.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _fields(self, **overrides):
        entry = _entry(**overrides)
        return [arg for key, value in entry.items() for arg in ("--field", f"{key}={value}")]

    def test_append_file_then_read_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            entry_file = Path(tmp) / "entry.json"
            entry_file.write_text(json.dumps(_entry()), encoding="utf-8")
            code, out, err = self._run_main(
                ["append", "--file", str(entry_file), "--ledger", str(ledger)]
            )
            self.assertEqual(code, 0, err)
            self.assertIn("appended d-001", out)
            code, out, err = self._run_main(["read", "--ledger", str(ledger)])
            self.assertEqual(code, 0, err)
            (entry,) = [json.loads(line) for line in out.splitlines()]
            dl.validate_entry(entry)
            self.assertEqual(entry["decision_id"], "d-001")

    def test_append_fields_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            code, _, err = self._run_main(["append", *self._fields(), "--ledger", str(ledger)])
            self.assertEqual(code, 0, err)
            entries = dl.read_ledger(ledger)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["context_size"], 128)
            self.assertEqual(stat.S_IMODE(ledger.stat().st_mode), 0o600)

    def test_gwt2_cli_invalid_action_named_error_ledger_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            code, _, err = self._run_main(["append", *self._fields(), "--ledger", str(ledger)])
            self.assertEqual(code, 0, err)
            before = ledger.read_bytes()
            missing_action = _entry(decision_id="d-002")
            del missing_action["orchestrator_action"]
            for fields in (
                self._fields(decision_id="d-002", orchestrator_action="taken"),
                [
                    arg
                    for key, value in missing_action.items()
                    for arg in ("--field", f"{key}={value}")
                ],
            ):
                with self.subTest(fields=fields[:4]):
                    code, out, err = self._run_main(["append", *fields, "--ledger", str(ledger)])
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("entry_schema_invalid", err)
            self.assertEqual(ledger.read_bytes(), before)

    def test_gwt3_cli_read_reports_corrupt_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            ledger.write_text(_line(_entry()) + '{"decision_id": "d-00', encoding="utf-8")
            code, out, err = self._run_main(["read", "--ledger", str(ledger)])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("ledger_corrupt", err)
            self.assertIn("line 2", err)

    def test_list_prints_compact_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            dl.append_entry(ledger, _entry())
            dl.append_entry(
                ledger,
                _entry(
                    decision_id="d-002",
                    partner="comms-partner",
                    orchestrator_action="modified",
                ),
            )
            code, out, err = self._run_main(["list", "--ledger", str(ledger)])
            self.assertEqual(code, 0, err)
            rows = out.splitlines()
            self.assertEqual(len(rows), 2)
            self.assertIn("strategy-partner", rows[0])
            self.assertIn("adopted", rows[0])
            self.assertIn("comms-partner", rows[1])
            self.assertIn("modified", rows[1])
            self.assertIn("d-002", rows[1])

    def test_validate_ledger_and_single_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            dl.append_entry(ledger, _entry())
            dl.append_entry(ledger, _entry(decision_id="d-002"))
            code, out, err = self._run_main(["validate", "--ledger", str(ledger)])
            self.assertEqual(code, 0, err)
            self.assertIn("ok 2 entries", out)
            entry_file = Path(tmp) / "entry.json"
            entry_file.write_text(json.dumps(_entry()), encoding="utf-8")
            code, out, err = self._run_main(["validate", "--file", str(entry_file)])
            self.assertEqual(code, 0, err)
            self.assertIn("ok d-001", out)

    def test_validate_rejects_bad_entry_file_and_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry_file = Path(tmp) / "entry.json"
            entry_file.write_text(json.dumps(_entry(schema_version=2)), encoding="utf-8")
            code, _, err = self._run_main(["validate", "--file", str(entry_file)])
            self.assertEqual(code, 1)
            self.assertIn("unsupported_schema_version", err)
            ledger = Path(tmp) / "decisions.jsonl"
            ledger.write_text("{corrupt\n", encoding="utf-8")
            code, _, err = self._run_main(["validate", "--ledger", str(ledger)])
            self.assertEqual(code, 1)
            self.assertIn("ledger_corrupt", err)
            self.assertIn("line 1", err)

    def test_append_unwritable_path_named_error(self):
        code, out, err = self._run_main(
            [
                "append",
                *self._fields(),
                "--ledger",
                "/nonexistent-dir/decisions.jsonl",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("ledger_write_failed", err)

    def test_entry_file_not_found_named_error(self):
        code, out, err = self._run_main(["append", "--file", "/nonexistent/e.json"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("invalid_usage", err)

    def test_append_needs_entry_source(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_main(["append", "--ledger", "decisions.jsonl"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
