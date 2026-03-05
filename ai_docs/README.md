# AI Documentation Sources

This directory contains cached AI documentation for agents to use as context.
Use `/prime` or `/prime_specific_docs` to load these into agent context before a task.

## Documentation URLs

### Claude Agent SDK
- https://platform.claude.com/docs/en/agent-sdk/sessions
- https://platform.claude.com/docs/en/agent-sdk/slash-commands
- https://platform.claude.com/docs/en/agent-sdk/hooks
- https://platform.claude.com/docs/en/agent-sdk/python
- https://platform.claude.com/docs/en/agent-sdk/custom-tools
- https://platform.claude.com/docs/en/agent-sdk/skills
- https://platform.claude.com/docs/en/agent-sdk/structured-outputs

### Claude Code CLI
- https://docs.claude.com/en/docs/claude-code/sdk/custom-tools
- https://docs.claude.com/en/docs/claude-code/sdk/sdk-slash-commands
- https://docs.claude.com/en/docs/claude-code/sdk/migration-guide

### Anthropic Platform
- https://www.anthropic.com/engineering/writing-tools-for-agents
- https://docs.claude.com/en/api/skills-guide
- https://docs.claude.com/en/docs/agents-and-tools/agent-skills/best-practices
- https://docs.claude.com/en/docs/about-claude/models/overview
- https://docs.claude.com/en/api/agent-sdk/cost-tracking

### Tooling
- https://docs.astral.sh/uv/guides/scripts/
- https://docs.astral.sh/uv/guides/projects/#managing-dependencies

## How to Add Documentation

1. Add the URL to this README
2. Use the `/prime_specific_docs` command or `docs-scraper` agent to fetch and cache
3. Cached files are stored as markdown in this directory
4. Agents reference cached docs to avoid repeated web fetches

## Repo-Centric Context Loading

When working across multiple repos, agents should:
1. Load this directory for platform/SDK docs
2. Load the target repo's own `CLAUDE.md` or `README.md` for project-specific context
3. Use `/prime` to combine both into a single context window
