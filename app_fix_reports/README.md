# App Fix Reports (Legacy)

This directory is no longer used. Fix reports are now written to the **target repo's** `.ai/fix-reports/` directory.

## Where Fix Reports Live Now

```
/repos/any-project/
  └── .ai/
      └── fix-reports/
          └── fix_2026-03-04T150000.md    ← /fix writes here
```

The `/fix` command creates `.ai/fix-reports/` in the target repo's working directory automatically.

## Fix Priority

| Priority | Action |
|----------|--------|
| BLOCKER | Always fixed — blocks the merge |
| HIGH | Always fixed — should not ship |
| MEDIUM | Fixed if time permits |
| LOW | Fixed if trivial |

See `ai_docs/repo-management.md` for the full architecture.
