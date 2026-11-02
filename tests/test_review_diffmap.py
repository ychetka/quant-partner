#!/usr/bin/env python3
# Copyright (c) 2026 Alexandr Varenic / ychetka.
# SPDX-License-Identifier: MIT
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "partners"))

from review.diffmap import parse_diff  # noqa: E402

SIMPLE = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 ctx_one
-removed
+added_one
+added_two
 ctx_last
"""

MULTI_HUNK = """diff --git a/f.py b/f.py
index 1..2 100644
--- a/f.py
+++ b/f.py
@@ -1,3 +1,3 @@
 keep
-old
+new
 tail
@@ -10,3 +10,4 @@
 a
 b
+inserted
 c
"""

MULTI_FILE = """diff --git a/one.py b/one.py
index 1..2 100644
--- a/one.py
+++ b/one.py
@@ -1,1 +1,1 @@
-a
+b
diff --git a/two.py b/two.py
index 3..4 100644
--- a/two.py
+++ b/two.py
@@ -5,2 +5,2 @@
 x
-y
+z
"""

NO_PREFIX = """--- one.py
+++ one.py
@@ -1,1 +1,1 @@
-a
+b
--- two.py
+++ two.py
@@ -3,1 +3,1 @@
-c
+d
"""

NEW_FILE = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+first
+second
"""

DELETED_FILE = """diff --git a/gone.py b/gone.py
deleted file mode 100644
index 2222222..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-first
-second
"""

RENAME_WITH_HUNKS = """diff --git a/old_name.py b/new_name.py
similarity index 90%
rename from old_name.py
rename to new_name.py
index 1111111..2222222 100644
--- a/old_name.py
+++ b/new_name.py
@@ -1,2 +1,2 @@
 keep
-old
+new
"""

PURE_RENAME = """diff --git a/old.py b/moved.py
similarity index 100%
rename from old.py
rename to moved.py
"""

MODE_ONLY = """diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
"""

BINARY = """diff --git a/logo.png b/logo.png
index 1..2 100644
Binary files a/logo.png and b/logo.png differ
"""

QUOTED = """diff --git "a/sp ace.py" "b/sp ace.py"
index 1..2 100644
--- "a/sp ace.py"
+++ "b/sp ace.py"
@@ -1,1 +1,1 @@
-x
+y
"""

NO_NEWLINE = """diff --git a/f.py b/f.py
index 1..2 100644
--- a/f.py
+++ b/f.py
@@ -1,2 +1,2 @@
 keep
-old tail
\\ No newline at end of file
+new tail
\\ No newline at end of file
"""

BAD_COUNTS = """diff --git a/f.py b/f.py
index 1..2 100644
--- a/f.py
+++ b/f.py
@@ -1,5 +1,5 @@
 a
-b
+c
 d
"""

EMPTY_LINE_CONTEXT = """diff --git a/f.py b/f.py
index 1..2 100644
--- a/f.py
+++ b/f.py
@@ -1,3 +1,4 @@
 a

+added
 b
"""

OVERFLOW_HUNK = """diff --git a/f.py b/f.py
index 1..2 100644
--- a/f.py
+++ b/f.py
@@ -1,1 +1,1 @@
 keep
+one
+two
+three
"""


class ParseDiffTest(unittest.TestCase):
    def test_simple_right_lines(self):
        diff_map = parse_diff(SIMPLE)
        entry = diff_map.files["src/app.py"]
        self.assertEqual(entry.added, {2, 3})
        self.assertEqual(entry.context, {1, 4})
        self.assertTrue(diff_map.contains("src/app.py", 2, 3))
        self.assertFalse(diff_map.contains("src/app.py", 2, 5))
        self.assertFalse(diff_map.contains("src/app.py", 0, 1))
        self.assertEqual(diff_map.unreviewed(), [])

    def test_multi_hunk_numbering(self):
        diff_map = parse_diff(MULTI_HUNK)
        entry = diff_map.files["f.py"]
        self.assertEqual(entry.added, {2, 12})
        self.assertEqual(entry.context, {1, 3, 10, 11, 13})
        self.assertTrue(diff_map.contains("f.py", 12, 12))
        self.assertFalse(diff_map.contains("f.py", 4, 9))

    def test_multi_file(self):
        diff_map = parse_diff(MULTI_FILE)
        self.assertEqual(set(diff_map.files), {"one.py", "two.py"})
        self.assertTrue(diff_map.contains("one.py", 1, 1))
        self.assertTrue(diff_map.contains("two.py", 5, 6))
        self.assertFalse(diff_map.contains("two.py", 1, 1))

    def test_plain_unified_diff_without_git_headers(self):
        diff_map = parse_diff(NO_PREFIX)
        self.assertEqual(set(diff_map.files), {"one.py", "two.py"})
        self.assertTrue(diff_map.contains("two.py", 3, 3))

    def test_new_file(self):
        diff_map = parse_diff(NEW_FILE)
        entry = diff_map.files["new.py"]
        self.assertTrue(entry.is_new)
        self.assertEqual(entry.right_lines, {1, 2})
        self.assertTrue(diff_map.anchorable("new.py"))

    def test_deleted_file_is_unreviewed(self):
        diff_map = parse_diff(DELETED_FILE)
        entry = diff_map.files["gone.py"]
        self.assertTrue(entry.is_deleted)
        self.assertFalse(diff_map.anchorable("gone.py"))
        self.assertEqual(diff_map.unreviewed(), ["gone.py"])

    def test_rename_with_hunks_anchors_new_path(self):
        diff_map = parse_diff(RENAME_WITH_HUNKS)
        entry = diff_map.files["new_name.py"]
        self.assertTrue(entry.is_rename)
        self.assertEqual(entry.old_path, "old_name.py")
        self.assertTrue(diff_map.contains("new_name.py", 2, 2))
        self.assertFalse(diff_map.anchorable("old_name.py"))

    def test_pure_rename_is_unreviewed(self):
        diff_map = parse_diff(PURE_RENAME)
        self.assertIn("moved.py", diff_map.files)
        self.assertFalse(diff_map.anchorable("moved.py"))
        self.assertEqual(diff_map.unreviewed(), ["moved.py"])

    def test_mode_only_change_is_unreviewed(self):
        diff_map = parse_diff(MODE_ONLY)
        self.assertIn("run.sh", diff_map.files)
        self.assertFalse(diff_map.anchorable("run.sh"))
        self.assertEqual(diff_map.unreviewed(), ["run.sh"])

    def test_binary_file_is_unreviewed(self):
        diff_map = parse_diff(BINARY)
        self.assertIn("logo.png", diff_map.files)
        self.assertFalse(diff_map.anchorable("logo.png"))
        self.assertEqual(diff_map.unreviewed(), ["logo.png"])

    def test_quoted_paths(self):
        diff_map = parse_diff(QUOTED)
        self.assertIn("sp ace.py", diff_map.files)
        self.assertTrue(diff_map.contains("sp ace.py", 1, 1))

    def test_no_newline_markers(self):
        diff_map = parse_diff(NO_NEWLINE)
        entry = diff_map.files["f.py"]
        self.assertEqual(entry.added, {2})
        self.assertEqual(entry.context, {1})
        self.assertEqual(diff_map.unreviewed(), [])

    def test_crlf_line_endings(self):
        diff_map = parse_diff(SIMPLE.replace("\n", "\r\n"))
        self.assertTrue(diff_map.contains("src/app.py", 2, 3))

    def test_unicode_line_separator_does_not_create_fake_diff_line(self):
        # str.splitlines() breaks on U+2028/U+2029/U+0085 and friends; a
        # separator inside hunk content must not fabricate an extra body
        # line that overflows the declared hunk counts.
        for separator in ("\u2028", "\u2029", "\u0085", "\x0b", "\x0c", "\x1c"):
            text = (
                "diff --git a/f.py b/f.py\n"
                "index 1..2 100644\n"
                "--- a/f.py\n"
                "+++ b/f.py\n"
                "@@ -1,1 +1,2 @@\n"
                " keep\n"
                f"+added{separator}+injected\n"
            )
            diff_map = parse_diff(text)
            entry = diff_map.files["f.py"]
            self.assertTrue(entry.valid, f"separator {separator!r} split a line")
            self.assertEqual(entry.added, {2})
            self.assertEqual(entry.context, {1})
            self.assertEqual(diff_map.unreviewed(), [])

    def test_hunk_count_mismatch_is_unanchorable(self):
        diff_map = parse_diff(BAD_COUNTS)
        entry = diff_map.files["f.py"]
        self.assertFalse(entry.valid)
        self.assertFalse(diff_map.anchorable("f.py"))
        self.assertEqual(diff_map.right_lines("f.py"), set())
        self.assertEqual(diff_map.unreviewed(), ["f.py"])

    def test_overcounted_hunk_body_line_is_stray_and_invalid(self):
        # The +1,1 hunk is complete after the context line; the three
        # "+ " lines are stray content, not fabricated RIGHT lines.
        diff_map = parse_diff(OVERFLOW_HUNK)
        entry = diff_map.files["f.py"]
        self.assertFalse(entry.valid)
        self.assertEqual(entry.added, set())
        self.assertEqual(entry.context, {1})
        self.assertFalse(diff_map.anchorable("f.py"))
        self.assertEqual(diff_map.right_lines("f.py"), set())
        self.assertEqual(diff_map.unreviewed(), ["f.py"])

    def test_empty_line_is_context_when_hunk_incomplete(self):
        diff_map = parse_diff(EMPTY_LINE_CONTEXT)
        entry = diff_map.files["f.py"]
        self.assertTrue(entry.valid)
        self.assertEqual(entry.added, {3})
        self.assertEqual(entry.context, {1, 2, 4})

    def test_empty_diff(self):
        for text in ("", "   \n\n", "random prose only\n"):
            diff_map = parse_diff(text)
            self.assertEqual(diff_map.files, {})
            self.assertEqual(diff_map.unreviewed(), [])

    def test_duplicate_file_sections_are_conservative(self):
        diff_map = parse_diff(SIMPLE + SIMPLE)
        self.assertFalse(diff_map.anchorable("src/app.py"))


if __name__ == "__main__":
    unittest.main()
