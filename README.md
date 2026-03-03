# Hivemind

**An interactive AI assistant for the terminal with multi-agent swarm orchestration.**

Hivemind is a feature-rich command-line interface that connects to multiple LLM providers (Ollama, Anthropic, OpenAI) and offers capabilities like multi-agent collaboration, secure code execution, session persistence, multimodal input, web fetching, and more — all from your terminal.

```
  ✦ Hivemind v9.0.0
  ╭─────────────────────────────────────────╮
  │  ✦ Welcome to Hivemind!                    │
  │  Model: llama3.2 | Provider: ollama     │
  │  Type /help for commands                │
  ╰─────────────────────────────────────────╯
```

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Commands Reference](#commands-reference)
- [Multi-Provider Support](#multi-provider-support)
- [Multi-Agent Swarm](#multi-agent-swarm)
- [Architecture](#architecture)
- [Version History](#version-history)

---

## Features

| Category | Features |
|----------|----------|
| **Chat** | Streaming responses, system prompt, conversation history, undo/redo |
| **Providers** | Ollama (local), Anthropic (Claude), OpenAI (GPT) with hot-switching |
| **Agents** | Multi-agent swarm with orchestrator, dependency graph, parallel execution |
| **Code** | Secure Python sandbox, auto-run detection, /run command |
| **Files** | File attachment with security validation, image input (multimodal) |
| **Sessions** | Save/load/fork conversations, export to Markdown/JSON |
| **Shell** | `!command` execution, `!!command` to feed output to the LLM |
| **Web** | URL fetching with HTML-to-text extraction |
| **UI** | Animated Phoenix mascot, smart streaming buffer, Rich panels |
| **Utilities** | Aliases, prompt library, token/cost tracking, diff, multiline mode |

---

## Installation

### Requirements

- Python 3.12+
- [Ollama](https://ollama.ai) (for local models, default provider)

### Quick Install (recommended)

The installer creates an isolated environment and makes `hivemind` available globally — no source code is exposed.

**macOS / Linux:**

```bash
./install.sh
```

**Windows:**

```cmd
install.cmd
```

This installs Hivemind to `~/.hivemind-env/` and links the `hivemind` command to your PATH.

### Uninstall

```bash
./install.sh --uninstall    # macOS / Linux
install.cmd --uninstall     # Windows
```

### Developer Install (from source)

```bash
pip install -e .                 # Editable mode
pip install -e ".[all]"          # With Anthropic + OpenAI SDKs
```

### Build for Distribution

```bash
./build.sh      # Creates dist/hivemind_ai-<version>.whl
```

Distribute the `.whl` file together with `install.sh` / `install.cmd`. The installer compiles Python to bytecode and removes source files — end users only see compiled `.pyc` files.

On first run, Hivemind creates `~/.hivemind/` with default configuration files.

---

## Quick Start

```bash
# Interactive mode
hivemind

# Demo mode (no Ollama required, explore the UI)
hivemind --demo

# Pipe mode (one-shot, no interactive prompt)
echo "Explain quicksort" | hivemind

# Pipe from file
cat question.txt | hivemind

# Alternative: run as Python module
python -m hivemind
```

---

## Commands Reference

### Chat & History

| Command | Description |
|---------|-------------|
| `/help` | Show all available commands |
| `/clear` | Clear screen and conversation history |
| `/history` | Display conversation history |
| `/undo` | Remove last user + assistant message pair |
| `/redo` | Restore last undone message pair |
| `/diff` | Compare the last two assistant responses (unified diff) |
| `/compact` | Summarize older messages using the LLM to reduce context |
| `/compact hard` | Drop older messages, keep only the most recent 6 |

### Model & Provider

| Command | Description |
|---------|-------------|
| `/model <name>` | Switch to a different model |
| `/model list` | List available models from the current provider |
| `/models` | Alias for `/model list` |
| `/provider <name>` | Switch provider (`ollama`, `anthropic`, `openai`) |
| `/providers` | Show provider configuration status |
| `/system <prompt>` | Set the system prompt |
| `/system clear` | Reset to the default system prompt |

### File & Image

| Command | Description |
|---------|-------------|
| `/file <path>` | Attach a file to the conversation context |
| `/file list` | List currently attached files |
| `/file remove <path>` | Remove an attached file |
| `/file clear` | Remove all attached files |
| `/image <path>` | Queue an image for the next message |
| `/image <path> <prompt>` | Send an image immediately with a prompt |

### Code Execution

| Command | Description |
|---------|-------------|
| `/run <code>` | Execute Python code in a secure sandbox |
| Auto-run | When the LLM generates Python code blocks, Hivemind offers to execute them |

### Sessions & Export

| Command | Description |
|---------|-------------|
| `/save <name>` | Save the current conversation |
| `/load <name>` | Load a saved conversation |
| `/sessions` | List saved sessions |
| `/sessions delete <name>` | Delete a saved session |
| `/fork [name]` | Fork the current conversation as a new saved session |
| `/export md` | Export conversation as Markdown |
| `/export json` | Export conversation as JSON |

### Agents & Swarm

| Command | Description |
|---------|-------------|
| `/agent create <name>` | Create a new agent (interactive setup) |
| `/agent list` | List all agents with their provider and model |
| `/agent delete <name>` | Delete an agent |
| `/agent info <name>` | Show detailed agent information |
| `/agent template <name>` | Create agent from a built-in template |
| `/swarm <task>` | Execute a task using the multi-agent swarm |

### Utilities

| Command | Description |
|---------|-------------|
| `/alias <name>=<value>` | Create a command alias |
| `/alias list` | List all aliases |
| `/alias delete <name>` | Delete an alias |
| `/prompt save <name>` | Save the current system prompt |
| `/prompt load <name>` | Load a saved prompt as the system prompt |
| `/prompt list` | List saved prompts |
| `/prompt delete <name>` | Delete a saved prompt |
| `/prompt show <name>` | Display a saved prompt's content |
| `/web <url>` | Fetch a URL and display its text content |
| `/web <url> <prompt>` | Fetch a URL and send its content to the LLM |
| `/multi` | Toggle multiline input mode |
| `/cost` | Show token usage and estimated cost for the session |
| `/exit` | Exit Hivemind |

### Shell Integration

| Syntax | Description |
|--------|-------------|
| `!<command>` | Run a shell command and display the output |
| `!!<command>` | Run a shell command and send the output to the LLM for analysis |

---

## Multi-Provider Support

Hivemind supports three LLM backends. Configuration is stored in `~/.hivemind/providers.json`.

```json
{
  "ollama_base_url": "http://localhost:11434",
  "anthropic_api_key": "",
  "openai_api_key": "",
  "openai_base_url": "https://api.openai.com/v1"
}
```

| Provider | Setup | Notes |
|----------|-------|-------|
| **Ollama** | Install Ollama, pull a model (`ollama pull llama3.2`) | Default, local, free. Supports vision models. |
| **Anthropic** | Set `anthropic_api_key` in config | Requires `pip install anthropic`. |
| **OpenAI** | Set `openai_api_key` in config | Requires `pip install openai`. Custom `base_url` supports Groq, Together, etc. |

All providers are wrapped with a **RetryProvider** that handles transient errors with exponential backoff (up to 3 retries). Mid-stream errors are propagated immediately.

Switch providers mid-conversation with `/provider <name>` — history is preserved.

---

## Multi-Agent Swarm

The swarm system lets multiple specialized AI agents collaborate on a task.

### How it Works

1. **You run** `/swarm "Build a REST API for a todo app"`
2. **The Orchestrator** (a special agent) analyzes the task and creates an execution plan as a JSON dependency graph
3. **Agents execute** their subtasks in parallel where dependencies allow, using `asyncio.gather` with event-based synchronization
4. **The Orchestrator synthesizes** all results into a final cohesive response

### Built-in Agent Templates

| Template | Specialty |
|----------|-----------|
| `coder` | Expert software engineer — clean, efficient code with best practices |
| `reviewer` | Senior code reviewer — bugs, security, performance, style |
| `researcher` | Technical analyst — information synthesis, comparisons, citations |
| `writer` | Technical writer — documentation, tutorials, audience-appropriate formatting |

### Example

```
/agent template coder
/agent template reviewer
/swarm "Write a binary search function with full test coverage"
```

The orchestrator will assign the coder to write the function, the reviewer to audit it, and synthesize the final result.

### Execution Plan Format

The orchestrator generates a JSON dependency graph:

```json
{
  "plan": [
    {"id": "task_1", "agent": "coder", "task": "Write binary search", "depends_on": []},
    {"id": "task_2", "agent": "reviewer", "task": "Review the code", "depends_on": ["task_1"]}
  ]
}
```

Tasks without dependencies run in parallel. Tasks with dependencies wait for their prerequisites to complete, receiving the results as context.

---

## Architecture

### Project Structure

```
python_cli/
├── pyproject.toml      # Package metadata, dependencies, entry point
├── README.md
└── hivemind/              # Main package
    ├── __init__.py     # Version
    ├── __main__.py     # python -m hivemind support
    ├── cli.py          # Main CLI — UI, commands, chat loop, all interactive features
    ├── providers.py    # Multi-provider abstraction (Ollama, Anthropic, OpenAI)
    ├── agents.py       # Agent registry, templates, CRUD, orchestrator prompt
    ├── swarm.py        # Swarm orchestration, dependency graph, parallel execution
    ├── sandbox.py      # Secure Python code execution with resource limits
    └── sessions.py     # Session save/load/list/delete persistence
```

### Configuration Files

```
~/.hivemind/
├── providers.json       # API keys and provider URLs
├── agents.json          # Registered agents
├── aliases.json         # Command aliases
├── prompts/             # Saved system prompts (.txt files)
└── sessions/            # Saved conversations (.json files)
```

### Key Design Decisions

- **Providers are stateless** — they don't hold conversation history. The chat loop in `index.py` manages all state.
- **Lazy SDK imports** — `anthropic` and `openai` are imported only when needed, so they're truly optional.
- **StreamBuffer** reduces terminal flicker by batching tokens and only re-rendering at logical block boundaries (paragraphs, code fences, list items, headings).
- **Security-first file handling** — blocked patterns for `.env`, `.pem`, `.key`, SSH configs, AWS credentials. Binary files rejected. 100KB per-file limit.
- **Sandbox isolation** — code execution uses subprocess with resource limits (128MB RAM, 10s CPU), temp directory isolation, and static analysis to block dangerous imports (`subprocess`, `socket`, `os.system`, `eval`, etc.).

---

## Version History

### v1.0 — Foundation

The initial release establishing the core interactive chat experience.

- Interactive chat loop with Ollama as the LLM backend
- Streaming token-by-token responses via `httpx` async streaming
- Rich terminal UI with styled panels and Markdown rendering
- **Phoenix** — animated ASCII mascot with state-based animations (idle, thinking, responding, happy, error) using Braille characters
- Basic slash commands: `/help`, `/clear`, `/exit`, `/model`, `/history`
- `prompt_toolkit` integration with command history (`~/.python_cli_history`) and tab completion
- Connection check and model auto-detection on startup

### v2.0 — Multi-Agent Swarm

Introduced the multi-agent architecture — the defining feature of Hivemind.

- **Multi-provider abstraction** (`providers.py`): `BaseProvider` ABC with `OllamaProvider`, `AnthropicProvider`, `OpenAIProvider`
- **Provider configuration** in `~/.hivemind/providers.json`
- **Agent system** (`agents.py`): `Agent` dataclass, `AgentRegistry` with CRUD and JSON persistence
- **4 built-in agent templates**: coder, reviewer, researcher, writer
- **Orchestrator agent** with specialized system prompt for task decomposition into JSON plans
- **Swarm engine** (`swarm.py`): `SwarmPlan` with dependency graph validation (cycle detection), `SwarmRunner` with parallel execution via `asyncio.gather` + `asyncio.Event` synchronization
- **Streaming synthesis** — orchestrator merges all agent results with streaming output
- **File context** (`FileContextManager`): attach files to conversations with security validation (blocked patterns, size limits, binary detection)
- `/agent create|list|delete|info|template` commands
- `/swarm <task>` command with live status panel (pending/thinking/responding/done/error per agent)
- `/providers` command to inspect configuration
- `/file <path>|list|remove|clear` commands

### v3.0 — Sandbox & Sessions

Added code execution safety and conversation persistence.

- **Secure Python sandbox** (`sandbox.py`): subprocess isolation with resource limits (128MB RAM, 10s CPU, 1MB file size)
- **Static code analysis**: blocked imports (`subprocess`, `socket`, `http`, `pickle`, etc.) and patterns (`os.system`, `eval`, `exec`, `open()`)
- `/run <code>` command for direct code execution
- **Session management** (`sessions.py`): save/load/list/delete conversations as JSON in `~/.hivemind/sessions/`
- `/save <name>`, `/load <name>`, `/sessions`, `/sessions delete <name>`
- **RetryProvider** wrapper with exponential backoff (3 retries, pre-token only) for transient network/server errors
- Session name validation (alphanumeric + underscores/hyphens, max 40 chars, max 50 sessions)

### v4.0 — Auto-Run, Export & System Prompt

Quality-of-life features for power users.

- **Auto-run code blocks**: Hivemind detects Python code blocks in LLM responses and offers to execute them in the sandbox, feeding results back to the LLM
- `/export md` and `/export json` for conversation export
- `/system <prompt>` to change the system prompt mid-session
- `/system clear` to reset to the default prompt
- Wired system prompt through the full chat pipeline

### v5.0 — Pipe Mode, Context Management & Model Switch

Made Hivemind scriptable and context-aware.

- **Pipe/stdin one-shot mode**: `echo "question" | python3 index.py` for non-interactive use
- Detects `sys.stdin.isatty()` and switches to one-shot streaming mode
- **Context window management**: `/compact` summarizes older messages using the LLM, `/compact hard` drops all but the 6 most recent messages
- Auto-compact warning when conversation exceeds 40 messages (~token estimation at 4 chars/token)
- `/model <name>` hot-switching — change model mid-conversation preserving history
- `/model list` and `/models` to browse available models with active indicator

### v6.0 — Smart Streaming, Undo/Redo & Provider Switch

Polished the core experience with intelligent rendering and conversation editing.

- **StreamBuffer** — smart re-rendering that batches tokens and only updates the display at logical block boundaries (paragraph breaks, code fences, list items, headings, sentence endings). Min interval 50ms, fallback 150ms. Eliminates terminal flicker.
- **UndoManager** — `/undo` removes the last user+assistant message pair (pushes to redo stack), `/redo` restores it. Redo stack is cleared on new messages.
- `/provider <name>` — hot-switch between `ollama`, `anthropic`, `openai` mid-conversation with history preserved
- Banner now shows current provider alongside model name

### v7.0 — Shell, Prompts & Fork

Terminal integration and prompt management.

- **Shell integration**: `!<command>` runs a shell command and displays output; `!!<command>` runs and sends the output to the LLM for analysis
- Shell execution with 30s timeout, 20K char output limit
- **Prompt library**: `/prompt save|load|list|delete|show` — save and reuse system prompts as `.txt` files in `~/.hivemind/prompts/`
- **Conversation fork**: `/fork [name]` creates a copy of the current conversation as a saved session, allowing divergent exploration

### v8.0 — Image Input, Cost Tracking & Diff

Multimodal capabilities and response analysis.

- **Image input**: `/image <path> [prompt]` — supports PNG, JPG, GIF, WebP, BMP (max 10MB)
- Images are encoded as base64 data URIs and converted to each provider's native format (Ollama `images` array, Anthropic `source.type: base64`, OpenAI `image_url`)
- Queue an image for the next message or send immediately with a prompt
- **Token/cost tracker**: `TokenTracker` estimates tokens (~4 chars/token) and costs per provider. `/cost` displays session usage summary with pricing for Anthropic, OpenAI, and Ollama (free).
- **Response diff**: `ResponseHistory` tracks recent responses. `/diff` displays a colorized unified diff between the last two assistant responses.

### v9.0 — Aliases, Multiline & Web Fetch

The final layer of productivity features.

- **Command aliases**: `/alias <name>=<value>` creates persistent shortcuts (e.g., `/alias gs=!git status`). Aliases are expanded automatically on input. Stored in `~/.hivemind/aliases.json`.
- `/alias list`, `/alias delete <name>` for management
- **Multiline toggle**: `/multi` switches between normal mode (Enter=submit, Esc+Enter=newline) and multiline mode (Enter=newline, Esc+Enter=submit). Prompt shows `[multi]` indicator when active.
- **Web fetch**: `/web <url>` fetches a URL, extracts text from HTML (via `_HTMLTextExtractor`), and displays a preview. `/web <url> <prompt>` sends the fetched content to the LLM with your prompt.
- HTML-to-text extraction strips scripts/styles/SVG, normalizes whitespace, max 50K chars, 10s timeout.

---

## Security

Hivemind takes security seriously across multiple layers:

- **File attachments**: blocked patterns for secrets (`.env`, `.pem`, `.key`, SSH/AWS configs), binary rejection, 100KB limit per file
- **Code sandbox**: subprocess isolation, resource limits (RAM/CPU/disk), blocked dangerous imports and patterns via static analysis
- **No credential leaks**: API keys stored in `~/.hivemind/providers.json` (user-local), never sent to other providers
- **Shell commands**: timeout enforcement, output truncation, explicit user invocation only

---

## License

MIT
