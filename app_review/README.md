# App Review (Legacy)

This directory is no longer used. Review reports are now written to the **target repo's** `.ai/reviews/` directory.

## Where Reviews Live Now

```
/repos/any-project/
  └── .ai/
      └── reviews/
          └── review_2026-03-04T143000.md    ← /review writes here
```

The `/review` command creates `.ai/reviews/` in the target repo's working directory automatically.

## Risk Tiers

| Tier | Meaning | Action |
|------|---------|--------|
| BLOCKER | Security vuln, data loss, crash | Must fix before merge |
| HIGH | Perf regression, missing error handling, race condition | Should fix before merge |
| MEDIUM | Code duplication, missing tests, tech debt | Fix soon |
| LOW | Style, minor refactor, cosmetic | Nice to have |

**Verdict:** Any BLOCKER = FAIL. No BLOCKERS = PASS.

See `ai_docs/repo-management.md` for the full architecture.
