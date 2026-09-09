# Contributing to aiduMEI

Thanks for considering a contribution. aiduMEI keeps a few unusual engineering
disciplines; please read this before opening a PR.

## The short version

1. Fork, branch, change, test.
2. Run the local gates before pushing — CI runs them again on your PR.
3. One PR = one concern. Refactors and behavior changes do not mix.

## Local gates (all must pass)

```bash
pytest tests/ -q -rs        # full suite; README's measured matrix applies
bash scripts/push_gate.sh   # tests + ruff + compile + release-scan (7 aspects)
```

- **Tests are the contract.** New behavior ships with new tests; fixed bugs
  ship with a regression test that fails without the fix (negative control).
- **Guards are part of the codebase.** Many tests pin documentation numbers,
  env registries, migration points and brand policy. If a guard goes red after
  your change, either the change or the guard's criterion is wrong — fix the
  one that is wrong, and say which in the commit message.
- **Ratchets only tighten.** `except Exception` density, ruff rule coverage
  and similar counters never move upward without a written reason in
  `CHANGELOG.md`.

## Style

- Match the surrounding code. Comments explain *why*, not *what*.
- `ducky/version.py` is the single source of truth for the version number;
  never hardcode versions elsewhere.
- User-facing documents are bilingual: `README.md` (中文) and `README_EN.md`
  change together (a guard enforces parity).

## Commits and PRs

- English, imperative subject lines; reference the audit/finding ID when the
  change closes one (e.g. `P1-3`, `SEC-01`).
- Do not commit credentials, hostnames, or internal identifiers — the release
  scan gate will catch them anyway.

## Security issues

Do **not** open a public issue. See [SECURITY.md](SECURITY.md).

---

## 中文摘要

- 先跑 `pytest tests/ -q -rs` 与 `bash scripts/push_gate.sh`，全绿再提 PR。
- 新功能带新测试；修 bug 带回归测试（不修就红的负向对照）。
- 守卫变红时，改错的那个（代码或判据），commit 里写明改了哪边、为什么。
- 棘轮只降不升；版本号只认 `ducky/version.py`；README 中英双语同步改。
- 安全问题走 [SECURITY.md](SECURITY.md)，不开公开 issue。
