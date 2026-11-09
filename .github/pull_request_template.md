## Summary

<!-- what this PR does, in one or two sentences -->

## Change entry

- [ ] `.changes/NNNN-slug.md` added (see `.changes/README.md`; public-safe content)
- [ ] ADR required: **yes/no** - required for `type: feat`, `breaking: true`, or cross-module refactor (`docs/adr/`); for fix/docs/chore the `.changes` file is the micro-ADR
- ADR link (if yes):

## Test plan

- [ ] `pytest tests/ -x -q` green
- [ ] `ruff check .` + `ruff format --check .` clean
- [ ] pyright `--outputjson` at baseline (`PYRIGHT_ERROR_BASELINE`, currently 0)
- [ ] `.devin/scripts/verify_constraints.sh` ALL PASS

Closes #
