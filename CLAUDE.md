# Engineering rules for our Multi-Agent Orchestration System

## Do not mock tests

- Use real database connections
- Use real Claude Agent SDK agents
- IMPORTANT: The trick with database connection is to make sure your tests are ephemeral, it should start and end the database in the exact same state. Create the test data you need for the test, then clean it up after the test.

## Use .env file when needed and expose with python-dotenv

- Use python-dotenv to load environment variables from .env file

## IMPORTANT: Actually read the file

- IMPORTANT: When asked to read a file, read all of it - don't just read the first N lines.
- Read the file in chunks. If that's too large, cut in half and try again, then iterate to the next chunk.
- This is VERY IMPORTANT for understanding the codebase.
- Even if the file is large, read all of it in chunks.
- IMPORTANT: Use `wc -l <filename>` to get line counts if needed. So you can properly divide your Read tool in the right chunks.

## Use Astral UV, never raw python

- We're using Astral UV to manage our python projects.
- Always use uv to run commands, never raw python.

## Python rich panels

- Always full width panels with rich.

## Git Commits

- IMPORTANT: Do NOT commit any changes to the git repository unless you are explicitly asked to do so.

## Avoid dict and prefer pydantic models

- Prefer pydantic models over dicts.

## IMPORTANT: Never silently fail, always raise an error and log it.

- Never create a try/except block that skips over errors.
- If you try catch an error, always log it and raise it again.

## Per-repo AI artifacts

- All specs, reviews, and fix reports go into the target repo's `.ai/` directory
- Structure: `.ai/specs/`, `.ai/reviews/`, `.ai/fix-reports/`
