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
ADW Test Workflow - Standalone test step with worktree isolation and auto-PR.

This workflow:
1. Receives --adw-id as CLI arg
2. Fetches prompt, working_dir, and plan_path from DB
3. Creates a git worktree in the target repo
4. Runs /test <prompt> <plan_path> agent inside the worktree
5. Extracts PASS/FAIL verdict from agent output
6. Pushes branch and creates PR for human review
7. Logs all events to agent_logs for swimlane visualization

Usage:
    uv run adws/adw_workflows/adw_test.py --adw-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
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
    QueryInput,
    QueryOptions,
    MessageHandlers,
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

STEP_TEST = "test"
TOTAL_STEPS = 1

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
        command_name: Command filename (e.g., "test.md", "build.md")
        variables: Dict mapping variable placeholders to values.
                   e.g., {"$1": "the prompt", "$2": "/path/to/plan"}
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
            f"## ADW Automated Workflow\n\n"
            f"**ADW ID:** `{adw_id}`\n"
            f"**Branch:** `{branch_name}`\n\n"
            f"### Prompt\n{prompt}\n\n"
            f"---\n"
            f"*Created by ADW automated workflow*"
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
        adw_step: Current step slug (e.g., "test")
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
            # Update database
            await update_log_summary(log_id, summary)
            # Broadcast to frontend so UI updates in real-time
            await broadcast_adw_event_summary_update(adw_id, log_id, summary)
    except Exception as e:
        # Log but don't fail - summary is non-critical
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


async def run_test_step(
    adw_id: str,
    orchestrator_agent_id: str,
    user_prompt: str,
    plan_path: str,
    working_dir: str,
    model: str = ModelName.OPUS.value,
) -> tuple[bool, str | None, str | None, str | None]:
    """Run the /test step.

    Args:
        adw_id: ADW ID for logging
        orchestrator_agent_id: Parent orchestrator agent ID
        user_prompt: The task prompt to test
        plan_path: Absolute path to the plan file
        working_dir: Working directory for the agent (worktree path)
        model: Model to use

    Returns:
        Tuple of (success, session_id, agent_id, verdict)
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
        payload={"prompt": user_prompt, "plan_path": plan_path, "model": model},
        summary=f"Starting test step for: {user_prompt[:100]}...",
    )

    await update_adw_status(
        adw_id=adw_id,
        status="in_progress",
        current_step=STEP_TEST,
    )

    console.print(Panel(
        f"[bold yellow]Step 1/{TOTAL_STEPS}: Test[/bold yellow]\n\nPrompt: {user_prompt[:200]}...\nPlan: {plan_path}",
        title="ADW Test Workflow",
        width=console.width,
    ))

    try:
        await log_system_event(
            adw_id=adw_id,
            adw_step=STEP_TEST,
            level="INFO",
            message=f"Executing test command with model {model}",
            metadata={"prompt_preview": user_prompt[:100], "plan_path": plan_path, "working_dir": working_dir},
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
        verdict: str | None = None
        if result.success and result.result:
            if "PASS" in result.result and "FAIL" not in result.result:
                verdict = "PASS"
            elif "FAIL" in result.result:
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
            console.print(f"[green]Test step completed successfully (verdict: {verdict})[/green]")
            await log_step_end(
                adw_id=adw_id,
                adw_step=STEP_TEST,
                agent_id=agent_id,
                status="success",
                duration_ms=duration_ms,
                payload={"session_id": result.session_id, "verdict": verdict},
                summary=f"Test step completed: {verdict or 'no verdict'}",
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
                payload={"error": result.error, "verdict": verdict},
                summary=f"Test step failed: {result.error}",
            )
            return False, None, agent_id, verdict

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


# =============================================================================
# MAIN WORKFLOW
# =============================================================================


async def run_workflow(adw_id: str) -> bool:
    """Run the test workflow with worktree isolation and auto-PR.

    Args:
        adw_id: The ADW ID to execute

    Returns:
        True if successful, False otherwise
    """
    workflow_start_time = time.time()

    await init_logging(verbose=False)

    console.print(Panel(
        f"[bold]Starting ADW Test Workflow[/bold]\n\nADW ID: {adw_id}",
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
    plan_path = input_data.get("plan_path")
    model = input_data.get("model", ModelName.OPUS.value)

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

    if not plan_path:
        console.print("[red]No plan_path found in ADW input_data[/red]")
        await update_adw_status(
            adw_id=adw_id,
            status="failed",
            error_message="No plan_path found in input_data",
        )
        return False

    console.print(f"[cyan]Prompt:[/cyan] {prompt[:200]}...")
    console.print(f"[cyan]Working Dir:[/cyan] {working_dir}")
    console.print(f"[cyan]Plan Path:[/cyan] {plan_path}")
    console.print(f"[cyan]Model:[/cyan] {model}")

    await log_system_event(
        adw_id=adw_id,
        adw_step=None,
        level="INFO",
        message=f"Workflow started: {adw.get('adw_name', 'unknown')}",
        metadata={
            "prompt": prompt,
            "working_dir": working_dir,
            "plan_path": plan_path,
            "model": model,
            "total_steps": TOTAL_STEPS,
        },
    )

    try:
        # =================================================================
        # Create worktree in target repo
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
        # Step 1: Test (runs inside worktree)
        # =================================================================
        test_success, test_session_id, test_agent_id, verdict = await run_test_step(
            adw_id=adw_id,
            orchestrator_agent_id=orchestrator_agent_id,
            user_prompt=prompt,
            plan_path=plan_path,
            working_dir=worktree_path,
            model=model,
        )

        if not test_success:
            await update_adw_status(
                adw_id=adw_id,
                status="failed",
                error_message="Test step failed",
                error_step=STEP_TEST,
            )
            return False

        # =================================================================
        # Push and create PR
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

        await log_system_event(
            adw_id=adw_id,
            adw_step=None,
            level="INFO",
            message=f"Workflow completed in {duration_seconds}s",
            metadata={
                "plan_path": plan_path,
                "verdict": verdict,
                "test_session_id": test_session_id,
                "test_agent_id": test_agent_id,
                "pr_url": pr_url,
                "worktree_path": worktree_path,
                "branch_name": branch_name,
                "duration_seconds": duration_seconds,
            },
        )

        console.print(Panel(
            f"[bold green]Test Workflow Completed![/bold green]\n\n"
            f"Duration: {duration_seconds}s\n"
            f"Plan file: {plan_path}\n"
            f"Verdict: {verdict or 'N/A'}\n"
            f"Worktree: {worktree_path}\n"
            f"Branch: {branch_name}\n"
            f"PR: {pr_url or 'N/A'}",
            title="ADW Test Complete",
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
    """CLI entrypoint for the test workflow."""
    parser = argparse.ArgumentParser(
        description="ADW Test Workflow - Standalone test with worktree isolation and auto-PR",
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
