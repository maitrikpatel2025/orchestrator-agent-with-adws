# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "asyncpg>=0.29.0",
#   "python-dotenv>=1.0.0",
#   "pydantic>=2.0",
#   "claude-agent-sdk>=0.1.18",
#   "rich>=13.0",
#   "websockets>=12.0",
# ]
# ///
"""
ADW SDLC Workflow - Five-step Software Development Life Cycle with worktree isolation and auto-PR.

This workflow:
1. Receives --adw-id as CLI arg
2. Fetches prompt and working_dir from DB
3. Creates a single git worktree in the target repo
4. Runs /plan <prompt> agent inside the worktree
5. Extracts plan file path via quick_prompt
6. Runs /build <path> agent inside the worktree
7. Runs /test <prompt> <path> agent inside the worktree
8. Runs /review <prompt> <path> agent inside the worktree
9. Runs /docs <prompt> <path> agent inside the worktree
10. Pushes branch and creates a single PR for human review
11. Logs all events to agent_logs for swimlane visualization

All 5 steps run in the same worktree. Single PR at the end.

Usage:
    uv run adws/adw_workflows/adw_sdlc.py --adw-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from adw_modules.adw_database import get_adw, create_agent, update_agent, update_log_summary
from adw_modules.adw_logging import (
    init_logging,
    close_logging,
    log_step_start,
    log_step_end,
    log_adw_event,
    log_system_event,
    update_adw_status,
)
from adw_modules.adw_summarizer import summarize_event
from adw_modules.adw_websockets import broadcast_adw_event_summary_update
from adw_modules.adw_agent_sdk import (
    query_to_completion,
    quick_prompt,
    QueryInput,
    QueryOptions,
    MessageHandlers,
    AdhocPrompt,
    HookEventName,
    HooksConfig,
    HookMatcher,
    HookInput,
    HookResponse,
    HookContext,
    PreToolUseInput,
    PostToolUseInput,
    StopInput,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    ModelName,
)

load_dotenv()

console = Console()

# =============================================================================
# CONSTANTS
# =============================================================================

STEP_PLAN = "plan"
STEP_BUILD = "build"
STEP_TEST = "test"
STEP_REVIEW = "review"
STEP_DOCS = "docs"
TOTAL_STEPS = 5

# Orchestrator project root (where .claude/commands/ lives)
ORCHESTRATOR_ROOT = Path(__file__).parent.parent.parent


# =============================================================================
# COMMAND LOADER - Reads .claude/commands/ and inlines as prompts
# =============================================================================


def load_command(command_name: str, variables: dict[str, str] | None = None) -> str:
    """Read a .claude/commands/<name>.md file and substitute variables.

    This inlines the command content as the prompt instead of relying on
    slash command discovery (which only works when cwd is the orchestrator).
    This allows agents to work in external repos while using orchestrator commands.

    Args:
        command_name: Command filename (e.g., "plan.md", "build.md")
        variables: Dict mapping variable placeholders to values.
                   e.g., {"$1": "the prompt", "$ARGUMENTS": "/path/to/plan"}
    Returns:
        The command content with variables substituted.
    """
    command_path = ORCHESTRATOR_ROOT / ".claude" / "commands" / command_name
    if not command_path.exists():
        raise FileNotFoundError(f"Command file not found: {command_path}")

    content = command_path.read_text()

    # Strip YAML frontmatter (between --- markers)
    if content.startswith("---"):
        end_idx = content.index("---", 3)
        content = content[end_idx + 3:].lstrip("\n")

    # Substitute variables
    if variables:
        for key, value in variables.items():
            content = content.replace(key, value)

    return content


# =============================================================================
# WORKTREE OPERATIONS - Create worktree in target repo
# =============================================================================


def create_worktree_in_target(
    working_dir: str,
    adw_id: str,
    branch_name: str | None = None,
) -> tuple[str, str]:
    """Create a git worktree inside the target repo for isolated work.

    Args:
        working_dir: The target repo's working directory
        adw_id: ADW ID (used for worktree path and default branch name)
        branch_name: Optional branch name (defaults to adw-{adw_id[:8]})

    Returns:
        Tuple of (worktree_path, branch_name)

    Raises:
        RuntimeError: If worktree creation fails
    """
    if branch_name is None:
        branch_name = f"adw-{adw_id[:8]}"

    worktree_dir = Path(working_dir) / ".claude" / "worktrees" / adw_id
    worktree_path = str(worktree_dir)

    # If worktree already exists, return it
    if worktree_dir.exists():
        console.print(f"[yellow]Worktree already exists: {worktree_path}[/yellow]")
        return worktree_path, branch_name

    # Ensure parent directory exists
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    # Determine base ref: prefer origin/main, fallback to HEAD
    base_ref = "HEAD"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/main"],
        capture_output=True, text=True, cwd=working_dir,
    )
    if result.returncode == 0:
        base_ref = "origin/main"

    # Try creating worktree with new branch
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, worktree_path, base_ref],
        capture_output=True, text=True, cwd=working_dir,
    )

    if result.returncode != 0:
        # Branch might already exist — try without -b
        if "already exists" in result.stderr:
            console.print(f"[yellow]Branch {branch_name} already exists, using it[/yellow]")
            result = subprocess.run(
                ["git", "worktree", "add", worktree_path, branch_name],
                capture_output=True, text=True, cwd=working_dir,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to create worktree (branch exists): {result.stderr}"
                )
        else:
            raise RuntimeError(f"Failed to create worktree: {result.stderr}")

    console.print(f"[green]Created worktree: {worktree_path} (branch: {branch_name})[/green]")
    return worktree_path, branch_name


# =============================================================================
# GIT OPERATIONS - Push and create PR
# =============================================================================


async def push_and_create_pr(
    worktree_path: str,
    branch_name: str,
    adw_id: str,
    prompt: str,
) -> str | None:
    """Push branch and create PR at end of workflow.

    Stages and commits any uncommitted changes, pushes the branch,
    and creates a PR if one doesn't already exist.

    Args:
        worktree_path: Path to the worktree
        branch_name: Branch name to push
        adw_id: ADW ID for PR metadata
        prompt: Original prompt (used in PR description)

    Returns:
        PR URL if created, or None (never fails the workflow)
    """
    try:
        # Stage any uncommitted changes
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True, cwd=worktree_path,
        )

        # Check if there are changes to commit
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if status_result.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"adw-{adw_id[:8]}: automated workflow changes"],
                capture_output=True, text=True, cwd=worktree_path,
            )

        # Push branch
        push_result = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if push_result.returncode != 0:
            console.print(f"[yellow]Push failed: {push_result.stderr}[/yellow]")
            await log_system_event(
                adw_id=adw_id,
                adw_step=None,
                level="WARNING",
                message=f"Git push failed: {push_result.stderr}",
            )
            return None

        console.print(f"[green]Pushed branch: {branch_name}[/green]")

        # Check if PR already exists
        pr_check = subprocess.run(
            ["gh", "pr", "list", "--head", branch_name, "--json", "url", "--limit", "1"],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if pr_check.returncode == 0 and pr_check.stdout.strip() not in ("", "[]"):
            import json
            pr_data = json.loads(pr_check.stdout)
            if pr_data:
                pr_url = pr_data[0]["url"]
                console.print(f"[green]PR already exists: {pr_url}[/green]")
                await log_system_event(
                    adw_id=adw_id,
                    adw_step=None,
                    level="INFO",
                    message=f"PR already exists: {pr_url}",
                )
                return pr_url

        # Create PR
        pr_title = f"adw-{adw_id[:8]}: {prompt[:60]}"
        pr_body = (
            f"## ADW SDLC Workflow\n\n"
            f"**ADW ID:** `{adw_id}`\n"
            f"**Branch:** `{branch_name}`\n"
            f"**Steps:** plan, build, test, review, docs\n\n"
            f"### Prompt\n{prompt}\n\n"
            f"---\n"
            f"*Created by ADW SDLC automated workflow*"
        )

        pr_result = subprocess.run(
            ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--base", "main"],
            capture_output=True, text=True, cwd=worktree_path,
        )

        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
            console.print(f"[green]Created PR: {pr_url}[/green]")
            await log_system_event(
                adw_id=adw_id,
                adw_step=None,
                level="INFO",
                message=f"Created PR: {pr_url}",
            )
            return pr_url
        else:
            console.print(f"[yellow]PR creation failed: {pr_result.stderr}[/yellow]")
            await log_system_event(
                adw_id=adw_id,
                adw_step=None,
                level="WARNING",
                message=f"PR creation failed: {pr_result.stderr}",
            )
            return None

    except Exception as e:
        console.print(f"[yellow]push_and_create_pr error: {e}[/yellow]")
        await log_system_event(
            adw_id=adw_id,
            adw_step=None,
            level="WARNING",
            message=f"push_and_create_pr error: {e}",
        )
        return None


# =============================================================================
# HOOK FACTORY - Creates hooks that log to DB
# =============================================================================


def create_logging_hooks(adw_id: str, adw_step: str, agent_id: str) -> HooksConfig:
    """Create hooks that log tool and lifecycle events to agent_logs.

    Captures:
    - PreToolUse: Before each tool execution
    - PostToolUse: After each tool execution (with results)
    - Stop: When the agent stops

    Args:
        adw_id: The ADW ID for logging
        adw_step: Current step slug (e.g., "plan", "build", "test", "review", "docs")
        agent_id: The agent ID for logging

    Returns:
        HooksConfig with all logging hooks
    """

    def _get_tool_summary(tool_name: str, tool_input: dict) -> str:
        """Build a human-readable summary for a tool call."""
        if tool_name == "Read" and "file_path" in tool_input:
            file_name = Path(tool_input["file_path"]).name
            return f"Read: {file_name}"
        elif tool_name == "Write" and "file_path" in tool_input:
            file_name = Path(tool_input["file_path"]).name
            return f"Write: {file_name}"
        elif tool_name == "Edit" and "file_path" in tool_input:
            file_name = Path(tool_input["file_path"]).name
            return f"Edit: {file_name}"
        elif tool_name == "Bash" and "command" in tool_input:
            cmd = tool_input["command"][:40]
            return f"Bash: {cmd}..."
        elif tool_name == "Glob" and "pattern" in tool_input:
            return f"Glob: {tool_input['pattern']}"
        elif tool_name == "Grep" and "pattern" in tool_input:
            return f"Grep: {tool_input['pattern']}"
        elif tool_name == "Skill" and "skill" in tool_input:
            return f"Skill: /{tool_input['skill']}"
        elif tool_name == "Task":
            desc = tool_input.get("description", "")[:30]
            return f"Task: {desc}..."
        return f"Tool: {tool_name}"

    async def pre_tool_use_hook(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookResponse:
        """Log tool call BEFORE execution."""
        if not isinstance(hook_input, PreToolUseInput):
            return HookResponse.allow()

        tool_name = hook_input.tool_name
        tool_input = hook_input.tool_input
        summary = _get_tool_summary(tool_name, tool_input)

        await log_adw_event(
            adw_id=adw_id,
            adw_step=adw_step,
            event_category="hook",
            event_type="PreToolUse",
            content=f"Using tool: {tool_name}",
            agent_id=agent_id,
            payload={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
            },
            summary=summary,
        )

        return HookResponse.allow()

    async def post_tool_use_hook(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookResponse:
        """Log tool usage AFTER execution with results."""
        if not isinstance(hook_input, PostToolUseInput):
            return HookResponse.allow()

        tool_name = hook_input.tool_name
        tool_input = hook_input.tool_input
        summary = _get_tool_summary(tool_name, tool_input) + " ✓"

        await log_adw_event(
            adw_id=adw_id,
            adw_step=adw_step,
            event_category="hook",
            event_type="PostToolUse",
            content=f"PostToolUse: {tool_name}",
            agent_id=agent_id,
            payload={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
                "tool_response": str(hook_input.tool_response)
                if hook_input.tool_response
                else None,
            },
            summary=summary,
        )

        return HookResponse.allow()

    async def stop_hook(
        hook_input: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookResponse:
        """Log when agent stops."""
        if not isinstance(hook_input, StopInput):
            return HookResponse.allow()

        reason = getattr(hook_input, "reason", "unknown")
        await log_adw_event(
            adw_id=adw_id,
            adw_step=adw_step,
            event_category="hook",
            event_type="Stop",
            content=f"Agent stopped: {reason}",
            agent_id=agent_id,
            payload={"reason": reason},
            summary=f"Stop: {reason}",
        )

        return HookResponse.allow()

    return HooksConfig(
        pre_tool_use=[
            HookMatcher(
                matcher=None,
                hooks=[pre_tool_use_hook],
                timeout=30,
            )
        ],
        post_tool_use=[
            HookMatcher(
                matcher=None,
                hooks=[post_tool_use_hook],
                timeout=30,
            )
        ],
        stop=[
            HookMatcher(
                matcher=None,
                hooks=[stop_hook],
                timeout=30,
            )
        ],
    )


# =============================================================================
# MESSAGE HANDLERS - Log agent response blocks
# =============================================================================


async def _summarize_and_update(log_id: str, adw_id: str, event_data: dict, event_type: str) -> None:
    """Background task to generate AI summary, update DB, and broadcast to frontend.

    Args:
        log_id: The log entry ID to update
        adw_id: The ADW ID for WebSocket broadcast
        event_data: Event data for summarization
        event_type: Type of event (TextBlock, ThinkingBlock, ToolUseBlock, etc.)
    """
    try:
        summary = await summarize_event(event_data, event_type)
        if summary and summary.strip():
            await update_log_summary(log_id, summary)
            await broadcast_adw_event_summary_update(adw_id, log_id, summary)
    except Exception as e:
        console.print(f"[dim red]Summary generation failed: {e}[/dim red]")


def create_message_handlers(adw_id: str, adw_step: str, agent_id: str) -> MessageHandlers:
    """Create handlers that log agent message blocks to agent_logs.

    Captures individual blocks for granular visualization:
    - TextBlock: Agent text responses
    - ThinkingBlock: Agent thinking/reasoning
    - ToolUseBlock: Tool call declarations (before execution)

    Each block is logged with a static fallback summary, then an async task
    is spawned to generate an AI-powered summary using Claude Haiku.

    Args:
        adw_id: The ADW ID for logging
        adw_step: Current step slug
        agent_id: The agent ID for logging

    Returns:
        MessageHandlers for response logging
    """

    async def on_assistant_block(block: TextBlock | ThinkingBlock | ToolUseBlock) -> None:
        """Log individual assistant message blocks with async AI summarization."""
        if isinstance(block, TextBlock):
            text = block.text
            preview = text[:150] + "..." if len(text) > 150 else text
            log_id = await log_adw_event(
                adw_id=adw_id,
                adw_step=adw_step,
                event_category="response",
                event_type="TextBlock",
                content=text,
                agent_id=agent_id,
                payload={"text": text},
                summary=f"Response: {preview}",
            )
            asyncio.create_task(_summarize_and_update(
                log_id, adw_id, {"content": text}, "TextBlock"
            ))

        elif isinstance(block, ThinkingBlock):
            thinking = block.thinking
            preview = thinking[:100] + "..." if len(thinking) > 100 else thinking
            log_id = await log_adw_event(
                adw_id=adw_id,
                adw_step=adw_step,
                event_category="response",
                event_type="ThinkingBlock",
                content=thinking,
                agent_id=agent_id,
                payload={"thinking": thinking},
                summary=f"Thinking: {preview}",
            )
            asyncio.create_task(_summarize_and_update(
                log_id, adw_id, {"thinking": thinking}, "ThinkingBlock"
            ))

        elif isinstance(block, ToolUseBlock):
            tool_name = block.name
            tool_input = block.input
            log_id = await log_adw_event(
                adw_id=adw_id,
                adw_step=adw_step,
                event_category="response",
                event_type="ToolUseBlock",
                content=f"[Tool] {tool_name}",
                agent_id=agent_id,
                payload={
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_use_id": block.id,
                },
                summary=f"Using tool: {tool_name}",
            )
            asyncio.create_task(_summarize_and_update(
                log_id, adw_id, {"tool_name": tool_name, "tool_input": tool_input}, "ToolUseBlock"
            ))

    async def on_result(msg: ResultMessage) -> None:
        """Log final result with usage stats."""
        await log_adw_event(
            adw_id=adw_id,
            adw_step=adw_step,
            event_category="response",
            event_type="result",
            content=msg.result if msg.result else "",
            agent_id=agent_id,
            payload={
                "subtype": msg.subtype.value if msg.subtype else None,
                "usage": msg.usage.model_dump() if msg.usage else None,
                "session_id": msg.session_id,
            },
            summary=f"Step completed: {msg.subtype.value if msg.subtype else 'unknown'}",
        )

    return MessageHandlers(
        on_assistant_block=on_assistant_block,
        on_result=on_result,
    )


# =============================================================================
# WORKFLOW STEPS
# =============================================================================


async def run_plan_step(
    adw_id: str,
    orchestrator_agent_id: str,
    prompt: str,
    working_dir: str,
    model: str = ModelName.OPUS.value,
) -> tuple[bool, str | None, str | None]:
    """Run the /plan step.

    Args:
        adw_id: ADW ID for logging
        orchestrator_agent_id: Parent orchestrator agent ID
        prompt: The task prompt to plan
        working_dir: Working directory for the agent (worktree path)
        model: Model to use

    Returns:
        Tuple of (success, session_id, agent_id)
    """
    step_start_time = time.time()

    agent_id = await create_agent(
        orchestrator_agent_id=orchestrator_agent_id,
        name=f"plan-{adw_id[:8]}",
        model=model,
        working_dir=working_dir,
        adw_id=adw_id,
        adw_step=STEP_PLAN,
    )
    console.print(f"[dim]Created plan agent: {agent_id}[/dim]")

    await log_system_event(
        adw_id=adw_id,
        adw_step=STEP_PLAN,
        level="INFO",
        message=f"Created plan agent: {agent_id[:8]}",
        metadata={"agent_id": agent_id, "model": model},
    )

    await update_agent(agent_id=agent_id, status="executing", old_status="idle")

    await log_step_start(
        adw_id=adw_id,
        adw_step=STEP_PLAN,
        agent_id=agent_id,
        payload={"prompt": prompt, "model": model},
        summary=f"Starting plan step for: {prompt[:100]}...",
    )

    await update_adw_status(
        adw_id=adw_id,
        status="in_progress",
        current_step=STEP_PLAN,
    )

    console.print(Panel(
        f"[bold cyan]Step 1/{TOTAL_STEPS}: Plan[/bold cyan]\n\nPrompt: {prompt[:200]}...",
        title="ADW SDLC Workflow",
        width=console.width,
    ))

    try:
        await log_system_event(
            adw_id=adw_id,
            adw_step=STEP_PLAN,
            level="INFO",
            message=f"Executing plan command with model {model}",
            metadata={"prompt_preview": prompt[:100], "working_dir": working_dir},
        )

        plan_command = load_command("plan.md", {"$1": prompt})
        query_input = QueryInput(
            prompt=plan_command,
            options=QueryOptions(
                model=model,
                cwd=working_dir,
                allowed_tools=[
                    "Read", "Glob", "Grep", "Bash", "Write", "Edit",
                    "Task", "TodoWrite", "WebFetch", "WebSearch", "Skill",
                ],
                hooks=create_logging_hooks(adw_id, STEP_PLAN, agent_id),
                bypass_permissions=True,
            ),
            handlers=create_message_handlers(adw_id, STEP_PLAN, agent_id),
        )

        result = await query_to_completion(query_input)

        duration_ms = int((time.time() - step_start_time) * 1000)

        await update_agent(
            agent_id=agent_id,
            session_id=result.session_id,
            status="complete" if result.success else "blocked",
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            total_cost=result.usage.total_cost_usd if result.usage else None,
        )

        if result.success:
            console.print(f"[green]Plan step completed successfully[/green]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_PLAN,
                agent_id=agent_id,
                status="success",
                duration_ms=duration_ms,
                payload={"session_id": result.session_id},
                summary="Plan step completed successfully",
            )
            return True, result.session_id, agent_id
        else:
            console.print(f"[red]Plan step failed: {result.error}[/red]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_PLAN,
                agent_id=agent_id,
                status="failed",
                duration_ms=duration_ms,
                payload={"error": result.error},
                summary=f"Plan step failed: {result.error}",
            )
            return False, None, agent_id

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        console.print(f"[red]Plan step exception: {e}[/red]")
        await update_agent(agent_id=agent_id, status="blocked", old_status="executing")
        await log_step_end(
            adw_id=adw_id,
            adw_step=STEP_PLAN,
            agent_id=agent_id,
            status="failed",
            duration_ms=duration_ms,
            payload={"exception": str(e)},
            summary=f"Plan step exception: {e}",
        )
        return False, None, agent_id


async def extract_plan_path(
    working_dir: str,
    session_id: str | None,
    model: str = ModelName.OPUS.value,
) -> str | None:
    """Use quick_prompt to extract the plan file path.

    Args:
        working_dir: Working directory
        session_id: Session ID from plan step (for context)
        model: Model to use

    Returns:
        Path to the plan file, or None if not found
    """
    console.print("[cyan]Extracting plan file path...[/cyan]")

    extraction_prompt = """You just ran /plan and created a plan file.

IMPORTANT: Respond with ONLY the absolute file path to the plan file you created.
- No explanation
- No markdown
- No quotes
- Just the raw file path on a single line

Example correct response:
/Users/user/project/.ai/specs/feature-plan.md

What is the absolute path to the plan file you created?"""

    try:
        result = await quick_prompt(AdhocPrompt(
            prompt=extraction_prompt,
            model=model,
            cwd=working_dir,
        ))

        if result:
            path = result.strip().strip("`").strip('"').strip("'").strip()
            path = path.split("\n")[0].strip()

            if path.startswith("/") or path.startswith("./"):
                console.print(f"[green]Found plan file: {path}[/green]")
                return path
            else:
                console.print(f"[yellow]Unexpected path format: {path}[/yellow]")
                match = re.search(r'(/[^\s]+\.md)', result)
                if match:
                    path = match.group(1)
                    console.print(f"[green]Extracted path: {path}[/green]")
                    return path

        console.print("[yellow]Could not extract plan file path[/yellow]")
        return None

    except Exception as e:
        console.print(f"[red]Error extracting plan path: {e}[/red]")
        return None


async def run_build_step(
    adw_id: str,
    orchestrator_agent_id: str,
    plan_path: str,
    working_dir: str,
    model: str = ModelName.OPUS.value,
) -> tuple[bool, str | None, str | None]:
    """Run the /build step.

    Args:
        adw_id: ADW ID for logging
        orchestrator_agent_id: Parent orchestrator agent ID
        plan_path: Path to the plan file
        working_dir: Working directory for the agent (worktree path)
        model: Model to use

    Returns:
        Tuple of (success, session_id, agent_id)
    """
    step_start_time = time.time()

    agent_id = await create_agent(
        orchestrator_agent_id=orchestrator_agent_id,
        name=f"build-{adw_id[:8]}",
        model=model,
        working_dir=working_dir,
        adw_id=adw_id,
        adw_step=STEP_BUILD,
    )
    console.print(f"[dim]Created build agent: {agent_id}[/dim]")

    await log_system_event(
        adw_id=adw_id,
        adw_step=STEP_BUILD,
        level="INFO",
        message=f"Created build agent: {agent_id[:8]}",
        metadata={"agent_id": agent_id, "model": model},
    )

    await update_agent(agent_id=agent_id, status="executing", old_status="idle")

    await log_step_start(
        adw_id=adw_id,
        adw_step=STEP_BUILD,
        agent_id=agent_id,
        payload={"plan_path": plan_path, "model": model},
        summary=f"Starting build step with plan: {plan_path}",
    )

    await update_adw_status(
        adw_id=adw_id,
        status="in_progress",
        current_step=STEP_BUILD,
        completed_steps=1,
    )

    console.print(Panel(
        f"[bold cyan]Step 2/{TOTAL_STEPS}: Build[/bold cyan]\n\nPlan: {plan_path}",
        title="ADW SDLC Workflow",
        width=console.width,
    ))

    try:
        await log_system_event(
            adw_id=adw_id,
            adw_step=STEP_BUILD,
            level="INFO",
            message=f"Executing build command with plan: {plan_path}",
            metadata={"plan_path": plan_path, "working_dir": working_dir},
        )

        build_command = load_command("build.md", {"$ARGUMENTS": plan_path})
        query_input = QueryInput(
            prompt=build_command,
            options=QueryOptions(
                model=model,
                cwd=working_dir,
                allowed_tools=[
                    "Read", "Glob", "Grep", "Bash", "Write", "Edit",
                    "Task", "TodoWrite", "WebFetch", "WebSearch", "Skill",
                ],
                hooks=create_logging_hooks(adw_id, STEP_BUILD, agent_id),
                bypass_permissions=True,
            ),
            handlers=create_message_handlers(adw_id, STEP_BUILD, agent_id),
        )

        result = await query_to_completion(query_input)

        duration_ms = int((time.time() - step_start_time) * 1000)

        await update_agent(
            agent_id=agent_id,
            session_id=result.session_id,
            status="complete" if result.success else "blocked",
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            total_cost=result.usage.total_cost_usd if result.usage else None,
        )

        if result.success:
            console.print(f"[green]Build step completed successfully[/green]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_BUILD,
                agent_id=agent_id,
                status="success",
                duration_ms=duration_ms,
                payload={"session_id": result.session_id},
                summary="Build step completed successfully",
            )
            return True, result.session_id, agent_id
        else:
            console.print(f"[red]Build step failed: {result.error}[/red]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_BUILD,
                agent_id=agent_id,
                status="failed",
                duration_ms=duration_ms,
                payload={"error": result.error},
                summary=f"Build step failed: {result.error}",
            )
            return False, None, agent_id

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        console.print(f"[red]Build step exception: {e}[/red]")
        await update_agent(agent_id=agent_id, status="blocked", old_status="executing")
        await log_step_end(
            adw_id=adw_id,
            adw_step=STEP_BUILD,
            agent_id=agent_id,
            status="failed",
            duration_ms=duration_ms,
            payload={"exception": str(e)},
            summary=f"Build step exception: {e}",
        )
        return False, None, agent_id


async def run_test_step(
    adw_id: str,
    orchestrator_agent_id: str,
    user_prompt: str,
    plan_path: str,
    working_dir: str,
    model: str = ModelName.OPUS.value,
) -> tuple[bool, str | None, str | None, str | None]:
    """Run the /test step to write and execute tests.

    Args:
        adw_id: ADW ID for logging
        orchestrator_agent_id: Parent orchestrator agent ID
        user_prompt: Original user prompt describing the work
        plan_path: Path to the plan file that was implemented
        working_dir: Working directory for the agent (worktree path)
        model: Model to use

    Returns:
        Tuple of (success, session_id, agent_id, verdict)
        verdict is "PASS" or "FAIL" extracted from the test report
    """
    step_start_time = time.time()

    agent_id = await create_agent(
        orchestrator_agent_id=orchestrator_agent_id,
        name=f"test-{adw_id[:8]}",
        model=model,
        working_dir=working_dir,
        adw_id=adw_id,
        adw_step=STEP_TEST,
    )
    console.print(f"[dim]Created test agent: {agent_id}[/dim]")

    await log_system_event(
        adw_id=adw_id,
        adw_step=STEP_TEST,
        level="INFO",
        message=f"Created test agent: {agent_id[:8]}",
        metadata={"agent_id": agent_id, "model": model},
    )

    await update_agent(agent_id=agent_id, status="executing", old_status="idle")

    await log_step_start(
        adw_id=adw_id,
        adw_step=STEP_TEST,
        agent_id=agent_id,
        payload={
            "user_prompt": user_prompt,
            "plan_path": plan_path,
            "model": model,
        },
        summary=f"Starting test step for: {user_prompt[:80]}...",
    )

    await update_adw_status(
        adw_id=adw_id,
        status="in_progress",
        current_step=STEP_TEST,
        completed_steps=2,
    )

    console.print(Panel(
        f"[bold yellow]Step 3/{TOTAL_STEPS}: Test[/bold yellow]\n\n"
        f"Prompt: {user_prompt[:150]}...\n"
        f"Plan: {plan_path}",
        title="ADW SDLC Workflow",
        width=console.width,
    ))

    try:
        await log_system_event(
            adw_id=adw_id,
            adw_step=STEP_TEST,
            level="INFO",
            message=f"Executing test command with model {model}",
            metadata={
                "user_prompt_preview": user_prompt[:100],
                "plan_path": plan_path,
                "working_dir": working_dir,
            },
        )

        test_command = load_command("test.md", {"$1": user_prompt, "$2": plan_path})
        query_input = QueryInput(
            prompt=test_command,
            options=QueryOptions(
                model=model,
                cwd=working_dir,
                allowed_tools=[
                    "Read", "Glob", "Grep", "Bash", "Write", "Edit", "Skill",
                ],
                hooks=create_logging_hooks(adw_id, STEP_TEST, agent_id),
                bypass_permissions=True,
            ),
            handlers=create_message_handlers(adw_id, STEP_TEST, agent_id),
        )

        result = await query_to_completion(query_input)

        duration_ms = int((time.time() - step_start_time) * 1000)

        # Extract verdict from result
        verdict = None
        if result.success and result.result:
            result_text = result.result.upper()
            if "PASS" in result_text and "FAIL" not in result_text:
                verdict = "PASS"
            elif "FAIL" in result_text:
                verdict = "FAIL"

        await update_agent(
            agent_id=agent_id,
            session_id=result.session_id,
            status="complete" if result.success else "blocked",
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            total_cost=result.usage.total_cost_usd if result.usage else None,
        )

        if result.success:
            verdict_emoji = "✅" if verdict == "PASS" else "⚠️" if verdict == "FAIL" else "❓"
            console.print(f"[green]Test step completed: {verdict_emoji} {verdict or 'Unknown'}[/green]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_TEST,
                agent_id=agent_id,
                status="success",
                duration_ms=duration_ms,
                payload={
                    "session_id": result.session_id,
                    "verdict": verdict,
                },
                summary=f"Test completed: {verdict or 'Unknown verdict'}",
            )
            return True, result.session_id, agent_id, verdict
        else:
            console.print(f"[red]Test step failed: {result.error}[/red]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_TEST,
                agent_id=agent_id,
                status="failed",
                duration_ms=duration_ms,
                payload={"error": result.error},
                summary=f"Test step failed: {result.error}",
            )
            return False, None, agent_id, None

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        console.print(f"[red]Test step exception: {e}[/red]")
        await update_agent(agent_id=agent_id, status="blocked", old_status="executing")
        await log_step_end(
            adw_id=adw_id,
            adw_step=STEP_TEST,
            agent_id=agent_id,
            status="failed",
            duration_ms=duration_ms,
            payload={"exception": str(e)},
            summary=f"Test step exception: {e}",
        )
        return False, None, agent_id, None


async def extract_test_report_path(working_dir: str) -> str | None:
    """Find the most recently created test report file in .ai/test-reports/.

    Args:
        working_dir: Working directory

    Returns:
        Path to the test report file, or None if not found
    """
    console.print("[cyan]Finding test report path...[/cyan]")

    test_report_dir = Path(working_dir) / ".ai" / "test-reports"
    if not test_report_dir.exists():
        console.print("[yellow]No .ai/test-reports directory found[/yellow]")
        return None

    report_files = sorted(
        test_report_dir.glob("test_*.md"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )

    if report_files:
        report_path = str(report_files[0])
        console.print(f"[green]Found test report: {report_path}[/green]")
        return report_path

    console.print("[yellow]No test report files found in .ai/test-reports/[/yellow]")
    return None


async def run_review_step(
    adw_id: str,
    orchestrator_agent_id: str,
    user_prompt: str,
    plan_path: str,
    working_dir: str,
    model: str = ModelName.OPUS.value,
) -> tuple[bool, str | None, str | None, str | None]:
    """Run the /review step to validate completed work.

    Args:
        adw_id: ADW ID for logging
        orchestrator_agent_id: Parent orchestrator agent ID
        user_prompt: Original user prompt describing the work
        plan_path: Path to the plan file that was implemented
        working_dir: Working directory for the agent (worktree path)
        model: Model to use

    Returns:
        Tuple of (success, session_id, agent_id, verdict)
        verdict is "PASS" or "FAIL" extracted from the review
    """
    step_start_time = time.time()

    agent_id = await create_agent(
        orchestrator_agent_id=orchestrator_agent_id,
        name=f"review-{adw_id[:8]}",
        model=model,
        working_dir=working_dir,
        adw_id=adw_id,
        adw_step=STEP_REVIEW,
    )
    console.print(f"[dim]Created review agent: {agent_id}[/dim]")

    await log_system_event(
        adw_id=adw_id,
        adw_step=STEP_REVIEW,
        level="INFO",
        message=f"Created review agent: {agent_id[:8]}",
        metadata={"agent_id": agent_id, "model": model},
    )

    await update_agent(agent_id=agent_id, status="executing", old_status="idle")

    await log_step_start(
        adw_id=adw_id,
        adw_step=STEP_REVIEW,
        agent_id=agent_id,
        payload={
            "user_prompt": user_prompt,
            "plan_path": plan_path,
            "model": model,
        },
        summary=f"Starting review step for: {user_prompt[:80]}...",
    )

    await update_adw_status(
        adw_id=adw_id,
        status="in_progress",
        current_step=STEP_REVIEW,
        completed_steps=3,
    )

    console.print(Panel(
        f"[bold yellow]Step 4/{TOTAL_STEPS}: Review[/bold yellow]\n\n"
        f"Prompt: {user_prompt[:150]}...\n"
        f"Plan: {plan_path}",
        title="ADW SDLC Workflow",
        width=console.width,
    ))

    try:
        await log_system_event(
            adw_id=adw_id,
            adw_step=STEP_REVIEW,
            level="INFO",
            message=f"Executing review command with model {model}",
            metadata={
                "user_prompt_preview": user_prompt[:100],
                "plan_path": plan_path,
                "working_dir": working_dir,
            },
        )

        review_command = load_command("review.md", {"$1": user_prompt, "$2": plan_path})
        query_input = QueryInput(
            prompt=review_command,
            options=QueryOptions(
                model=model,
                cwd=working_dir,
                allowed_tools=[
                    "Read", "Glob", "Grep", "Bash", "Write", "Skill",
                ],
                hooks=create_logging_hooks(adw_id, STEP_REVIEW, agent_id),
                bypass_permissions=True,
            ),
            handlers=create_message_handlers(adw_id, STEP_REVIEW, agent_id),
        )

        result = await query_to_completion(query_input)

        duration_ms = int((time.time() - step_start_time) * 1000)

        # Extract verdict from result
        verdict = None
        if result.success and result.result:
            result_text = result.result.upper()
            if "PASS" in result_text and "FAIL" not in result_text:
                verdict = "PASS"
            elif "FAIL" in result_text:
                verdict = "FAIL"

        await update_agent(
            agent_id=agent_id,
            session_id=result.session_id,
            status="complete" if result.success else "blocked",
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            total_cost=result.usage.total_cost_usd if result.usage else None,
        )

        if result.success:
            verdict_emoji = "✅" if verdict == "PASS" else "⚠️" if verdict == "FAIL" else "❓"
            console.print(f"[green]Review step completed: {verdict_emoji} {verdict or 'Unknown'}[/green]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_REVIEW,
                agent_id=agent_id,
                status="success",
                duration_ms=duration_ms,
                payload={
                    "session_id": result.session_id,
                    "verdict": verdict,
                },
                summary=f"Review completed: {verdict or 'Unknown verdict'}",
            )
            return True, result.session_id, agent_id, verdict
        else:
            console.print(f"[red]Review step failed: {result.error}[/red]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_REVIEW,
                agent_id=agent_id,
                status="failed",
                duration_ms=duration_ms,
                payload={"error": result.error},
                summary=f"Review step failed: {result.error}",
            )
            return False, None, agent_id, None

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        console.print(f"[red]Review step exception: {e}[/red]")
        await update_agent(agent_id=agent_id, status="blocked", old_status="executing")
        await log_step_end(
            adw_id=adw_id,
            adw_step=STEP_REVIEW,
            agent_id=agent_id,
            status="failed",
            duration_ms=duration_ms,
            payload={"exception": str(e)},
            summary=f"Review step exception: {e}",
        )
        return False, None, agent_id, None


async def extract_review_path(working_dir: str) -> str | None:
    """Find the most recently created review file in .ai/reviews/.

    Args:
        working_dir: Working directory

    Returns:
        Path to the review file, or None if not found
    """
    console.print("[cyan]Finding review report path...[/cyan]")

    review_dir = Path(working_dir) / ".ai" / "reviews"
    if not review_dir.exists():
        console.print("[yellow]No .ai/reviews directory found[/yellow]")
        return None

    review_files = sorted(
        review_dir.glob("review_*.md"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )

    if review_files:
        review_path = str(review_files[0])
        console.print(f"[green]Found review file: {review_path}[/green]")
        return review_path

    console.print("[yellow]No review files found in .ai/reviews/[/yellow]")
    return None


async def run_docs_step(
    adw_id: str,
    orchestrator_agent_id: str,
    user_prompt: str,
    plan_path: str,
    working_dir: str,
    model: str = ModelName.OPUS.value,
) -> tuple[bool, str | None, str | None]:
    """Run the /docs step to generate documentation.

    Args:
        adw_id: ADW ID for logging
        orchestrator_agent_id: Parent orchestrator agent ID
        user_prompt: Original user prompt describing the work
        plan_path: Path to the plan file
        working_dir: Working directory for the agent (worktree path)
        model: Model to use

    Returns:
        Tuple of (success, session_id, agent_id)
    """
    step_start_time = time.time()

    agent_id = await create_agent(
        orchestrator_agent_id=orchestrator_agent_id,
        name=f"docs-{adw_id[:8]}",
        model=model,
        working_dir=working_dir,
        adw_id=adw_id,
        adw_step=STEP_DOCS,
    )
    console.print(f"[dim]Created docs agent: {agent_id}[/dim]")

    await log_system_event(
        adw_id=adw_id,
        adw_step=STEP_DOCS,
        level="INFO",
        message=f"Created docs agent: {agent_id[:8]}",
        metadata={"agent_id": agent_id, "model": model},
    )

    await update_agent(agent_id=agent_id, status="executing", old_status="idle")

    await log_step_start(
        adw_id=adw_id,
        adw_step=STEP_DOCS,
        agent_id=agent_id,
        payload={
            "user_prompt": user_prompt,
            "plan_path": plan_path,
            "model": model,
        },
        summary=f"Starting docs step for: {user_prompt[:80]}...",
    )

    await update_adw_status(
        adw_id=adw_id,
        status="in_progress",
        current_step=STEP_DOCS,
        completed_steps=4,
    )

    console.print(Panel(
        f"[bold magenta]Step 5/{TOTAL_STEPS}: Docs[/bold magenta]\n\n"
        f"Prompt: {user_prompt[:150]}...\n"
        f"Plan: {plan_path}",
        title="ADW SDLC Workflow",
        width=console.width,
    ))

    try:
        await log_system_event(
            adw_id=adw_id,
            adw_step=STEP_DOCS,
            level="INFO",
            message=f"Executing docs command with model {model}",
            metadata={
                "user_prompt_preview": user_prompt[:100],
                "plan_path": plan_path,
                "working_dir": working_dir,
            },
        )

        docs_command = load_command("docs.md", {"$1": user_prompt, "$2": plan_path})
        query_input = QueryInput(
            prompt=docs_command,
            options=QueryOptions(
                model=model,
                cwd=working_dir,
                allowed_tools=[
                    "Read", "Glob", "Grep", "Bash", "Write", "Edit", "Skill",
                ],
                hooks=create_logging_hooks(adw_id, STEP_DOCS, agent_id),
                bypass_permissions=True,
            ),
            handlers=create_message_handlers(adw_id, STEP_DOCS, agent_id),
        )

        result = await query_to_completion(query_input)

        duration_ms = int((time.time() - step_start_time) * 1000)

        await update_agent(
            agent_id=agent_id,
            session_id=result.session_id,
            status="complete" if result.success else "blocked",
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            total_cost=result.usage.total_cost_usd if result.usage else None,
        )

        if result.success:
            console.print(f"[green]Docs step completed successfully[/green]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_DOCS,
                agent_id=agent_id,
                status="success",
                duration_ms=duration_ms,
                payload={"session_id": result.session_id},
                summary="Docs step completed successfully",
            )
            return True, result.session_id, agent_id
        else:
            console.print(f"[red]Docs step failed: {result.error}[/red]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_DOCS,
                agent_id=agent_id,
                status="failed",
                duration_ms=duration_ms,
                payload={"error": result.error},
                summary=f"Docs step failed: {result.error}",
            )
            return False, None, agent_id

    except Exception as e:
        duration_ms = int((time.time() - step_start_time) * 1000)
        console.print(f"[red]Docs step exception: {e}[/red]")
        await update_agent(agent_id=agent_id, status="blocked", old_status="executing")
        await log_step_end(
            adw_id=adw_id,
            adw_step=STEP_DOCS,
            agent_id=agent_id,
            status="failed",
            duration_ms=duration_ms,
            payload={"exception": str(e)},
            summary=f"Docs step exception: {e}",
        )
        return False, None, agent_id


# =============================================================================
# MAIN WORKFLOW
# =============================================================================


async def run_workflow(adw_id: str) -> bool:
    """Run the complete SDLC workflow with worktree isolation and auto-PR.

    All 5 steps (plan, build, test, review, docs) run in the same worktree.
    A single PR is created at the end.

    Args:
        adw_id: The ADW ID to execute

    Returns:
        True if successful, False otherwise
    """
    workflow_start_time = time.time()

    await init_logging(verbose=False)

    console.print(Panel(
        f"[bold]Starting ADW SDLC Workflow[/bold]\n\nADW ID: {adw_id}",
        title="ADW Workflow",
        width=console.width,
    ))

    adw = await get_adw(adw_id)
    if not adw:
        console.print(f"[red]ADW not found: {adw_id}[/red]")
        return False

    orchestrator_agent_id = adw.get("orchestrator_agent_id")
    if not orchestrator_agent_id:
        console.print("[red]No orchestrator_agent_id found in ADW record[/red]")
        await update_adw_status(
            adw_id=adw_id,
            status="failed",
            error_message="No orchestrator_agent_id found",
        )
        return False
    orchestrator_agent_id = str(orchestrator_agent_id)

    input_data = adw.get("input_data", {})
    prompt = input_data.get("prompt")
    working_dir = input_data.get("working_dir")
    model = input_data.get("model", ModelName.OPUS.value)
    test_model = input_data.get("test_model", ModelName.OPUS.value)
    review_model = input_data.get("review_model", ModelName.OPUS.value)
    docs_model = input_data.get("docs_model", ModelName.OPUS.value)

    if not prompt:
        console.print("[red]No prompt found in ADW input_data[/red]")
        await update_adw_status(
            adw_id=adw_id,
            status="failed",
            error_message="No prompt found in input_data",
        )
        return False

    if not working_dir:
        console.print("[red]No working_dir found in ADW input_data[/red]")
        await update_adw_status(
            adw_id=adw_id,
            status="failed",
            error_message="No working_dir found in input_data",
        )
        return False

    console.print(f"[cyan]Prompt:[/cyan] {prompt[:200]}...")
    console.print(f"[cyan]Working Dir:[/cyan] {working_dir}")
    console.print(f"[cyan]Plan/Build Model:[/cyan] {model}")
    console.print(f"[cyan]Test Model:[/cyan] {test_model}")
    console.print(f"[cyan]Review Model:[/cyan] {review_model}")
    console.print(f"[cyan]Docs Model:[/cyan] {docs_model}")

    await log_system_event(
        adw_id=adw_id,
        adw_step=None,
        level="INFO",
        message=f"Workflow started: {adw.get('adw_name', 'unknown')}",
        metadata={
            "prompt": prompt,
            "working_dir": working_dir,
            "model": model,
            "test_model": test_model,
            "review_model": review_model,
            "docs_model": docs_model,
            "total_steps": TOTAL_STEPS,
        },
    )

    try:
        # =================================================================
        # Create worktree in target repo (once for all steps)
        # =================================================================
        try:
            worktree_path, branch_name = create_worktree_in_target(
                working_dir=working_dir,
                adw_id=adw_id,
            )
        except RuntimeError as e:
            console.print(f"[red]Worktree creation failed: {e}[/red]")
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message=f"Worktree creation failed: {e}",
            )
            return False

        await log_system_event(
            adw_id=adw_id,
            adw_step=None,
            level="INFO",
            message=f"Created worktree: {worktree_path} (branch: {branch_name})",
        )

        # =================================================================
        # Step 1: Plan (runs inside worktree)
        # =================================================================
        plan_success, plan_session_id, plan_agent_id = await run_plan_step(
            adw_id=adw_id,
            orchestrator_agent_id=orchestrator_agent_id,
            prompt=prompt,
            working_dir=worktree_path,
            model=model,
        )

        if not plan_success:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Plan step failed",
                error_step=STEP_PLAN,
            )
            return False

        # Extract plan file path
        plan_path = await extract_plan_path(
            working_dir=worktree_path,
            session_id=plan_session_id,
            model=model,
        )

        if not plan_path:
            console.print("[yellow]Attempting fallback plan path detection...[/yellow]")
            specs_dir = Path(worktree_path) / ".ai" / "specs"
            if specs_dir.exists():
                md_files = sorted(specs_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
                if md_files:
                    plan_path = str(md_files[0])
                    console.print(f"[green]Found recent plan file: {plan_path}[/green]")

        if not plan_path:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Could not extract plan file path",
                error_step=STEP_PLAN,
            )
            return False

        # =================================================================
        # Step 2: Build (runs inside same worktree)
        # =================================================================
        build_success, build_session_id, build_agent_id = await run_build_step(
            adw_id=adw_id,
            orchestrator_agent_id=orchestrator_agent_id,
            plan_path=plan_path,
            working_dir=worktree_path,
            model=model,
        )

        if not build_success:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Build step failed",
                error_step=STEP_BUILD,
                completed_steps=1,
            )
            return False

        # =================================================================
        # Step 3: Test (runs inside same worktree)
        # =================================================================
        test_success, test_session_id, test_agent_id, test_verdict = await run_test_step(
            adw_id=adw_id,
            orchestrator_agent_id=orchestrator_agent_id,
            user_prompt=prompt,
            plan_path=plan_path,
            working_dir=worktree_path,
            model=test_model,
        )

        if not test_success:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Test step failed",
                error_step=STEP_TEST,
                completed_steps=2,
            )
            return False

        # =================================================================
        # Step 4: Review (runs inside same worktree)
        # =================================================================
        review_success, review_session_id, review_agent_id, review_verdict = await run_review_step(
            adw_id=adw_id,
            orchestrator_agent_id=orchestrator_agent_id,
            user_prompt=prompt,
            plan_path=plan_path,
            working_dir=worktree_path,
            model=review_model,
        )

        if not review_success:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Review step failed",
                error_step=STEP_REVIEW,
                completed_steps=3,
            )
            return False

        # =================================================================
        # Step 5: Docs (runs inside same worktree, always runs)
        # =================================================================
        docs_success, docs_session_id, docs_agent_id = await run_docs_step(
            adw_id=adw_id,
            orchestrator_agent_id=orchestrator_agent_id,
            user_prompt=prompt,
            plan_path=plan_path,
            working_dir=worktree_path,
            model=docs_model,
        )

        if not docs_success:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Docs step failed",
                error_step=STEP_DOCS,
                completed_steps=4,
            )
            return False

        # =================================================================
        # Push and create PR (once at end)
        # =================================================================
        pr_url = await push_and_create_pr(
            worktree_path=worktree_path,
            branch_name=branch_name,
            adw_id=adw_id,
            prompt=prompt,
        )

        # =================================================================
        # Workflow Completed
        # =================================================================
        duration_seconds = int(time.time() - workflow_start_time)

        await update_adw_status(
            adw_id=adw_id,
            status="completed",
            completed_steps=TOTAL_STEPS,
        )

        test_report_path = await extract_test_report_path(worktree_path)
        review_path = await extract_review_path(worktree_path)

        await log_system_event(
            adw_id=adw_id,
            adw_step=None,
            level="INFO",
            message=f"Workflow completed in {duration_seconds}s - Test: {test_verdict or 'Unknown'}, Review: {review_verdict or 'Unknown'}",
            metadata={
                "plan_path": plan_path,
                "plan_session_id": plan_session_id,
                "plan_agent_id": plan_agent_id,
                "build_session_id": build_session_id,
                "build_agent_id": build_agent_id,
                "test_session_id": test_session_id,
                "test_agent_id": test_agent_id,
                "test_verdict": test_verdict,
                "test_report_path": test_report_path,
                "review_session_id": review_session_id,
                "review_agent_id": review_agent_id,
                "review_verdict": review_verdict,
                "review_path": review_path,
                "docs_session_id": docs_session_id,
                "docs_agent_id": docs_agent_id,
                "pr_url": pr_url,
                "worktree_path": worktree_path,
                "branch_name": branch_name,
                "duration_seconds": duration_seconds,
            },
        )

        test_color = "green" if test_verdict == "PASS" else "yellow"
        test_emoji = "✅" if test_verdict == "PASS" else "⚠️"
        review_color = "green" if review_verdict == "PASS" else "yellow"
        review_emoji = "✅" if review_verdict == "PASS" else "⚠️"

        console.print(Panel(
            f"[bold green]SDLC Workflow Completed![/bold green]\n\n"
            f"Duration: {duration_seconds}s\n"
            f"Plan file: {plan_path}\n"
            f"Test verdict: {test_emoji} {test_verdict or 'Unknown'}\n"
            f"Test report: {test_report_path or 'N/A'}\n"
            f"Review verdict: {review_emoji} {review_verdict or 'Unknown'}\n"
            f"Review report: {review_path or 'N/A'}\n"
            f"Docs generated: Yes\n"
            f"Worktree: {worktree_path}\n"
            f"Branch: {branch_name}\n"
            f"PR: {pr_url or 'N/A'}",
            title="ADW SDLC Complete",
            width=console.width,
        ))

        return True

    except Exception as e:
        console.print(f"[red]Workflow exception: {e}[/red]")
        await update_adw_status(
            adw_id=adw_id,
            status="failed",
            error_message=str(e),
        )
        await log_system_event(
            adw_id=adw_id,
            adw_step=None,
            level="ERROR",
            message=f"Workflow exception: {e}",
        )
        return False

    finally:
        await close_logging()


# =============================================================================
# CLI ENTRYPOINT
# =============================================================================


def main():
    """CLI entrypoint for the SDLC workflow."""
    parser = argparse.ArgumentParser(
        description="ADW SDLC Workflow - Five-step: plan, build, test, review, docs with worktree isolation and auto-PR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--adw-id",
        required=True,
        help="ADW ID (UUID) to execute",
    )

    args = parser.parse_args()

    success = asyncio.run(run_workflow(args.adw_id))

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
