#!/usr/bin/env bash
# hygiene_check.sh - deterministic gate for outbound text.
# stdin: text to check. --diff: stdin is a raw git diff (filenames
# parsed for path exemptions). --diff-base <ref>: check commit
# subjects + added lines of the diff vs <ref>.
# Exit 0 = clean. Exit 1 = violations JSON.
set -uo pipefail

# .devin may sit outside the git toplevel - resolve it from the
# script's own location so callers running from $TOP still find config.
DEVIN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PJ="$DEVIN_DIR/project.json"
LANG="en"
CYR_PATHS="[]"
if [ -f "$PJ" ]; then
  # Path passed via env - never interpolated into Python source
  # (a quote in the path would otherwise be a code-injection sink).
  LANG=$(HYGIENE_PJ="$PJ" python3 -c "import json,os;print(json.load(open(os.environ['HYGIENE_PJ'])).get('text_language','en'))" 2>/dev/null || echo en)
  CYR_PATHS=$(HYGIENE_PJ="$PJ" python3 -c "import json,os;print(json.dumps(json.load(open(os.environ['HYGIENE_PJ'])).get('cyrillic_paths',[])))" 2>/dev/null || echo '[]')
fi

run_check() { # $1 = file to scan, $2 = mode (stdin|diff)
  HYGIENE_INPUT_FILE="$1" HYGIENE_LANG="$LANG" HYGIENE_MODE="$2" \
  HYGIENE_CYR_PATHS="$CYR_PATHS" python3 - <<'PYEOF'
import json, os, re, sys
lang = os.environ.get("HYGIENE_LANG", "en")
mode = os.environ.get("HYGIENE_MODE", "stdin")
with open(os.environ["HYGIENE_INPUT_FILE"], encoding="utf-8",
          errors="replace") as fh:
    text = fh.read()
# Fleet convention: .devin/agents/ profiles are intentionally Russian
# in every repo, so that path is exempt even without project.json.
cyr_paths = list({".devin/agents/"} | set(
    json.loads(os.environ.get("HYGIENE_CYR_PATHS", "[]"))))
violations = []

def hit(cls, pattern, line_no, hint, path=None):
    v = {"class": cls, "line": line_no,
         "pattern": pattern, "hint": hint}
    if path is not None:
        v["file"] = path
    violations.append(v)

rules = [
    (r"generated\s+with", "attribution",
     "tool attribution footer - remove it"),
    (r"co-""authored-""by", "attribution",
     "attribution trailer - remove it"),
    (r"signed-""off-""by:.*(devin|claude|copilot|bot|ai\b)", "attribution",
     "tool sign-off - remove it"),
    (r"\b(devin|claude|copilot|chatgpt|gpt-\d|gemini|swe-\d)[\w.-]*\s*<", "attribution",
     "tool name in author/trailer line"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "secret",
     "private key material"),
    (r"(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}", "secret",
     "GitHub token pattern"),
    (r"(s""k-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})", "secret",
     "API key pattern"),
    (r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}", "secret",
     "credential assignment"),
]

# In diff mode each checked line carries the file it was added to;
# any non-diff content is scanned as plain text with no path context.
# Subjects and diffs are NEVER mixed into one stream - callers scan
# them separately, so no sentinel can be forged by input content.
def items():
    path = None
    in_diff = mode != "diff"
    header = False
    for line in text.splitlines():
        if mode == "diff" and line.startswith("diff --git"):
            in_diff = True
            header = True
            path = ""
            yield line, None  # filenames are outbound text too
            continue
        if mode == "diff" and not in_diff:
            yield line, None
            continue
        if mode == "diff" and line.startswith("@@"):
            header = False
            continue
        if mode == "diff" and header and line.startswith("+++"):
            if line.startswith("+++ b/"):
                path = line[6:]
            else:
                path = line[4:]
            yield line, None  # scan the path itself with no exemption
            continue
        if mode == "diff" and line.startswith("+"):
            yield line, path
            continue
        if mode == "diff":
            continue
        yield line, None

for i, (line, path) in enumerate(items(), 1):
    for pattern, cls, hint in rules:
        if re.search(pattern, line, re.IGNORECASE):
            hit(cls, pattern, i, hint, path)
    exempt = path is not None and any(
        path.startswith(p) for p in cyr_paths)
    if lang == "en" and not exempt and re.search(
            "[\\u0410-\\u044f\\u0401\\u0451]", line):
        hit("language", "[cyrillic]", i,
            "non-English text in en-language repository", path)

if violations:
    print(json.dumps({"violations": violations}, indent=2))
    sys.exit(1)
sys.exit(0)
PYEOF
}

fail=0
if [ "${1:-}" = "--diff-base" ]; then
  BASE="${2:?--diff-base needs a ref}"
  TOP=$(git rev-parse --show-toplevel 2>/dev/null || git -C code rev-parse --show-toplevel 2>/dev/null) || { echo '{"violations":[{"class":"infra","line":0,"hint":"not a git repo - fail closed"}]}'; exit 1; }
  # Subjects and the diff are checked in SEPARATE passes (issue #88):
  # a subject can forge a sentinel or a fake "diff --git"/"+++ <exempt>"
  # header and smuggle content under an exempt path if both share one
  # stream. Stdin mode gives subjects no path context (no exemptions).
  F1=$(mktemp); F2=$(mktemp)
  trap 'rm -f "$F1" "$F2"' EXIT
  git -C "$TOP" log --format=%s "${BASE}..HEAD" > "$F1" 2>/dev/null
  git -C "$TOP" diff "${BASE}..HEAD" -- ':(exclude)*.lock' > "$F2"
  run_check "$F1" stdin || fail=1
  run_check "$F2" diff || fail=1
elif [ "${1:-}" = "--diff" ]; then
  F1=$(mktemp); trap 'rm -f "$F1"' EXIT
  cat > "$F1"
  run_check "$F1" diff || fail=1
else
  F1=$(mktemp); trap 'rm -f "$F1"' EXIT
  cat > "$F1"
  run_check "$F1" stdin || fail=1
fi
exit $fail
