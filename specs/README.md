# Specs (Legacy)

This directory is no longer used. Specs are now written to the **target repo's** `.ai/specs/` directory.

## Where Specs Live Now

```
/repos/any-project/
  └── .ai/
      └── specs/
          └── add-jwt-auth.md    ← /plan writes here
```

The `/plan` command creates `.ai/specs/` in the target repo's working directory automatically.

## Why

Specs belong with the code they describe. This makes them:
- Visible in the repo's git history and PRs
- Self-contained per project
- Accessible to any developer on the repo
- Cleaned up when the repo is removed

See `ai_docs/repo-management.md` for the full architecture.
