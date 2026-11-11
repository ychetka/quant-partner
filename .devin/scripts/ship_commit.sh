#!/usr/bin/env bash
# ship_commit.sh - commit wrapper with enforced gates.
# Usage: ship_commit.sh "message" [file ...]
# Stages given files (or keeps existing index), then enforces, in order:
#   1. precommit re-grounding (mechanical, unconditional): the caller is
#      shown the real staged footprint before any commit happens
#   2. scope gate: if .devin/scope_whitelist.txt exists, every staged
#      file must be inside it (exact path or dir/ prefix entries)
#   3. migration gate: staged migration files require a valid
#      .devin/migration_ok stamp produced by migration_gate.sh
#   4. hygiene gate: message + staged diff through hygiene_check.sh
# Exit codes: 1 usage/env, 2 scope or hygiene reject, 3 migration gate.
set -uo pipefail
# .devin may live outside the git toplevel (shared_person layout) -
# resolve it from this script's own location BEFORE cd changes CWD.
DEVIN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TOP=$(git rev-parse --show-toplevel 2>/dev/null || git -C code rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo" >&2; exit 1; }
cd "$TOP"

MSG="${1:?usage: ship_commit.sh \"message\" [file ...]}"
shift || true

# Stage explicit file list if given
[ $# -gt 0 ] && { git add -- "$@" || { echo "git add failed" >&2; exit 1; }; }

# --- Gate 0: precommit re-grounding (mechanical, cannot be skipped) ---
# The caller is always confronted with the real staged footprint.
echo "=== RE-GROUNDING: staged footprint ===" >&2
git status --porcelain >&2
git diff --cached --stat >&2
echo "=== END RE-GROUNDING ===" >&2

# --- Gate 1: whitelist scope ---
# -z: filenames with whitespace/quotes come back raw, not git-quoted.
WL="$DEVIN_DIR/scope_whitelist.txt"
if [ -f "$WL" ]; then
  bad=""
  while IFS= read -r -d '' f; do
    [ -z "$f" ] && continue
    ok=0
    while IFS= read -r line; do
      case "$line" in ''|'#'*) continue ;; esac
      line="${line%/}"
      if [ "$f" = "$line" ]; then ok=1; break; fi
      case "$f" in "$line"/*) ok=1; break ;; esac
    done < "$WL"
    [ "$ok" -eq 0 ] && bad="$bad$f"$'\n'
  done < <(git diff --cached --name-only -z)
  if [ -n "$bad" ]; then
    echo "SCOPE_GATE: staged files outside $WL:" >&2
    printf '%s' "$bad" >&2
    exit 2
  fi
fi

# --- Gate 2: migration gate ---
MIG_PAT='(^|/)(migrations?|alembic/versions|db/migrate|flyway|liquibase)/|(^|/)[VU][0-9]+(_[0-9]+)*__.*\.sql$'
migs=$(git diff --cached --name-only -z --diff-filter=AMR | grep -zE "$MIG_PAT" | tr '\0' '\n' || true)
if [ -n "$migs" ]; then
  stamp="$DEVIN_DIR/migration_ok"
  # Bind to staged blob hashes (index), not working-copy files.
  want=$(while IFS= read -r f; do git ls-files -s -- "$f"; done <<< "$migs" | awk '{print $2}' | sort | sha256sum | cut -d' ' -f1)
  have=""
  [ -f "$stamp" ] && have=$(grep -m1 '^files=' "$stamp" | cut -d= -f2)
  if [ "$want" != "$have" ] || [ -z "$have" ]; then
    echo "MIGRATION_GATE: staged migration files lack a valid stamp." >&2
    echo "Run .devin/scripts/migration_gate.sh first (verifies rollback" >&2
    echo "or approved roll-forward for exactly these files)." >&2
    echo "$migs" >&2
    exit 3
  fi
fi

# --- Gate 3: hygiene. Message and diff are checked SEPARATELY: a
# multi-line MSG could forge the '@@SUBJECTS_END@@' sentinel or a fake
# "diff --git"/"+++" header and smuggle unscanned content under an
# exempt path into `git commit -m`. Stdin mode scans the whole message
# with no path context (no exemptions); the raw diff gets path tracking
# for per-file language exemptions.
printf '%s\n' "$MSG" | "$DEVIN_DIR/scripts/hygiene_check.sh" || {
  echo "HYGIENE_GATE: commit message rejected" >&2; exit 2; }
git diff --cached | "$DEVIN_DIR/scripts/hygiene_check.sh" --diff || {
  echo "HYGIENE_GATE: staged content rejected" >&2; exit 2; }

git commit -m "$MSG" || exit 1

# Consume-once: the migration stamp is valid for exactly one commit.
# Without this the same stamp could be reused for later commits whose
# migration set happens to hash identically (e.g. re-staged blobs).
if [ -f "$DEVIN_DIR/migration_ok" ]; then
  rm -f "$DEVIN_DIR/migration_ok"
fi
