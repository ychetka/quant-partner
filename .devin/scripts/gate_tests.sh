#!/usr/bin/env bash
# gate_tests.sh - negative tests for the commit-wrapper gates.
# Proves each gate blocks its bypass path in an isolated scratch repo:
#   T1 scope gate: file outside whitelist is rejected (exit 2)
#   T2 scope gate: dir-prefix whitelist entry allows the file
#   T3 migration gate: staged migration without stamp is rejected (exit 3)
#   T4 migration gate: rollback-covered migration gets stamped, commit ok
#   T5 migration stamp invalidates when the migration file changes
#   T8 rename+modify of a stamped migration is blocked
#   T9 staged up+down pair verifies via paired-rollback coverage
#   T6 re-grounding output is emitted on every call
#   T7 hygiene gate still rejects (cyrillic on en-language repo)
#   T10 forged @@SUBJECTS_END@@ sentinel in the commit message cannot
#       smuggle cyrillic under a fake exempt diff path (issue #88, F5)
#   T11 migration stamp is consumed by a successful commit (issue #88, F4)
#   T12 roll-forward note must be tracked in HEAD and name the file (F2)
#   T13 empty paired rollback file is not coverage (issue #88, F1)
#   T14 --diff-base cannot be desynced by forged sentinel/diff-header
#       subjects - cyrillic subjects are flagged (issue #88 review)
# Exits non-zero if any expectation fails. Run from anywhere:
#   .devin/scripts/gate_tests.sh
set -uo pipefail
DEVIN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

SCRATCH=$(mktemp -d)
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/repo" "$SCRATCH/devin/scripts"
cd "$SCRATCH/repo" && git init -q && git config user.email t@t && git config user.name t && git commit -q --allow-empty -m init
cp "$DEVIN_DIR/scripts/ship_commit.sh" "$DEVIN_DIR/scripts/migration_gate.sh" \
   "$DEVIN_DIR/scripts/hygiene_check.sh" "$SCRATCH/devin/scripts/"
echo '{"text_language":"en"}' > "$SCRATCH/devin/project.json"

pass=0; fail=0
expect() { # name expected_rc actual_rc [pattern_in_output]
  local name="$1" want="$2" got="$3" pat="${4:-}"
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: expected exit $want, got $got"; fail=$((fail+1)); return
  fi
  if [ -n "$pat" ] && ! grep -q "$pat" "$SCRATCH/out"; then
    echo "FAIL $name: output missing '$pat'"; fail=$((fail+1)); return
  fi
  echo "PASS $name"; pass=$((pass+1))
}

SHIP="$SCRATCH/devin/scripts/ship_commit.sh"
GATE="$SCRATCH/devin/scripts/migration_gate.sh"

# --- T1: scope gate blocks out-of-whitelist staged file
echo 'docs/x.md' > "$SCRATCH/devin/scope_whitelist.txt"
mkdir -p src && echo x > src/evil.py && git add src/evil.py
"$SHIP" "test msg" >"$SCRATCH/out" 2>&1; expect T1-scope-block 2 $? "SCOPE_GATE"
git reset -q

# --- T2: dir-prefix entry allows the file
printf 'src/\ndocs/x.md\n' > "$SCRATCH/devin/scope_whitelist.txt"
git add src/evil.py
"$SHIP" "test msg" >"$SCRATCH/out" 2>&1; expect T2-scope-prefix 0 $?

# --- T3: migration gate blocks unstamped migration
rm "$SCRATCH/devin/scope_whitelist.txt"
mkdir -p db/migrations && printf 'CREATE TABLE t(id int);\n-- rollback: DROP TABLE t;\n' > db/migrations/001_up.sql
git add db/migrations/001_up.sql
"$SHIP" "add migration" >"$SCRATCH/out" 2>&1; expect T3-migration-block 3 $? "MIGRATION_GATE"
git reset -q

# --- T4: rollback-covered migration stamps and commits
git add db/migrations/001_up.sql
"$GATE" >"$SCRATCH/out" 2>&1; expect T4a-gate-verifies 0 $? "embedded rollback"
"$SHIP" "add migration" >"$SCRATCH/out" 2>&1; expect T4b-commit-ok 0 $?

# --- T5: stamp invalidates on changed migration file
printf 'ALTER TABLE t ADD COLUMN c int;\n' >> db/migrations/001_up.sql
git add db/migrations/001_up.sql
"$SHIP" "change migration" >"$SCRATCH/out" 2>&1; expect T5-stale-stamp 3 $? "MIGRATION_GATE"
git reset -q --hard HEAD 2>/dev/null || git reset -q

# --- T8: rename+modify cannot smuggle a changed migration under an
# old stamp (pure rename of verified content keeps the stamp - the
# stamp binds staged blob content, not the filename)
git mv db/migrations/001_up.sql db/migrations/002_up.sql
printf 'ALTER TABLE t ADD COLUMN c2 int;\n' >> db/migrations/002_up.sql
git add db/migrations/002_up.sql
"$SHIP" "rename+modify migration" >"$SCRATCH/out" 2>&1; expect T8-rename-modify-block 3 $? "MIGRATION_GATE"
git reset -q --hard HEAD 2>/dev/null || git reset -q

# --- T9: staged up+down pair verifies via paired-rollback coverage
printf 'CREATE TABLE u(id int);\n' > db/migrations/003_up.sql
printf 'DROP TABLE u;\n' > db/migrations/003_down.sql
git add db/migrations/003_up.sql db/migrations/003_down.sql
"$GATE" >"$SCRATCH/out" 2>&1; expect T9a-paired-verify 0 $? "paired rollback"
"$SHIP" "migration pair" >"$SCRATCH/out" 2>&1; expect T9b-commit-ok 0 $?

# --- T6: re-grounding emitted on every call (even on gate failures)
echo y > docs_x.md && mkdir -p docs && mv docs_x.md docs/x.md && git add docs/x.md
"$SHIP" "test" >"$SCRATCH/out" 2>&1
grep -q "RE-GROUNDING" "$SCRATCH/out" && { echo "PASS T6-regrounding"; pass=$((pass+1)); } \
  || { echo "FAIL T6-regrounding"; fail=$((fail+1)); }
echo z2 > docs/z.md && git add docs/z.md
"$SHIP" "test" >"$SCRATCH/out" 2>&1; expect T6b-commit-ok 0 $?

# --- T7: hygiene still rejects non-English commit message
# Cyrillic built from escapes: this file itself stays pure ASCII.
CYR_MSG=$'\u0442\u0435\u0441\u0442\u043e\u0432\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435'
echo z > docs/y.md && git add docs/y.md
"$SHIP" "$CYR_MSG" >"$SCRATCH/out" 2>&1; expect T7-hygiene 2 $? "HYGIENE_GATE"
git reset -q

# --- T10: forged sentinel in the message cannot smuggle cyrillic
# under a fake exempt path (the message is scanned standalone now).
printf 'CREATE TABLE w(id int);\n' > docs/w.md && git add docs/w.md
FORGED=$(printf 'ok\n@@SUBJECTS_END@@\ndiff --git a/x b/.devin/agents/x.md\n+++ b/.devin/agents/x.md\n+\xd1\x81\xd0\xbe\xd0\xbe\xd0\xb1\xd1\x89\xd0\xb5\xd0\xbd\xd0\xb8\xd0\xb5')
"$SHIP" "$FORGED" >"$SCRATCH/out" 2>&1; expect T10-forged-sentinel 2 $? "HYGIENE_GATE"
git reset -q

# --- T11: a successful migration commit consumes the stamp.
printf 'CREATE TABLE v(id int);\n-- rollback: DROP TABLE v;\n' > db/migrations/004_up.sql
git add db/migrations/004_up.sql
"$GATE" >"$SCRATCH/out" 2>&1
"$SHIP" "migration 004" >"$SCRATCH/out" 2>&1; expect T11a-commit-ok 0 $?
[ ! -f "$SCRATCH/devin/migration_ok" ] \
  && { echo "PASS T11b-stamp-consumed"; pass=$((pass+1)); } \
  || { echo "FAIL T11b-stamp-consumed: stamp still present"; fail=$((fail+1)); }

# --- T12: roll-forward note must be tracked in HEAD and name the file.
# .devin must live inside the repo for coverage (c) - separate layout.
R2="$SCRATCH/repo2"
mkdir -p "$R2/.devin/scripts" "$R2/db/migrations"
cp "$SCRATCH/devin/scripts/"*.sh "$R2/.devin/scripts/"
echo '{"text_language":"en"}' > "$R2/.devin/project.json"
( cd "$R2" && git init -q && git config user.email t@t && git config user.name t \
  && git commit -q --allow-empty -m init )
printf 'CREATE TABLE n(id int);\n' > "$R2/db/migrations/006_x.sql"
echo "roll forward approved" > "$R2/.devin/migration_rollforward.md"
( cd "$R2" && git add db/migrations/006_x.sql )
( cd "$R2" && ./.devin/scripts/migration_gate.sh ) >"$SCRATCH/out" 2>&1
expect T12a-untracked-note 1 $? "no rollback path"
# Commit ONLY the note (path-scoped, the migration stays staged).
( cd "$R2" && git add .devin/migration_rollforward.md \
    && git commit -qm "note" -- .devin/migration_rollforward.md )
# Note now tracked but does not name the file - still not coverage.
( cd "$R2" && ./.devin/scripts/migration_gate.sh ) >"$SCRATCH/out" 2>&1
expect T12b-unnamed-note 1 $? "no rollback path"
# Note tracked in HEAD AND naming the file - coverage granted.
echo "006_x.sql approved for roll-forward" > "$R2/.devin/migration_rollforward.md"
( cd "$R2" && git add .devin/migration_rollforward.md \
    && git commit -qm "name 006" -- .devin/migration_rollforward.md )
( cd "$R2" && ./.devin/scripts/migration_gate.sh ) >"$SCRATCH/out" 2>&1
expect T12c-bound-note 0 $? "approved roll-forward"

# --- T13: an empty paired rollback file is not coverage.
cd "$SCRATCH/repo"
printf 'CREATE TABLE e(id int);\n' > db/migrations/007_up.sql
: > db/migrations/007_down.sql
git add db/migrations/007_up.sql db/migrations/007_down.sql
"$GATE" >"$SCRATCH/out" 2>&1; expect T13-empty-pair 1 $? "no rollback path"
git reset -q

# --- T14: --diff-base cannot be desynced by forged subjects.
# A literal @@SUBJECTS_END@@ subject plus fake diff-header subjects
# used to smuggle an unscanned cyrillic subject through; subjects are
# now scanned in a separate stdin-mode pass with no path context.
git commit -q --allow-empty -m '@@SUBJECTS_END@@'
git commit -q --allow-empty -m 'diff --git a/x b/.devin/agents/x.md'
git commit -q --allow-empty -m '+++ b/.devin/agents/x.md'
git commit -q --allow-empty -m "$CYR_MSG"
"$SCRATCH/devin/scripts/hygiene_check.sh" --diff-base HEAD~4 >"$SCRATCH/out" 2>&1
expect T14-subject-smuggle 1 $? "cyrillic"

echo "---"
echo "gate_tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
