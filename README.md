# AI Developer Workflows and Agent Experts

This repository demonstrates two powerful patterns for building production-ready AI agent systems:

1. **AI Developer Workflows (ADW)** - Autonomous multi-step workflows that plan, build, review, and fix code
2. **Agent Experts** - Self-improving agents that learn from their actions and build domain expertise

---

## 🤖 AI Developer Workflows (ADW)

> **The highest leverage point of agentic coding**: deterministic orchestration meets non-deterministic intelligence.

AI Developer Workflows represent a fundamental shift in how we build with AI agents. The insight is simple but profound:

**Raw agents are unreliable. Raw code is inflexible. Combined, they're unstoppable.**

Traditional software engineering gave us **deterministic workflows**—predictable, repeatable, but rigid. AI agents gave us **non-deterministic intelligence**—creative, adaptive, but unpredictable. ADWs fuse these paradigms: **deterministic Python code orchestrates non-deterministic Claude agents**, giving you the reliability of traditional engineering with the capability of frontier AI.

This is the past and future of engineering, combined as one.

### The Core Insight: Composable Workflow Steps

Each workflow is built from **interchangeable, composable steps**:

```
/plan   : always creates a spec file at a known path
/build  :  agent implements creatively based on spec
/review : always outputs risk-tiered report with PASS/FAIL
/fix    :  agent resolves issues based on review
```

The **orchestration is deterministic** (step order, file paths, status updates, database writes). The **execution is non-deterministic** (agent reasoning, code generation, problem-solving). You get predictable structure with intelligent flexibility.

**Why this outperforms raw agents:**
- Agents can't reliably chain multi-step tasks—they lose context, forget goals, hallucinate state
- ADWs enforce structure: each step has clear inputs, outputs, and success criteria
- Failures are isolated to specific steps, not catastrophic workflow collapses
- This keeps each agent focused on ONE SINGLE TASK. Remember, One Agent, One Prompt, One Purpose.

**Why this outperforms raw code:**
- Traditional automation can't adapt to novel problems or ambiguous requirements
- ADWs leverage frontier model reasoning for creative problem-solving
- The agent handles the hard part (thinking), the code handles the boring part (orchestration)

### Workflow Types

Workflows are **composable**—mix and match steps for your use case:

| Workflow                | Steps | Use Case                                                    |
| ----------------------- | ----- | ----------------------------------------------------------- |
| `plan_build`            | 2     | Quick features—plan the implementation, then build it       |
| `plan_build_review`     | 3     | Quality-focused—adds risk-tiered code review after building |
| `plan_build_review_fix` | 4     | Full automation—automatically fixes issues found in review  |

**Build your own**: Create `adw_<your_workflow>.py` by composing steps. Want `plan_build_test`? Copy a workflow, swap `/review` for `/test`. The pattern is the product.

### How It Works

```
1. TRIGGER   →  User says "start adw: plan_build: Build a todo app"
2. CREATE    →  Backend creates ADW record in PostgreSQL
3. SPAWN     →  Background process: `uv run adws/adw_workflows/adw_plan_build.py --adw-id <uuid>`
4. EXECUTE   →  Workflow runs steps sequentially, each step:
                - Creates an agent record
                - Runs a slash command (/plan, /build, /review, /fix)
                - Logs events to agent_logs table
                - Broadcasts via WebSocket in real-time
5. COMPLETE  →  ADW marked complete/failed, duration recorded
```

### Architecture

```
adws/
├── adw_modules/           # Core infrastructure
│   ├── adw_agent_sdk.py   # Typed Pydantic wrapper for Claude Agent SDK
│   ├── adw_logging.py     # Step lifecycle logging (log_step_start, log_step_end)
│   ├── adw_websockets.py  # Real-time WebSocket broadcasting to frontend
│   ├── adw_summarizer.py  # AI-powered event summaries using Claude Haiku
│   └── adw_database.py    # PostgreSQL operations (agents, logs, ADWs)
│
├── adw_workflows/         # Multi-step workflow implementations
│   ├── adw_plan_build.py           # 2-step: /plan → /build
│   ├── adw_plan_build_review.py    # 3-step: /plan → /build → /review
│   └── adw_plan_build_review_fix.py # 4-step: /plan → /build → /review → /fix
│
└── adw_triggers/          # How workflows get started
    ├── adw_manual_trigger.py  # CLI trigger for testing
    └── adw_scripts.py         # Spawns background processes via `uv run`
```

### Starting an ADW

**Via CLI:**
```bash
uv run adws/adw_triggers/adw_manual_trigger.py \
  "markdown-preview" \
  "plan_build_review" \
  "Create a markdown preview app with live rendering" \
  "/path/to/project"
```

### Key Implementation Details

**Typed Agent SDK Wrapper** (`adw_agent_sdk.py`):
- Pydantic models for all Claude SDK types (QueryInput, HooksConfig, MessageHandlers)
- ModelName enum with OPUS, SONNET, HAIKU aliases
- `query_to_completion()` for full agent runs with hooks
- `quick_prompt()` for fast single-shot queries

**WebSocket Broadcasting** (`adw_websockets.py`):
- Resilient client that fails silently if server unavailable
- Event types: `adw_created`, `adw_event`, `adw_step_change`, `adw_status`

**AI Event Summaries** (`adw_summarizer.py`):
- Uses Claude Haiku for cheap, fast summarization
- Generates 1-sentence summaries (50-100 chars) for each event
- Summaries stored in database for each event

### Learn More: Talk to the ADW Expert

Want to understand how ADWs work in this codebase? Ask the ADW agent expert:

```bash
/experts:adw:question "How do I create a new workflow type?"
/experts:adw:question "What happens when a workflow step fails?"
/experts:adw:question "How do I add a new step to a workflow?"
```

The expert reads from a curated expertise file (`.claude/commands/experts/adw/expertise.yaml`) and validates against the actual codebase to give you accurate, up-to-date answers.

---

## 🧠 Agent Experts

> Finally, agents that actually learn.

The massive problem with agents is this: **your agents forget**. And that means your agents don't learn. Traditional software improves as it's used—storing analytics, patterns, and data that create better algorithms. Agents of today don't.

**Agent Experts** solve this with a three-step workflow that transforms forgetful agents into self-improving specialists.

### The Core Pattern: ACT → LEARN → REUSE

```
ACT    →  Agent takes a useful action (builds, fixes, answers)
LEARN  →  Agent stores new information in its expertise file
REUSE  →  Agent uses that expertise on the next execution
```

The difference between a generic agent and an Agent Expert is simple: **one executes and forgets, the other executes and learns**.

## 📁 Mental Models, Not Sources of Truth

The **expertise file** is your agent's mental model—a data structure that evolves over time. Just like the working memory you have of your codebases, this is NOT another source of truth.

> The code is always the source of truth. The expertise file is your agent's working memory that it validates against the code.

**Key insight**: You don't manually update expertise files. You teach your agents how to learn by writing self-improve prompts. The agent manages its own mental model.

## 🎯 Two Types of Agent Experts

### 1. Codebase Experts

Deploy experts for high-risk or complex areas of your codebase:

| Expert Domain | Use Case                                   |
| ------------- | ------------------------------------------ |
| **Database**  | Schema changes, migrations, query patterns |
| **WebSocket** | Real-time events, streaming architecture   |
| **Billing**   | Payment flows, subscription logic          |
| **Security**  | Auth patterns, permission systems          |

**See implementation**: [`.claude/commands/experts/`](.claude/commands/experts/)

For example, each expert can have:
- `expertise.yaml` - The mental model (~600-1000 lines of structured knowledge)
- `question.md` - Query the expert without making changes (reuse)
- `self-improve.md` - Sync expertise against the source of truth (the code) (learn)
- `plan.md` - Create domain-aware implementation plans (reuse)
- `plan_build_improve.md` - Composite prompt chain (or agentic workflow) that plans, builds, and improves the expertise file. (True expert workflow: act → learn → reuse)
- `other.md` - Other prompts that use the expertise file (act or reuse) that specializes in the experts domain.

### 2. Product Experts

Build adaptive user experiences where agents personalize based on behavior:

```
ACT    →  User views product, adds to cart, or checks out
LEARN  →  System updates user's Expertise JSONB in database
REUSE  →  Agent generates personalized home page sections
```

## 🔧 Building Your Own Agent Expert

### Step 1: Create the Expertise File (Mental Model)

```yaml
# .claude/commands/experts/<domain>/expertise.yaml
overview:
  description: "What this system does"
  key_files:
    - "path/to/critical/file.py"

core_implementation:
  # Structure your domain knowledge here
  # Let the agent define and maintain this structure
```

### Step 2: Create the Self-Improve Prompt

The self-improve prompt teaches your agent HOW to learn:

```markdown
# Purpose
Maintain expertise accuracy by comparing against actual codebase.

# Workflow
1. Optionally check git diff for recent changes
2. Read current expertise file
3. Validate against codebase (READ the actual files)
4. Identify discrepancies
5. Update expertise file
6. Enforce line limit (keep it focused)
```

### Step 3: Create Domain-Specific Commands

- **Question prompt**: Query expertise without changes
- **Plan prompt**: Create expertise-informed implementation plans
- **Action prompts**: Domain-specific workflows

### Step 4: Run Self-Improve Until Stable

```bash
# Run until your agent stops finding new things to update
/experts:<domain>:self-improve true
```

## 📂 Expert Files Reference

```
.claude/commands/experts/
└── adw/
    ├── expertise.yaml      # ADW mental model (~270 lines of structured knowledge)
    ├── question.md         # Query without coding
    └── self-improve.md     # Sync expertise with code
```

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.12+** with [Astral UV](https://docs.astral.sh/uv/)
- **Anthropic API key** ([Get one here](https://console.anthropic.com/))
- **Claude Code CLI** installed
- **PostgreSQL database** (for ADW event logging) - [NeonDB](https://neon.tech) recommended

### Setup

```bash
# Install Astral UV (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Copy and configure environment
cp .env.sample .env
# Edit .env: set ANTHROPIC_API_KEY and DATABASE_URL
```

### Try It Out

```bash
# Plan a feature (inside any target repo)
claude -p "/plan Add user authentication"

# Build from a plan
claude -p "/build .ai/specs/add-user-auth.md"

# Review code changes
claude -p "/review"

# Full pipeline via ADW trigger
uv run adws/adw_triggers/adw_manual_trigger.py \
  "auth-feature" \
  "plan_build_review_fix" \
  "Add user authentication" \
  "/path/to/repo"

# Scout a codebase with parallel agents
claude -p "/plan_w_scouters Investigate the auth system"
```

---

## 🗂️ Project Structure

```
.
├── .claude/
│   ├── commands/                  # Slash commands
│   │   ├── plan.md               # /plan - Create implementation spec
│   │   ├── build.md              # /build - Implement from spec
│   │   ├── review.md             # /review - Risk-tiered code review
│   │   ├── fix.md                # /fix - Fix review issues
│   │   ├── prime.md              # /prime - Analyze codebase
│   │   └── experts/
│   │       └── adw/
│   │           ├── expertise.yaml    # ADW mental model
│   │           ├── question.md       # Query ADW expert
│   │           └── self-improve.md   # Sync expertise with code
│   │
│   └── agents/                    # Subagent templates
│       ├── scout-report-suggest.md       # Codebase analysis (Opus)
│       ├── scout-report-suggest-fast.md  # Fast analysis (Haiku)
│       └── build-agent.md                # Parallel file builder
│
├── adws/
│   ├── adw_modules/               # Core infrastructure
│   │   ├── adw_agent_sdk.py       # Typed Claude Agent SDK wrapper
│   │   ├── adw_logging.py         # Step lifecycle logging
│   │   ├── adw_websockets.py      # WebSocket broadcasting
│   │   ├── adw_summarizer.py      # AI event summaries (Haiku)
│   │   └── adw_database.py        # PostgreSQL operations
│   │
│   ├── adw_workflows/             # Multi-step workflows
│   │   ├── adw_plan_build.py              # /plan → /build
│   │   ├── adw_plan_build_review.py       # /plan → /build → /review
│   │   └── adw_plan_build_review_fix.py   # /plan → /build → /review → /fix
│   │
│   └── adw_triggers/              # Workflow launchers
│       ├── adw_manual_trigger.py  # CLI trigger
│       └── adw_scripts.py         # Background process spawner
│
├── .env.sample                    # Environment variable template
├── CLAUDE.md                      # Engineering rules for AI agents
└── README.md
```

## 📚 Resources

- **Claude Code Docs**: https://docs.anthropic.com/en/docs/claude-code
