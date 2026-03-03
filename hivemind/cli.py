#!/usr/bin/env python3
"""Hivemind - An interactive AI assistant with swarm capabilities, powered by Ollama."""

import asyncio
import base64
import difflib
import html.parser
import json
import mimetypes
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .agents import AGENT_TEMPLATES, AgentRegistry
from .providers import BaseProvider, get_provider, load_provider_config
from .sandbox import ExecutionResult, execute_code, validate_code
from .sessions import (delete_session, list_sessions, load_session, save_session)
from .swarm import SwarmPlan, SwarmRunner, TaskStatus, demo_swarm
from . import __version__

# ═══ Config ══════════════════════════════════════════════════════════════════

APP_NAME = "Hivemind"
APP_VERSION = __version__
DEFAULT_MODEL = "llama3.2"
DEFAULT_SYSTEM_PROMPT = "You are a helpful AI assistant."
HISTORY_FILE = Path.home() / ".hivemind_history"


def build_system_prompt(base_prompt: str) -> str:
    """Enrich the system prompt with working directory context."""
    cwd = Path.cwd()
    parts = [base_prompt.rstrip()]

    # Working directory
    parts.append(f"\n\nWorking directory: {cwd}")

    # List top-level files (quick overview, max 30)
    try:
        entries = sorted(cwd.iterdir())
        files = [e.name + ("/" if e.is_dir() else "") for e in entries
                 if not e.name.startswith(".")][:30]
        if files:
            parts.append(f"Files: {', '.join(files)}")
    except PermissionError:
        pass

    return "\n".join(parts)

# Context window management
MAX_CONTEXT_MESSAGES = 40  # auto-compact threshold
COMPACT_KEEP_RECENT = 6   # messages to keep when compacting

# Prompt library
PROMPTS_DIR = Path.home() / ".hivemind" / "prompts"

# Shell integration
SHELL_TIMEOUT = 30  # seconds
MAX_SHELL_OUTPUT = 20_000  # chars

# Image input
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Aliases
ALIASES_FILE = Path.home() / ".hivemind" / "aliases.json"

# Web fetch
WEB_TIMEOUT = 10  # seconds
MAX_WEB_TEXT = 50_000  # chars

# Token pricing (USD per 1M tokens) — approximate, updated as needed
TOKEN_PRICING = {
    "anthropic": {"input": 3.00, "output": 15.00},    # Claude 3.5 Sonnet
    "openai": {"input": 2.50, "output": 10.00},       # GPT-4o
    "ollama": {"input": 0.0, "output": 0.0},          # Local, free
}

console = Console(theme=Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "dim": "dim white",
}))

# ═══ Phoenix (prompt mascot) ═════════════════════════════════════════════════

SPARKLE_STATES = {
    "idle": [
        ("⚡", "bold bright_yellow"), ("✦", "bold bright_red"),
        ("⚡", "bold yellow"), ("✦", "bold bright_yellow"),
        ("⚡", "bold bright_red"), ("✦", "bold yellow"),
    ],
    "thinking": [
        ("\u280b", "bold bright_red"), ("\u2819", "bold bright_yellow"),
        ("\u2839", "bold yellow"), ("\u2838", "bold bright_red"),
        ("\u283c", "bold bright_yellow"), ("\u2834", "bold yellow"),
        ("\u2826", "bold bright_red"), ("\u2827", "bold bright_yellow"),
        ("\u2807", "bold yellow"), ("\u280f", "bold bright_red"),
    ],
    "responding": [("⚡", "bold bright_yellow"), ("✦", "bold bright_red")],
    "happy": [("✦", "bold bright_yellow"), ("⚡", "bold bright_red")],
    "error": [("✦", "bold red")],
}

PT_COLORS = {
    "bold bright_red": "fg:#ff5555 bold",
    "bold bright_yellow": "fg:#f1fa8c bold",
    "bold yellow": "fg:#ffb86c bold",
    "bold bright_green": "fg:#50fa7b bold",
    "bold green": "fg:#50fa7b bold",
    "bold red": "fg:#ff5555 bold",
}


class Sparkle:
    def __init__(self):
        self.state = "idle"
        self._idx = 0
        self._tick = 0

    def set_state(self, state):
        if self.state != state:
            self.state = state
            self._idx = 0
            self._tick = 0

    def tick(self):
        self._tick += 1
        rate = 2 if self.state == "idle" else 1
        if self._tick % rate == 0:
            frames = SPARKLE_STATES[self.state]
            self._idx = (self._idx + 1) % len(frames)

    def _current(self):
        frames = SPARKLE_STATES[self.state]
        return frames[self._idx % len(frames)]

    def rich_str(self) -> tuple[str, str]:
        return self._current()

    def pt_char(self) -> str:
        return self._current()[0]

    def pt_style(self) -> str:
        _, s = self._current()
        return PT_COLORS.get(s, "")


# ═══ Secure File Context ═════════════════════════════════════════════════════

MAX_FILE_SIZE = 100 * 1024  # 100 KB
MAX_FILES = 10

# Patterns matched against the resolved filename (case-insensitive)
BLOCKED_PATTERNS = [
    r"\.env($|\.)",           # .env, .env.local, .env.production
    r"\.pem$",
    r"\.key$",
    r"\.p12$",
    r"\.pfx$",
    r"\.jks$",
    r"\.keystore$",
    r"id_rsa",
    r"id_ed25519",
    r"id_ecdsa",
    r"id_dsa",
    r"\.secret",
    r"credentials",
    r"\.password",
    r"token\.json$",
    r"tokens\.json$",
    r"auth\.json$",
    r"\.htpasswd$",
    r"shadow$",
    r"master\.key$",
    r"\.aws/",
    r"\.ssh/",
    r"\.gnupg/",
    r"\.kube/config",
]
_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]

# Only allow text-like MIME types
ALLOWED_MIME_PREFIXES = ("text/", "application/json", "application/xml",
                         "application/javascript", "application/x-yaml",
                         "application/toml", "application/sql")


def _is_blocked_file(resolved: Path) -> str | None:
    """Return a reason string if file should be blocked, else None."""
    name = str(resolved)
    for pattern in _BLOCKED_RE:
        if pattern.search(name):
            return f"Blocked: matches sensitive pattern '{pattern.pattern}'"
    return None


def _is_text_file(path: Path) -> bool:
    """Heuristic: check MIME type and try reading a small chunk."""
    mime, _ = mimetypes.guess_type(str(path))
    if mime and any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        return True
    # For files with no or unknown MIME, sample the first 512 bytes
    if mime and mime.startswith(("image/", "audio/", "video/", "application/octet")):
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(512)
        # NUL byte check — binary files usually contain \x00
        if b"\x00" in chunk:
            return False
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def validate_file(raw_path: str, cwd: Path) -> tuple[Path | None, str]:
    """Validate and resolve a file path securely.
    Returns (resolved_path, error_message). Error is empty on success."""
    if not raw_path.strip():
        return None, "No path provided."

    target = Path(raw_path.strip()).expanduser()

    # Resolve to absolute, following symlinks
    try:
        if not target.is_absolute():
            target = (cwd / target)
        resolved = target.resolve(strict=True)
    except (OSError, ValueError) as e:
        return None, f"Cannot resolve path: {e}"

    if not resolved.is_file():
        return None, f"Not a file: {resolved}"

    # Block sensitive files
    reason = _is_blocked_file(resolved)
    if reason:
        return None, reason

    # Size limit
    size = resolved.stat().st_size
    if size > MAX_FILE_SIZE:
        return None, f"File too large: {size:,} bytes (max {MAX_FILE_SIZE:,})"
    if size == 0:
        return None, "File is empty."

    # Text check
    if not _is_text_file(resolved):
        return None, "File appears to be binary. Only text files are supported."

    return resolved, ""


def read_file_safe(path: Path) -> str:
    """Read a validated text file with UTF-8 decoding."""
    return path.read_text(encoding="utf-8", errors="replace")


class FileContextManager:
    """Manages attached file contexts with security validation."""

    def __init__(self):
        self.files: dict[str, str] = {}  # resolved_path_str → content
        self._cwd = Path.cwd()

    def add(self, raw_path: str) -> tuple[bool, str]:
        """Add a file. Returns (success, message)."""
        if len(self.files) >= MAX_FILES:
            return False, f"Max {MAX_FILES} files attached. Use /file clear first."

        resolved, error = validate_file(raw_path, self._cwd)
        if error:
            return False, error

        key = str(resolved)
        if key in self.files:
            return False, f"Already attached: {resolved.name}"

        content = read_file_safe(resolved)
        self.files[key] = content
        return True, f"Attached: {resolved.name} ({len(content):,} chars)"

    def remove(self, raw_path: str) -> tuple[bool, str]:
        """Remove a file by path."""
        target = Path(raw_path.strip()).expanduser()
        try:
            if not target.is_absolute():
                target = (self._cwd / target)
            resolved = target.resolve(strict=False)
        except (OSError, ValueError):
            return False, "Invalid path."

        key = str(resolved)
        if key in self.files:
            del self.files[key]
            return True, f"Removed: {resolved.name}"
        return False, "File not in context."

    def clear(self) -> str:
        count = len(self.files)
        self.files.clear()
        return f"Cleared {count} file(s) from context."

    def list_files(self) -> list[tuple[str, int]]:
        """Return list of (filename, char_count)."""
        return [(Path(k).name, len(v)) for k, v in self.files.items()]

    def build_context_block(self) -> str:
        """Build a context string to prepend to user messages."""
        if not self.files:
            return ""
        parts = ["<attached_files>"]
        for path_str, content in self.files.items():
            name = Path(path_str).name
            parts.append(f"\n--- {name} ---\n{content}\n--- end {name} ---")
        parts.append("</attached_files>\n\n")
        return "\n".join(parts)


# ═══ Auto-Run: Code Block Extraction ═════════════════════════════════════════

_CODE_BLOCK_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


def extract_python_blocks(text: str) -> list[str]:
    """Extract Python code blocks from markdown-formatted LLM responses."""
    return [m.group(1).strip() for m in _CODE_BLOCK_RE.finditer(text) if m.group(1).strip()]


async def auto_run_blocks(blocks: list[str], sparkle: Sparkle, pt_session,
                          messages: list[dict], provider, model: str,
                          system_prompt: str):
    """Offer to execute detected Python code blocks and feed results back."""
    for i, code in enumerate(blocks):
        label = f"Code block {i + 1}/{len(blocks)}" if len(blocks) > 1 else "Code block"

        console.print(Panel(
            Text(code, style="bright_white"),
            title=f"[bold white] {label} [/]",
            border_style="bright_cyan",
            padding=(0, 1),
        ))

        try:
            answer = await pt_session.prompt_async(
                [("fg:#c084fc bold", f"  Run this code? "),
                 ("fg:#888888", "(y/n) "), ("", "> ")])
        except (KeyboardInterrupt, EOFError):
            console.print("  [dim]Skipped.[/dim]\n")
            continue

        if answer.strip().lower() not in ("y", "yes", "si", "s"):
            console.print("  [dim]Skipped.[/dim]\n")
            continue

        # Validate before executing
        error = validate_code(code)
        if error:
            console.print(f"  [error]{error}[/]\n")
            continue

        sparkle.set_state("thinking")
        console.print("  [dim]Executing in sandbox...[/dim]")
        result = await execute_code(code)

        if result.error:
            console.print(f"  [error]Sandbox: {result.error}[/]\n")
            continue

        sparkle.set_state("responding")

        # Display results
        if result.stdout.strip():
            console.print(Panel(
                Text(result.stdout.rstrip(), style="bright_green"),
                title="[bold white] stdout [/]",
                border_style="bright_green",
                padding=(0, 1),
            ))
        if result.stderr.strip():
            console.print(Panel(
                Text(result.stderr.rstrip(), style="red"),
                title="[bold white] stderr [/]",
                border_style="red",
                padding=(0, 1),
            ))

        if result.timed_out:
            console.print("  [warning]Timed out[/]")
        elif result.exit_code != 0:
            console.print(f"  [error]Exit code: {result.exit_code}[/]")
        else:
            console.print(f"  [success]Exit code: 0[/]")

        # Feed result back to the LLM
        output_parts = []
        if result.stdout.strip():
            output_parts.append(f"stdout:\n{result.stdout.strip()}")
        if result.stderr.strip():
            output_parts.append(f"stderr:\n{result.stderr.strip()}")
        if result.timed_out:
            output_parts.append("(execution timed out)")

        if output_parts:
            exec_feedback = (
                f"I executed the Python code you provided. Here are the results:\n\n"
                + "\n\n".join(output_parts)
            )
            messages.append({"role": "user", "content": exec_feedback})

            console.print("\n  [dim]Feeding results back to the assistant...[/dim]\n")
            # Stream the LLM's interpretation of the execution result
            accumulated = ""
            sparkle.set_state("thinking")
            with Live(console=console, refresh_per_second=12, vertical_overflow="visible") as live:
                async for token in provider.chat_stream(
                    messages=messages, model=model, system_prompt=system_prompt,
                ):
                    accumulated += token
                    sparkle.tick()
                    char, style = sparkle.rich_str()
                    header = Text.from_markup(
                        f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                    md = Markdown(accumulated, code_theme="monokai")
                    live.update(Group(header, md))

            if accumulated:
                messages.append({"role": "assistant", "content": accumulated})
            console.print()

        sparkle.set_state("idle")


# ═══ Export ══════════════════════════════════════════════════════════════════

def export_markdown(messages: list[dict], model: str) -> tuple[bool, str]:
    """Export conversation as Markdown. Returns (success, filepath_or_error)."""
    if not messages:
        return False, "No messages to export."

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"hivemind_export_{ts}.md"
    path = Path.cwd() / filename

    lines = [
        f"# Hivemind Conversation Export",
        f"",
        f"**Model:** {model}  ",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"**Messages:** {len(messages)}",
        f"",
        f"---",
        f"",
    ]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            lines.append(f"## User\n\n{content}\n")
        elif role == "assistant":
            lines.append(f"## Assistant\n\n{content}\n")
        elif role == "system":
            lines.append(f"## System\n\n{content}\n")
        lines.append("---\n")

    path.write_text("\n".join(lines), encoding="utf-8")
    return True, str(path)


def export_json(messages: list[dict], model: str) -> tuple[bool, str]:
    """Export conversation as JSON. Returns (success, filepath_or_error)."""
    if not messages:
        return False, "No messages to export."

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"hivemind_export_{ts}.json"
    path = Path.cwd() / filename

    data = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "model": model,
        "exported_at": datetime.now().isoformat(),
        "message_count": len(messages),
        "messages": messages,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True, str(path)


# ═══ Pipe / One-Shot Mode ════════════════════════════════════════════════════

async def run_oneshot(input_text: str, provider, model: str, system_prompt: str):
    """One-shot mode: process piped stdin and print response to stdout.

    Supports two forms:
      echo "question" | hivemind
      cat file.py | hivemind "review this code"
    """
    # Check if there's also a CLI argument (e.g. hivemind "prompt" with piped data)
    cli_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if cli_args:
        # Combine: piped content as context + CLI arg as instruction
        prompt = f"<input>\n{input_text}\n</input>\n\n{' '.join(cli_args)}"
    else:
        prompt = input_text

    messages = [{"role": "user", "content": prompt}]
    try:
        async for token in provider.chat_stream(
            messages=messages, model=model, system_prompt=system_prompt,
        ):
            sys.stdout.write(token)
            sys.stdout.flush()
        sys.stdout.write("\n")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ═══ Context Window Management ═══════════════════════════════════════════════

COMPACT_SYSTEM_PROMPT = (
    "Summarize the following conversation in 2-3 concise paragraphs. "
    "Preserve key facts, decisions, code snippets mentioned, and any "
    "unresolved questions. This summary will replace the older messages "
    "to keep the conversation context manageable."
)


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: ~4 chars per token."""
    return sum(len(m.get("content", "")) for m in messages) // 4


async def compact_messages(
    messages: list[dict],
    provider,
    model: str,
    keep_recent: int = COMPACT_KEEP_RECENT,
    force: bool = False,
) -> tuple[bool, str]:
    """Compact conversation history by summarizing older messages.

    Returns (did_compact, status_message).
    """
    if len(messages) <= keep_recent + 2:
        return False, "History too short to compact."

    old_messages = messages[:-keep_recent]
    recent_messages = messages[-keep_recent:]

    # Build conversation text for summarization
    convo_lines = []
    for msg in old_messages:
        role = msg["role"].capitalize()
        content = msg["content"][:2000]  # limit per message for summarization
        convo_lines.append(f"{role}: {content}")
    convo_text = "\n\n".join(convo_lines)

    if not force and _estimate_tokens(old_messages) < 500:
        return False, "Not enough context to warrant compaction."

    # Ask LLM to summarize
    summary_messages = [{"role": "user", "content": convo_text}]
    summary = ""
    try:
        async for token in provider.chat_stream(
            messages=summary_messages, model=model,
            system_prompt=COMPACT_SYSTEM_PROMPT,
        ):
            summary += token
    except Exception:
        # Fallback: simple truncation without summary
        messages[:] = recent_messages
        return True, f"Compacted (no summary): kept last {keep_recent} messages."

    # Replace history with summary + recent messages
    summary_msg = {"role": "system", "content": f"[Conversation summary]\n{summary}"}
    messages.clear()
    messages.append(summary_msg)
    messages.extend(recent_messages)

    old_count = len(old_messages)
    return True, f"Compacted: {old_count} old messages summarized, kept {keep_recent} recent."


def auto_should_compact(messages: list[dict]) -> bool:
    """Check if conversation is getting long enough to auto-suggest compaction."""
    return len(messages) >= MAX_CONTEXT_MESSAGES


# ═══ Shell Integration ══════════════════════════════════════════════════════

def run_shell(command: str) -> tuple[str, int]:
    """Run a shell command and return (output, exit_code).

    Captures both stdout and stderr, truncates output if too long.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
            cwd=Path.cwd(),
        )
        output = result.stdout + result.stderr
        if len(output) > MAX_SHELL_OUTPUT:
            output = output[:MAX_SHELL_OUTPUT] + f"\n... (truncated, {len(output):,} chars total)"
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return f"Command timed out after {SHELL_TIMEOUT}s", -1
    except Exception as e:
        return f"Error: {e}", -1


def display_shell_output(output: str, exit_code: int):
    """Display shell command output with styling."""
    if output.strip():
        border = "bright_green" if exit_code == 0 else "red"
        console.print(Panel(
            Text(output.rstrip(), style="white"),
            title=f"[bold white] Shell (exit {exit_code}) [/]",
            border_style=border,
            padding=(0, 1),
        ))
    elif exit_code == 0:
        console.print("  [dim]No output.[/dim]")
    else:
        console.print(f"  [error]Exit code: {exit_code}[/]")
    console.print()


# ═══ Prompt Library ═════════════════════════════════════════════════════════

def _ensure_prompts_dir():
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def _prompt_path(name: str) -> Path:
    safe = name.strip().replace(" ", "_").lower()
    return PROMPTS_DIR / f"{safe}.txt"


def save_prompt(name: str, content: str) -> tuple[bool, str]:
    """Save a system prompt to the library."""
    if not name.strip():
        return False, "Prompt name cannot be empty."
    if not content.strip():
        return False, "Prompt content cannot be empty."
    _ensure_prompts_dir()
    path = _prompt_path(name)
    path.write_text(content, encoding="utf-8")
    return True, f"Saved prompt: '{name}' ({len(content)} chars)"


def load_prompt(name: str) -> tuple[str | None, str]:
    """Load a prompt from the library. Returns (content, error)."""
    _ensure_prompts_dir()
    path = _prompt_path(name)
    if not path.exists():
        return None, f"Prompt '{name}' not found."
    content = path.read_text(encoding="utf-8")
    return content, ""


def list_prompts() -> list[tuple[str, int]]:
    """List saved prompts. Returns list of (name, char_count)."""
    _ensure_prompts_dir()
    prompts = []
    for path in sorted(PROMPTS_DIR.glob("*.txt")):
        content = path.read_text(encoding="utf-8")
        name = path.stem.replace("_", " ")
        prompts.append((name, len(content)))
    return prompts


def delete_prompt(name: str) -> tuple[bool, str]:
    """Delete a saved prompt."""
    _ensure_prompts_dir()
    path = _prompt_path(name)
    if not path.exists():
        return False, f"Prompt '{name}' not found."
    path.unlink()
    return True, f"Deleted prompt: '{name}'"


def handle_prompt_command(arg: str, system_prompt: str) -> str:
    """Handle /prompt subcommands. Returns updated system_prompt."""
    parts = arg.strip().split(maxsplit=1) if arg.strip() else []
    if not parts:
        console.print("\n  [dim]Usage:[/dim]")
        console.print("    [bold cyan]/prompt list[/]          List saved prompts")
        console.print("    [bold cyan]/prompt save <name>[/]   Save current system prompt")
        console.print("    [bold cyan]/prompt load <name>[/]   Load and activate a prompt")
        console.print("    [bold cyan]/prompt delete <name>[/] Delete a saved prompt")
        console.print("    [bold cyan]/prompt show <name>[/]   Show a prompt's content")
        console.print(f"  [dim]Prompts stored in: {PROMPTS_DIR}[/dim]\n")
        return system_prompt

    subcmd = parts[0].lower()
    subarg = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "list":
        prompts = list_prompts()
        if not prompts:
            console.print("\n  [dim]No saved prompts. Use /prompt save <name> to create one.[/dim]\n")
        else:
            console.print()
            for name, chars in prompts:
                console.print(f"  [bold bright_cyan]\u25cf[/] {name} [dim]({chars:,} chars)[/dim]")
            console.print()

    elif subcmd == "save":
        if not subarg:
            console.print("  [error]Usage: /prompt save <name>[/]\n")
        else:
            ok, msg = save_prompt(subarg, system_prompt)
            style = "success" if ok else "error"
            console.print(f"\n  [{style}]{msg}[/]\n")

    elif subcmd == "load":
        if not subarg:
            console.print("  [error]Usage: /prompt load <name>[/]\n")
        else:
            content, error = load_prompt(subarg)
            if error:
                console.print(f"\n  [error]{error}[/]\n")
            else:
                system_prompt = content
                preview = content[:100] + ("..." if len(content) > 100 else "")
                console.print(f"\n  [success]Loaded prompt: '{subarg}'[/]")
                console.print(f"  [dim]{preview}[/dim]\n")

    elif subcmd == "delete":
        if not subarg:
            console.print("  [error]Usage: /prompt delete <name>[/]\n")
        else:
            ok, msg = delete_prompt(subarg)
            style = "success" if ok else "error"
            console.print(f"\n  [{style}]{msg}[/]\n")

    elif subcmd == "show":
        if not subarg:
            console.print("  [error]Usage: /prompt show <name>[/]\n")
        else:
            content, error = load_prompt(subarg)
            if error:
                console.print(f"\n  [error]{error}[/]\n")
            else:
                console.print(Panel(
                    Text(content, style="white"),
                    title=f"[bold white] {subarg} [/]",
                    border_style="bright_cyan",
                    padding=(0, 1),
                ))
                console.print()

    else:
        console.print(f"  [error]Unknown subcommand: {subcmd}[/]")
        console.print("  [dim]Try: /prompt list | save | load | delete | show[/dim]\n")

    return system_prompt


# ═══ Conversation Fork ══════════════════════════════════════════════════════

def handle_fork_command(arg: str, messages: list[dict], model: str) -> tuple[bool, str]:
    """Fork current conversation into a new saved session.

    Returns (success, message).
    """
    name = arg.strip()
    if not name:
        # Auto-generate name
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"fork_{ts}"

    if not messages:
        return False, "No messages to fork."

    # Save as a new session
    ok, msg = save_session(name, model, list(messages))
    if ok:
        return True, f"Forked to session: '{name}' ({len(messages)} messages). Use /load {name} to switch."
    return False, msg


# ═══ Image Input ════════════════════════════════════════════════════════════

def validate_image(raw_path: str) -> tuple[Path | None, str]:
    """Validate an image file. Returns (resolved_path, error)."""
    if not raw_path.strip():
        return None, "No path provided."
    target = Path(raw_path.strip()).expanduser()
    try:
        if not target.is_absolute():
            target = Path.cwd() / target
        resolved = target.resolve(strict=True)
    except (OSError, ValueError) as e:
        return None, f"Cannot resolve path: {e}"

    if not resolved.is_file():
        return None, f"Not a file: {resolved}"

    suffix = resolved.suffix.lower()
    if suffix not in SUPPORTED_IMAGE_TYPES:
        return None, f"Unsupported image type: {suffix}. Supported: {', '.join(sorted(SUPPORTED_IMAGE_TYPES))}"

    size = resolved.stat().st_size
    if size > MAX_IMAGE_SIZE:
        return None, f"Image too large: {size / 1024 / 1024:.1f}MB (max {MAX_IMAGE_SIZE / 1024 / 1024:.0f}MB)"
    if size == 0:
        return None, "Image file is empty."

    return resolved, ""


def encode_image(path: Path) -> tuple[str, str]:
    """Read and base64-encode an image. Returns (data_uri, mime_type)."""
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "image/png"
    data = path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}", mime


def build_image_message(text: str, image_data_uri: str) -> dict:
    """Build a multimodal user message with text + image.

    Uses OpenAI-compatible format (image_url with data URI).
    Providers convert this to their native format.
    """
    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({
        "type": "image_url",
        "image_url": {"url": image_data_uri},
    })
    return {"role": "user", "content": content}


class ImageContext:
    """Manages a pending image to attach to the next message."""

    def __init__(self):
        self.pending_uri: str | None = None
        self.pending_name: str = ""

    def set(self, data_uri: str, name: str):
        self.pending_uri = data_uri
        self.pending_name = name

    def take(self) -> str | None:
        """Consume the pending image URI (returns it and clears)."""
        uri = self.pending_uri
        self.pending_uri = None
        self.pending_name = ""
        return uri

    @property
    def has_pending(self) -> bool:
        return self.pending_uri is not None


# ═══ Token Counter + Cost ═══════════════════════════════════════════════════

def _estimate_token_count(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return max(1, len(text) // 4)


def _count_message_tokens(msg: dict) -> int:
    """Estimate tokens in a single message."""
    content = msg.get("content", "")
    if isinstance(content, list):
        # Multimodal: count text parts, estimate ~85 tokens per image
        tokens = 0
        for block in content:
            if block.get("type") == "text":
                tokens += _estimate_token_count(block["text"])
            elif block.get("type") == "image_url":
                tokens += 85  # Approximate image token overhead
        return tokens
    return _estimate_token_count(content)


class TokenTracker:
    """Tracks token usage and cost across a session."""

    def __init__(self):
        self.total_input = 0
        self.total_output = 0
        self.message_log: list[dict] = []  # [{role, tokens, provider}]

    def record_input(self, messages: list[dict], provider_name: str):
        """Record input tokens from a list of messages."""
        tokens = sum(_count_message_tokens(m) for m in messages)
        self.total_input += tokens
        self.message_log.append({
            "role": "input", "tokens": tokens, "provider": provider_name,
        })

    def record_output(self, text: str, provider_name: str):
        """Record output tokens from a response."""
        tokens = _estimate_token_count(text)
        self.total_output += tokens
        self.message_log.append({
            "role": "output", "tokens": tokens, "provider": provider_name,
        })

    def cost(self, provider_name: str) -> float:
        """Estimate total cost in USD for a given provider."""
        pricing = TOKEN_PRICING.get(provider_name, {"input": 0.0, "output": 0.0})
        input_cost = (self.total_input / 1_000_000) * pricing["input"]
        output_cost = (self.total_output / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    def summary(self, provider_name: str) -> str:
        """Generate a cost summary string."""
        total = self.total_input + self.total_output
        cost = self.cost(provider_name)
        lines = [
            f"  [dim]Input tokens:[/dim]  ~{self.total_input:,}",
            f"  [dim]Output tokens:[/dim] ~{self.total_output:,}",
            f"  [dim]Total tokens:[/dim]  ~{total:,}",
        ]
        if cost > 0:
            lines.append(f"  [dim]Est. cost:[/dim]     [bold bright_cyan]${cost:.4f}[/]")
        else:
            lines.append(f"  [dim]Est. cost:[/dim]     [bold bright_green]free (local)[/]")
        lines.append(f"  [dim]Exchanges:[/dim]     {len(self.message_log) // 2}")
        return "\n".join(lines)


# ═══ Diff ═══════════════════════════════════════════════════════════════════

class ResponseHistory:
    """Tracks recent assistant responses for diff comparison."""

    def __init__(self):
        self._responses: list[str] = []

    def push(self, text: str):
        if text.strip():
            self._responses.append(text)

    @property
    def last(self) -> str | None:
        return self._responses[-1] if self._responses else None

    @property
    def prev(self) -> str | None:
        return self._responses[-2] if len(self._responses) >= 2 else None

    def can_diff(self) -> bool:
        return len(self._responses) >= 2


def generate_diff(old: str, new: str, old_label: str = "Previous",
                  new_label: str = "Current") -> str:
    """Generate a unified diff between two texts."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines,
                                 fromfile=old_label, tofile=new_label, lineterm="")
    return "".join(diff)


def display_diff(old: str, new: str, old_label: str = "Previous",
                 new_label: str = "Current"):
    """Display a colorized diff in the terminal."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=old_label, tofile=new_label, lineterm="",
    ))

    if not diff_lines:
        console.print("\n  [dim]No differences found.[/dim]\n")
        return

    output = Text()
    for line in diff_lines:
        if line.startswith("+++"):
            output.append(line + "\n", style="bold bright_green")
        elif line.startswith("---"):
            output.append(line + "\n", style="bold red")
        elif line.startswith("@@"):
            output.append(line + "\n", style="bold bright_cyan")
        elif line.startswith("+"):
            output.append(line + "\n", style="bright_green")
        elif line.startswith("-"):
            output.append(line + "\n", style="red")
        else:
            output.append(line + "\n", style="dim")

    console.print(Panel(
        output,
        title="[bold white] Diff [/]",
        border_style="bright_cyan",
        padding=(0, 1),
    ))
    console.print()


# ═══ Aliases ════════════════════════════════════════════════════════════════

class AliasManager:
    """Persistent command aliases stored in ~/.hivemind/aliases.json."""

    def __init__(self):
        self._aliases: dict[str, str] = {}
        self._load()

    def _load(self):
        if ALIASES_FILE.exists():
            try:
                self._aliases = json.loads(ALIASES_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                self._aliases = {}

    def _save(self):
        ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALIASES_FILE.write_text(json.dumps(self._aliases, indent=2))

    def set(self, name: str, value: str) -> str:
        name = name.strip().lower()
        if not name:
            return "Alias name cannot be empty."
        if name.startswith("/") or name.startswith("!"):
            return "Alias name should not start with / or !"
        self._aliases[name] = value
        self._save()
        return f"Alias set: {name} = {value}"

    def delete(self, name: str) -> tuple[bool, str]:
        name = name.strip().lower()
        if name not in self._aliases:
            return False, f"Alias '{name}' not found."
        del self._aliases[name]
        self._save()
        return True, f"Deleted alias: '{name}'"

    def get(self, name: str) -> str | None:
        return self._aliases.get(name.strip().lower())

    def list_all(self) -> dict[str, str]:
        return dict(self._aliases)

    def expand(self, user_input: str) -> str:
        """Try to expand the first word as an alias."""
        parts = user_input.split(maxsplit=1)
        if not parts:
            return user_input
        first = parts[0].lower()
        expansion = self._aliases.get(first)
        if expansion is None:
            return user_input
        rest = parts[1] if len(parts) > 1 else ""
        return f"{expansion} {rest}".strip() if rest else expansion


def handle_alias_command(arg: str, alias_mgr: AliasManager):
    """Handle /alias subcommands."""
    arg = arg.strip()
    if not arg or arg.lower() == "list":
        aliases = alias_mgr.list_all()
        if not aliases:
            console.print("\n  [dim]No aliases defined.[/dim]")
            console.print("  [dim]Usage: /alias <name>=<command>[/dim]\n")
        else:
            console.print()
            for name, value in sorted(aliases.items()):
                console.print(f"  [bold bright_cyan]{name}[/] = [dim]{value}[/dim]")
            console.print()
        return

    if arg.lower().startswith("delete "):
        name = arg[7:].strip()
        ok, msg = alias_mgr.delete(name)
        style = "success" if ok else "error"
        console.print(f"\n  [{style}]{msg}[/]\n")
        return

    # Parse name=value
    if "=" not in arg:
        console.print("\n  [dim]Usage:[/dim]")
        console.print("    [bold cyan]/alias <name>=<command>[/]  Create alias")
        console.print("    [bold cyan]/alias list[/]              List aliases")
        console.print("    [bold cyan]/alias delete <name>[/]     Delete alias")
        console.print("  [dim]Examples: /alias gs=!git status   /alias r=/run[/dim]\n")
        return

    name, value = arg.split("=", 1)
    msg = alias_mgr.set(name.strip(), value.strip())
    console.print(f"\n  [success]{msg}[/]\n")


# ═══ Web Fetch ══════════════════════════════════════════════════════════════

class _HTMLTextExtractor(html.parser.HTMLParser):
    """Simple HTML-to-text converter that strips tags."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag.lower() in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
                            "li", "tr", "blockquote"):
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Collapse excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()


def extract_text_from_html(html_content: str) -> str:
    """Extract readable text from HTML content."""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_content)
        return parser.get_text()
    except Exception:
        # Fallback: crude tag stripping
        text = re.sub(r"<[^>]+>", " ", html_content)
        return re.sub(r"\s+", " ", text).strip()


async def fetch_web_content(url: str) -> tuple[str | None, str]:
    """Fetch a URL and extract text. Returns (text, error)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(
            timeout=_httpx.Timeout(WEB_TIMEOUT),
            follow_redirects=True,
            headers={"User-Agent": "Hivemind/9.0 (text fetcher)"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                text = extract_text_from_html(resp.text)
            elif "text/" in content_type or "json" in content_type:
                text = resp.text
            else:
                return None, f"Unsupported content type: {content_type}"

            if len(text) > MAX_WEB_TEXT:
                text = text[:MAX_WEB_TEXT] + f"\n\n... (truncated, {len(text):,} chars total)"
            return text, ""

    except Exception as e:
        return None, f"Fetch error: {e}"


# ═══ Slash Commands ══════════════════════════════════════════════════════════

SLASH_COMMANDS = {
    "/help": "Show available commands",
    "/clear": "Clear screen and conversation",
    "/cd": "Change directory  (/cd <path>)",
    "/model": "Switch model  (/model <name> | list)",
    "/models": "List available Ollama models",
    "/provider": "Switch provider  (/provider <ollama|anthropic|openai>)",
    "/system": "Set system prompt  (/system <prompt> | clear)",
    "/prompt": "Prompt library  (/prompt save|load|list|delete|show)",
    "/alias": "Manage aliases  (/alias <name>=<cmd> | list | delete)",
    "/history": "Show conversation history",
    "/undo": "Undo last message pair",
    "/redo": "Redo last undone pair",
    "/diff": "Compare last two responses",
    "/fork": "Fork conversation  (/fork [name])",
    "/multi": "Toggle multiline mode (Enter = newline)",
    "/compact": "Smart compact  (/compact | /compact hard)",
    "/file": "Attach file  (/file <path> | clear | list)",
    "/image": "Attach image  (/image <path> [prompt])",
    "/web": "Fetch URL  (/web <url> [prompt])",
    "/run": "Execute Python code  (/run <code>)",
    "/cost": "Show token usage and estimated cost",
    "/export": "Export conversation  (/export md | json)",
    "/save": "Save conversation  (/save <name>)",
    "/load": "Load conversation  (/load <name>)",
    "/sessions": "List or delete sessions",
    "/agent": "Manage agents  (create/list/delete/info/template)",
    "/swarm": "Run a task using the agent swarm",
    "/providers": "Show provider configuration",
    "/exit": "Exit the CLI",
}

SHELL_HELP = (
    "  [dim]Shell integration:[/dim]\n"
    "    [bold cyan]!<command>[/]   Run shell command (e.g. !ls, !git status)\n"
    "    [bold cyan]!!<command>[/]  Run and send output to the LLM\n"
)


def show_help():
    lines = Text()
    for cmd, desc in SLASH_COMMANDS.items():
        lines.append(f"  {cmd:<14}", style="bold cyan")
        lines.append(f" {desc}\n", style="white")
    lines.append("\n")
    lines.append("  !<cmd>       ", style="bold cyan")
    lines.append("Run shell command\n", style="white")
    lines.append("  !!<cmd>      ", style="bold cyan")
    lines.append("Run shell and send output to LLM\n", style="white")
    console.print()
    console.print(Panel(lines, title="[bold white] Commands [/]",
                        title_align="left", border_style="bright_cyan", padding=(1, 2)))
    console.print()


def show_history(messages):
    if not messages:
        console.print("  [dim]No conversation history yet.[/dim]\n")
        return
    console.print()
    for i, msg in enumerate(messages):
        if msg["role"] == "user":
            console.print(f"  [bold bright_cyan]You [dim]#{i+1}[/dim][/]")
            console.print(f"  {msg['content']}\n")
        else:
            preview = msg["content"][:150].replace("\n", " ")
            if len(msg["content"]) > 150:
                preview += "..."
            console.print(f"  [bold bright_green]Assistant [dim]#{i+1}[/dim][/]")
            console.print(f"  [dim]{preview}[/dim]\n")
    console.print()


def handle_file_command(arg: str, file_ctx: FileContextManager):
    """Handle /file subcommands."""
    arg = arg.strip()
    if not arg:
        console.print("\n  [dim]Usage:[/dim]")
        console.print("    [bold cyan]/file <path>[/]   Attach a file to context")
        console.print("    [bold cyan]/file list[/]     Show attached files")
        console.print("    [bold cyan]/file remove <path>[/]  Remove a file")
        console.print("    [bold cyan]/file clear[/]    Remove all files")
        console.print(f"  [dim]Max {MAX_FILES} files, {MAX_FILE_SIZE // 1024}KB each. "
                      f"Sensitive files are blocked.[/dim]\n")
        return

    if arg.lower() == "clear":
        msg = file_ctx.clear()
        console.print(f"\n  [success]{msg}[/]\n")
    elif arg.lower() == "list":
        files = file_ctx.list_files()
        if not files:
            console.print("\n  [dim]No files attached.[/dim]\n")
        else:
            console.print()
            for name, chars in files:
                console.print(f"  [bold bright_cyan]\u25cf[/] {name} [dim]({chars:,} chars)[/dim]")
            console.print()
    elif arg.lower().startswith("remove "):
        path = arg[7:].strip()
        ok, msg = file_ctx.remove(path)
        style = "success" if ok else "error"
        console.print(f"\n  [{style}]{msg}[/]\n")
    else:
        ok, msg = file_ctx.add(arg)
        style = "success" if ok else "error"
        console.print(f"\n  [{style}]{msg}[/]\n")


async def handle_run_command(code: str, sparkle: Sparkle):
    """Execute Python code in the sandbox and display results."""
    if not code.strip():
        console.print("\n  [dim]Usage: /run <code>[/dim]")
        console.print("  [dim]Multiline: paste code with Opt+Enter for newlines[/dim]")
        console.print("  [dim]Sandbox: 10s timeout, no network, no file writes[/dim]\n")
        return

    # Quick validation before execution
    error = validate_code(code)
    if error:
        console.print(f"\n  [error]{error}[/]\n")
        return

    sparkle.set_state("thinking")
    console.print("\n  [dim]Executing in sandbox...[/dim]")

    result = await execute_code(code)

    if result.error:
        sparkle.set_state("error")
        console.print(f"  [error]Sandbox error: {result.error}[/]\n")
        sparkle.set_state("idle")
        return

    sparkle.set_state("responding")

    # Show stdout
    if result.stdout.strip():
        console.print(Panel(
            Text(result.stdout.rstrip(), style="bright_green"),
            title="[bold white] stdout [/]",
            border_style="bright_green",
            padding=(0, 1),
        ))

    # Show stderr
    if result.stderr.strip():
        console.print(Panel(
            Text(result.stderr.rstrip(), style="red"),
            title="[bold white] stderr [/]",
            border_style="red",
            padding=(0, 1),
        ))

    # Exit status
    if result.timed_out:
        console.print(f"  [warning]Timed out[/]")
    elif result.exit_code != 0:
        console.print(f"  [error]Exit code: {result.exit_code}[/]")
    else:
        if not result.stdout.strip() and not result.stderr.strip():
            console.print("  [dim]No output.[/dim]")
        else:
            console.print(f"  [success]Exit code: 0[/]")

    console.print()
    sparkle.set_state("idle")


def handle_session_save(arg: str, messages: list[dict], model: str):
    """Handle /save <name>."""
    name = arg.strip()
    if not name:
        console.print("\n  [dim]Usage: /save <name>[/dim]\n")
        return
    ok, msg = save_session(name, model, messages)
    style = "success" if ok else "error"
    console.print(f"\n  [{style}]{msg}[/]\n")


def handle_session_load(arg: str, messages: list[dict]) -> str | None:
    """Handle /load <name>. Returns model if loaded, None on failure."""
    name = arg.strip()
    if not name:
        console.print("\n  [dim]Usage: /load <name>[/dim]\n")
        return None
    session, error = load_session(name)
    if error:
        console.print(f"\n  [error]{error}[/]\n")
        return None
    messages.clear()
    messages.extend(session.messages)
    console.print(f"\n  [success]Loaded: '{session.name}'[/] "
                  f"[dim]({len(session.messages)} messages, model: {session.model})[/dim]\n")
    return session.model


def handle_sessions_command(arg: str):
    """Handle /sessions [delete <name>]."""
    parts = arg.strip().split(maxsplit=1)
    if parts and parts[0].lower() == "delete":
        name = parts[1].strip() if len(parts) > 1 else ""
        if not name:
            console.print("  [error]Usage: /sessions delete <name>[/]\n")
            return
        ok, msg = delete_session(name)
        style = "success" if ok else "error"
        console.print(f"\n  [{style}]{msg}[/]\n")
        return

    sessions = list_sessions()
    if not sessions:
        console.print("\n  [dim]No saved sessions.[/dim]")
        console.print("  [dim]Use /save <name> to save the current conversation.[/dim]\n")
        return

    console.print()
    for s in sessions:
        ts = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
        console.print(f"  [bold bright_cyan]\u25cf[/] {s.name:<20} "
                      f"[dim]{s.message_count} msgs  {s.model}  {ts}[/dim]")
    console.print(f"\n  [dim]/load <name> to restore  |  /sessions delete <name> to remove[/dim]\n")


def handle_system_command(arg: str, system_prompt: str) -> str:
    """Handle /system. Returns updated system prompt."""
    arg = arg.strip()
    if not arg:
        preview = system_prompt[:120] + ("..." if len(system_prompt) > 120 else "")
        console.print(f"\n  [dim]Current system prompt:[/dim]")
        console.print(f"  [bright_cyan]{preview}[/]\n")
        console.print("  [dim]Usage: /system <prompt>  |  /system clear[/dim]\n")
        return system_prompt

    if arg.lower() == "clear":
        console.print(f"\n  [success]System prompt reset to default.[/]\n")
        return DEFAULT_SYSTEM_PROMPT

    console.print(f"\n  [success]System prompt updated.[/] [dim]({len(arg)} chars)[/dim]\n")
    return arg


def handle_export_command(arg: str, messages: list[dict], model: str):
    """Handle /export md|json."""
    fmt = arg.strip().lower()
    if fmt not in ("md", "json"):
        console.print("\n  [dim]Usage:[/dim]")
        console.print("    [bold cyan]/export md[/]    Export as Markdown")
        console.print("    [bold cyan]/export json[/]  Export as JSON\n")
        return

    if fmt == "md":
        ok, result = export_markdown(messages, model)
    else:
        ok, result = export_json(messages, model)

    if ok:
        console.print(f"\n  [success]Exported:[/] [dim]{result}[/dim]\n")
    else:
        console.print(f"\n  [error]{result}[/]\n")


async def handle_command(cmd, provider, provider_name, provider_config, messages, model,
                         sparkle, registry, session, file_ctx, system_prompt, undo_mgr,
                         image_ctx, token_tracker, response_history, alias_mgr=None,
                         multiline_state=None):
    """Returns (should_exit, model, system_prompt, provider, provider_name)."""
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit"):
        return True, model, system_prompt, provider, provider_name

    if command == "/help":
        show_help()
    elif command == "/clear":
        os.system("clear" if os.name != "nt" else "cls")
        messages.clear()
        file_ctx.clear()
        print_banner(model, sparkle, provider_name=provider_name, animate=False)
        console.print("  [success]Conversation and file context cleared.[/success]\n")
    elif command == "/cd":
        target = arg.strip()
        if not target:
            console.print(f"\n  [dim]Current directory:[/dim] [bold white]{Path.cwd()}[/]")
            console.print(f"  [dim]Usage: /cd <path>[/dim]\n")
        else:
            target_path = Path(target).expanduser().resolve()
            if not target_path.exists():
                console.print(f"\n  [error]Directory not found:[/] {target}\n")
            elif not target_path.is_dir():
                console.print(f"\n  [error]Not a directory:[/] {target}\n")
            else:
                os.chdir(target_path)
                console.print(f"\n  [success]Changed to:[/] {_short_path(target_path)}\n")
    elif command == "/model":
        if not arg or arg.strip().lower() == "list":
            console.print(f"\n  [dim]Current model:[/dim] [bold bright_cyan]{model}[/]")
            console.print(f"  [dim]Messages in context:[/dim] {len(messages)}")
            console.print(f"\n  [dim]Fetching available models...[/dim]")
            models = await provider.list_models()
            if models:
                console.print()
                for m in models:
                    if m == model:
                        console.print(f"  [bold bright_green]\u25cf[/] {m} [dim](active)[/dim]")
                    else:
                        console.print(f"  [dim]\u25cb[/dim] {m}")
            console.print(f"\n  [dim]Usage: /model <name>  (history is preserved)[/dim]\n")
        else:
            old_model = model
            model = arg.strip()
            console.print(f"\n  [success]Switched:[/] {old_model} [dim]\u2192[/dim] [bold bright_cyan]{model}[/]")
            console.print(f"  [dim]History preserved ({len(messages)} messages).[/dim]\n")
    elif command == "/models":
        console.print("\n  [dim]Fetching...[/dim]")
        models = await provider.list_models()
        if models:
            console.print()
            for m in models:
                if m == model:
                    console.print(f"  [bold bright_green]\u25cf[/] {m} [dim](active)[/dim]")
                else:
                    console.print(f"  [dim]\u25cb[/dim] {m}")
            console.print()
        else:
            console.print("  [error]Could not fetch models.[/error]\n")
    elif command == "/system":
        system_prompt = handle_system_command(arg, system_prompt)
    elif command == "/prompt":
        system_prompt = handle_prompt_command(arg, system_prompt)
    elif command == "/history":
        show_history(messages)
    elif command == "/undo":
        ok, msg = undo_mgr.undo(messages)
        style = "success" if ok else "dim"
        console.print(f"\n  [{style}]{msg}[/]\n")
    elif command == "/redo":
        ok, msg = undo_mgr.redo(messages)
        style = "success" if ok else "dim"
        console.print(f"\n  [{style}]{msg}[/]\n")
    elif command == "/diff":
        if not response_history.can_diff():
            console.print("\n  [dim]Need at least 2 responses to diff. Try /undo then re-ask.[/dim]\n")
        else:
            display_diff(response_history.prev, response_history.last)
    elif command == "/provider":
        pname = arg.strip().lower()
        if not pname:
            console.print(f"\n  [dim]Current provider:[/dim] [bold bright_cyan]{provider_name}[/]")
            console.print(f"  [dim]Current model:[/dim] [bold bright_cyan]{model}[/]")
            console.print(f"\n  [dim]Available: ollama, anthropic, openai[/dim]")
            console.print(f"  [dim]Usage: /provider <name>[/dim]\n")
        elif pname not in ("ollama", "anthropic", "openai"):
            console.print(f"\n  [error]Unknown provider: '{pname}'[/]")
            console.print(f"  [dim]Available: ollama, anthropic, openai[/dim]\n")
        elif pname == provider_name:
            console.print(f"\n  [dim]Already using {pname}.[/dim]\n")
        else:
            try:
                new_provider = get_provider(pname, provider_config)
                old_name = provider_name
                provider = new_provider
                provider_name = pname
                console.print(f"\n  [success]Switched provider:[/] {old_name} [dim]\u2192[/dim] [bold bright_cyan]{pname}[/]")
                console.print(f"  [dim]History preserved ({len(messages)} messages). "
                              f"You may want to /model to pick a model for this provider.[/dim]\n")
            except ValueError as e:
                console.print(f"\n  [error]{e}[/]\n")
    elif command == "/fork":
        ok, msg = handle_fork_command(arg, messages, model)
        style = "success" if ok else "error"
        console.print(f"\n  [{style}]{msg}[/]\n")
    elif command == "/compact":
        count = len(messages)
        tokens_est = _estimate_tokens(messages)
        console.print(f"\n  [dim]Messages: {count}  |  ~{tokens_est:,} tokens estimated[/dim]")
        if arg.strip().lower() == "hard":
            # Hard compact: just keep recent, no summarization
            if count <= COMPACT_KEEP_RECENT:
                console.print("  [dim]History too short to compact.[/dim]\n")
            else:
                messages[:] = messages[-COMPACT_KEEP_RECENT:]
                console.print(f"  [success]Hard compacted:[/] {count} \u2192 {len(messages)} messages\n")
        else:
            sparkle.set_state("thinking")
            console.print("  [dim]Summarizing older messages...[/dim]")
            did_compact, msg = await compact_messages(messages, provider, model, force=True)
            sparkle.set_state("idle")
            style = "success" if did_compact else "dim"
            console.print(f"  [{style}]{msg}[/]\n")
    elif command == "/file":
        handle_file_command(arg, file_ctx)
    elif command == "/image":
        img_parts = arg.strip().split(maxsplit=1)
        img_path = img_parts[0] if img_parts else ""
        img_prompt = img_parts[1] if len(img_parts) > 1 else ""
        if not img_path:
            console.print("\n  [dim]Usage: /image <path> [prompt][/dim]")
            console.print("  [dim]Supported: PNG, JPG, GIF, WebP, BMP (max 10MB)[/dim]")
            console.print("  [dim]Image is sent with your next message, or immediately with a prompt.[/dim]\n")
        else:
            resolved, error = validate_image(img_path)
            if error:
                console.print(f"\n  [error]{error}[/]\n")
            else:
                data_uri, mime = encode_image(resolved)
                size_kb = resolved.stat().st_size / 1024
                if img_prompt:
                    # Send immediately with prompt
                    console.print(f"\n  [success]Sending image:[/] {resolved.name} "
                                  f"[dim]({size_kb:.0f}KB, {mime})[/dim]\n")
                    print_user_message(f"[image: {resolved.name}] {img_prompt}")
                    msg = build_image_message(img_prompt, data_uri)
                    messages.append(msg)
                    token_tracker.record_input([msg], provider_name)
                    response_text = ""
                    buf = StreamBuffer()
                    sparkle.set_state("thinking")
                    with Live(console=console, refresh_per_second=15,
                              vertical_overflow="visible") as live:
                        got_first = False
                        async for token in provider.chat_stream(
                            messages=messages, model=model, system_prompt=system_prompt,
                        ):
                            if not got_first:
                                got_first = True
                                sparkle.set_state("responding")
                            if buf.add(token):
                                sparkle.tick()
                                char, style = sparkle.rich_str()
                                header = Text.from_markup(
                                    f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                                md = Markdown(buf.text, code_theme="monokai")
                                live.update(Group(header, md))
                        if got_first:
                            final = buf.flush()
                            sparkle.tick()
                            char, style = sparkle.rich_str()
                            header = Text.from_markup(
                                f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                            live.update(Group(header, Markdown(final, code_theme="monokai")))
                    response_text = buf.text
                    if response_text:
                        messages.append({"role": "assistant", "content": response_text})
                        token_tracker.record_output(response_text, provider_name)
                        response_history.push(response_text)
                    sparkle.set_state("idle")
                    console.print()
                else:
                    # Queue for next message
                    image_ctx.set(data_uri, resolved.name)
                    console.print(f"\n  [success]Image queued:[/] {resolved.name} "
                                  f"[dim]({size_kb:.0f}KB, {mime})[/dim]")
                    console.print("  [dim]Type your message and the image will be attached.[/dim]\n")
    elif command == "/run":
        await handle_run_command(arg, sparkle)
    elif command == "/cost":
        console.print(f"\n{token_tracker.summary(provider_name)}\n")
    elif command == "/export":
        handle_export_command(arg, messages, model)
    elif command == "/save":
        handle_session_save(arg, messages, model)
    elif command == "/load":
        loaded_model = handle_session_load(arg, messages)
        if loaded_model:
            model = loaded_model
    elif command == "/sessions":
        handle_sessions_command(arg)
    elif command == "/agent":
        await handle_agent_command(arg, registry, session)
    elif command == "/providers":
        handle_providers_command()
    elif command == "/alias":
        if alias_mgr:
            handle_alias_command(arg, alias_mgr)
        else:
            console.print("\n  [dim]Alias manager not available.[/dim]\n")
    elif command == "/multi":
        if multiline_state is not None:
            multiline_state["enabled"] = not multiline_state["enabled"]
            state_str = "ON" if multiline_state["enabled"] else "OFF"
            if multiline_state["enabled"]:
                console.print(f"\n  [success]Multiline mode: {state_str}[/]")
                console.print("  [dim]Enter = newline.  Esc+Enter or Alt+Enter = submit.[/dim]\n")
            else:
                console.print(f"\n  [success]Multiline mode: {state_str}[/]")
                console.print("  [dim]Enter = submit.  Esc+Enter or Alt+Enter = newline.[/dim]\n")
        else:
            console.print("\n  [dim]Multiline state not available.[/dim]\n")
    elif command == "/web":
        web_parts = arg.strip().split(maxsplit=1)
        web_url = web_parts[0] if web_parts else ""
        web_prompt = web_parts[1] if len(web_parts) > 1 else ""
        if not web_url:
            console.print("\n  [dim]Usage: /web <url> [prompt][/dim]")
            console.print("  [dim]Fetches a URL and optionally sends the content to the LLM.[/dim]\n")
        else:
            console.print(f"\n  [dim]Fetching {web_url}...[/dim]")
            text, error = await fetch_web_content(web_url)
            if error:
                console.print(f"  [error]{error}[/]\n")
            elif text:
                chars = len(text)
                console.print(f"  [success]Fetched {chars:,} chars[/]\n")
                if web_prompt:
                    # Send to LLM with the fetched content
                    full_prompt = (
                        f"Here is the content from {web_url}:\n\n"
                        f"---\n{text}\n---\n\n{web_prompt}"
                    )
                    print_user_message(f"[web: {web_url}] {web_prompt}")
                    undo_mgr.clear_redo()
                    messages.append({"role": "user", "content": full_prompt})
                    token_tracker.record_input(
                        [{"role": "user", "content": full_prompt}], provider_name)
                    response_text = ""
                    buf = StreamBuffer()
                    sparkle.set_state("thinking")
                    with Live(console=console, refresh_per_second=15,
                              vertical_overflow="visible") as live:
                        got_first = False
                        async for token in provider.chat_stream(
                            messages=messages, model=model,
                            system_prompt=system_prompt,
                        ):
                            if not got_first:
                                got_first = True
                                sparkle.set_state("responding")
                            if buf.add(token):
                                sparkle.tick()
                                char, style = sparkle.rich_str()
                                header = Text.from_markup(
                                    f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                                md = Markdown(buf.text, code_theme="monokai")
                                live.update(Group(header, md))
                        if got_first:
                            final = buf.flush()
                            sparkle.tick()
                            char, style = sparkle.rich_str()
                            header = Text.from_markup(
                                f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                            live.update(Group(header, Markdown(final, code_theme="monokai")))
                    response_text = buf.text
                    if response_text:
                        messages.append({"role": "assistant", "content": response_text})
                        token_tracker.record_output(response_text, provider_name)
                        response_history.push(response_text)
                    sparkle.set_state("idle")
                    console.print()
                else:
                    # Just display a preview
                    preview = text[:2000]
                    if len(text) > 2000:
                        preview += f"\n\n... ({chars:,} chars total, use /web <url> <prompt> to analyze)"
                    console.print(Panel(
                        preview, title=f"[bold white] {web_url} [/]",
                        title_align="left", border_style="bright_cyan",
                        padding=(1, 2)))
                    console.print()
    else:
        console.print(f"\n  [error]Unknown command:[/] {command}")
        console.print("  [dim]Type /help for available commands.[/dim]\n")

    return False, model, system_prompt, provider, provider_name


# ═══ Agent Commands ══════════════════════════════════════════════════════════

async def handle_agent_command(arg, registry, session):
    parts = arg.strip().split(maxsplit=1) if arg else []
    if not parts:
        console.print("\n  [dim]Usage:[/dim]")
        console.print("    [bold cyan]/agent create <name>[/]    Create a new agent")
        console.print("    [bold cyan]/agent template <name>[/]  Create from template")
        console.print("    [bold cyan]/agent templates[/]        List available templates")
        console.print("    [bold cyan]/agent list[/]             List all agents")
        console.print("    [bold cyan]/agent delete <name>[/]    Delete an agent")
        console.print("    [bold cyan]/agent info <name>[/]      Show agent details\n")
        return

    subcmd = parts[0].lower()
    subarg = parts[1].strip() if len(parts) > 1 else ""

    match subcmd:
        case "create":
            if not subarg:
                console.print("  [error]Usage: /agent create <name>[/]\n")
                return
            console.print(f"\n  [bold]Creating agent:[/] [bright_cyan]{subarg}[/]\n")

            description = await session.prompt_async(
                [("fg:#c084fc bold", "  Specialty "), ("", "> ")])
            if not description.strip():
                console.print("  [error]Cancelled.[/]\n")
                return

            prov = await session.prompt_async(
                [("fg:#c084fc bold", "  Provider "), ("fg:#888888", "(ollama/anthropic/openai) "), ("", "> ")])
            prov = prov.strip().lower()
            if prov not in ("ollama", "anthropic", "openai"):
                console.print(f"  [error]Unknown provider: {prov}[/]\n")
                return

            model_name = await session.prompt_async(
                [("fg:#c084fc bold", "  Model "), ("", "> ")])
            if not model_name.strip():
                console.print("  [error]Cancelled.[/]\n")
                return

            try:
                agent = registry.create(subarg, description.strip(), prov, model_name.strip())
                console.print(f"\n  [success]Created[/] [{agent.color}]{agent.name}[/] "
                              f"[dim]{agent.provider}/{agent.model}[/dim]\n")
            except ValueError as e:
                console.print(f"  [error]{e}[/]\n")

        case "list":
            agents = registry.list_agents()
            console.print()
            for a in agents:
                marker = "[bold bright_magenta]\u273b[/]" if a.is_orchestrator else "[dim]\u25cb[/dim]"
                console.print(f"  {marker} [{a.color}]{a.name:<16}[/] [dim]{a.provider}/{a.model}[/dim]")
            console.print()

        case "templates":
            console.print("\n  [bold]Available Agent Templates[/]\n")
            for tname, tmpl in AGENT_TEMPLATES.items():
                desc_preview = tmpl["description"][:80] + "..."
                console.print(f"  [bold bright_cyan]{tname:<14}[/] [dim]{desc_preview}[/dim]")
            console.print(f"\n  [dim]Usage: /agent template <name>[/dim]\n")

        case "template":
            if not subarg:
                console.print("  [error]Usage: /agent template <name>[/]")
                console.print(f"  [dim]Available: {', '.join(AGENT_TEMPLATES)}[/dim]\n")
                return
            if subarg not in AGENT_TEMPLATES:
                console.print(f"  [error]Unknown template: '{subarg}'[/]")
                console.print(f"  [dim]Available: {', '.join(AGENT_TEMPLATES)}[/dim]\n")
                return

            console.print(f"\n  [bold]Creating from template:[/] [bright_cyan]{subarg}[/]\n")

            prov = await session.prompt_async(
                [("fg:#c084fc bold", "  Provider "), ("fg:#888888", "(ollama/anthropic/openai) "), ("", "> ")])
            prov = prov.strip().lower()
            if prov not in ("ollama", "anthropic", "openai"):
                console.print(f"  [error]Unknown provider: {prov}[/]\n")
                return

            model_name = await session.prompt_async(
                [("fg:#c084fc bold", "  Model "), ("", "> ")])
            if not model_name.strip():
                console.print("  [error]Cancelled.[/]\n")
                return

            try:
                agent = registry.create_from_template(subarg, prov, model_name.strip())
                console.print(f"\n  [success]Created[/] [{agent.color}]{agent.name}[/] "
                              f"[dim]{agent.provider}/{agent.model}[/dim]\n")
            except ValueError as e:
                console.print(f"  [error]{e}[/]\n")

        case "delete":
            if not subarg:
                console.print("  [error]Usage: /agent delete <name>[/]\n")
                return
            try:
                registry.delete(subarg)
                console.print(f"\n  [success]Deleted:[/] {subarg}\n")
            except ValueError as e:
                console.print(f"\n  [error]{e}[/]\n")

        case "info":
            if not subarg:
                console.print("  [error]Usage: /agent info <name>[/]\n")
                return
            agent = registry.get(subarg)
            if not agent:
                console.print(f"\n  [error]Agent '{subarg}' not found.[/]\n")
                return
            console.print()
            console.print(f"  [bold {agent.color}]{agent.name}[/]")
            console.print(f"  [dim]Provider:[/dim]  {agent.provider}")
            console.print(f"  [dim]Model:[/dim]     {agent.model}")
            console.print(f"  [dim]Color:[/dim]     [{agent.color}]\u2588\u2588[/]")
            desc_preview = agent.description[:100] + ("..." if len(agent.description) > 100 else "")
            console.print(f"  [dim]System:[/dim]    {desc_preview}")
            console.print()

        case _:
            console.print(f"  [error]Unknown subcommand: {subcmd}[/]\n")


def handle_providers_command():
    config = load_provider_config()
    console.print("\n  [bold]Provider Configuration[/]\n")
    console.print(f"  [bold cyan]ollama[/]     [bold bright_green]configured[/]  [dim]{config.ollama_base_url}[/dim]")
    if config.anthropic_api_key:
        masked = config.anthropic_api_key[:10] + "..." + config.anthropic_api_key[-4:]
        console.print(f"  [bold cyan]anthropic[/]  [bold bright_green]configured[/]  [dim]{masked}[/dim]")
    else:
        console.print(f"  [bold cyan]anthropic[/]  [dim]not configured[/dim]")
    if config.openai_api_key:
        masked = config.openai_api_key[:7] + "..." + config.openai_api_key[-4:]
        console.print(f"  [bold cyan]openai[/]     [bold bright_green]configured[/]  [dim]{masked}[/dim]")
    else:
        console.print(f"  [bold cyan]openai[/]     [dim]not configured[/dim]")
    console.print(f"\n  [dim]Edit ~/.hivemind/providers.json to configure.[/dim]\n")


# ═══ Smart Streaming Buffer ══════════════════════════════════════════════════

class StreamBuffer:
    """Buffers streaming tokens and only signals a re-render when a logical
    block boundary is detected (end of paragraph, code fence, list item, etc.).
    This dramatically reduces flicker compared to re-rendering on every token.
    """

    # Patterns that indicate a good re-render point
    _BLOCK_ENDS = re.compile(
        r"(\n\n"           # paragraph break
        r"|```\s*\n"       # code fence open/close
        r"|\n[-*+] "       # list item start
        r"|\n\d+\. "       # numbered list item
        r"|\n#{1,6} "      # heading
        r"|\n>"            # blockquote
        r"|\n\|"           # table row
        r"|\n---"          # horizontal rule
        r"|[.!?]\s)"       # sentence end
    )

    def __init__(self, min_interval: float = 0.05):
        self._text = ""
        self._render_text = ""  # last text sent to render
        self._min_interval = min_interval
        self._last_render = 0.0
        self._token_count = 0

    def add(self, token: str) -> bool:
        """Add a token. Returns True if it's time to re-render."""
        self._text += token
        self._token_count += 1
        now = time.monotonic()
        elapsed = now - self._last_render

        # Always render on first few tokens (so user sees immediate response)
        if self._token_count <= 3:
            self._render_text = self._text
            self._last_render = now
            return True

        # Respect minimum interval
        if elapsed < self._min_interval:
            return False

        # Check if we hit a block boundary in the new content
        new_content = self._text[len(self._render_text):]
        if self._BLOCK_ENDS.search(new_content):
            self._render_text = self._text
            self._last_render = now
            return True

        # Fallback: render every ~150ms even without a boundary
        if elapsed > 0.15:
            self._render_text = self._text
            self._last_render = now
            return True

        return False

    @property
    def text(self) -> str:
        return self._text

    def flush(self) -> str:
        """Return final complete text."""
        self._render_text = self._text
        return self._text


# ═══ Undo / Redo ════════════════════════════════════════════════════════════

class UndoManager:
    """Tracks undo/redo state for conversation message pairs."""

    def __init__(self):
        self._redo_stack: list[list[dict]] = []  # stack of [user_msg, assistant_msg]

    def can_undo(self, messages: list[dict]) -> bool:
        """Check if there's at least one user+assistant pair to undo."""
        # Find last user+assistant pair (ignoring system messages)
        count = 0
        for msg in reversed(messages):
            if msg["role"] in ("user", "assistant"):
                count += 1
            if count >= 2:
                return True
        return False

    def undo(self, messages: list[dict]) -> tuple[bool, str]:
        """Remove last user+assistant pair. Returns (success, status)."""
        if not messages:
            return False, "Nothing to undo."

        removed = []
        # Pop from end: expect assistant then user
        while messages and len(removed) < 2:
            if messages[-1]["role"] in ("user", "assistant"):
                removed.append(messages.pop())
            elif messages[-1]["role"] == "system":
                break  # don't undo system messages
            else:
                messages.pop()  # skip unknown roles

        if not removed:
            return False, "Nothing to undo."

        self._redo_stack.append(list(reversed(removed)))
        return True, f"Undone {len(removed)} message(s). Use /redo to restore."

    def redo(self, messages: list[dict]) -> tuple[bool, str]:
        """Restore last undone pair. Returns (success, status)."""
        if not self._redo_stack:
            return False, "Nothing to redo."

        pair = self._redo_stack.pop()
        messages.extend(pair)
        return True, f"Restored {len(pair)} message(s)."

    def clear_redo(self):
        """Clear redo stack (called when new messages are added)."""
        self._redo_stack.clear()


# ═══ UI ══════════════════════════════════════════════════════════════════════

PHOENIX_ART = [
    "         [bold bright_red].[/][bold yellow]✦[/][bold bright_red].[/]",
    "        [bold yellow].[/][bold bright_red]╱[/][bold bright_yellow]▲[/][bold bright_red]╲[/][bold yellow].[/]",
    "       [bold bright_yellow]╱[/][bold bright_red]▓[/][bold bright_yellow]▓[/][bold bright_red]▓[/][bold bright_yellow]╲[/]",
    "      [bold yellow]╱[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold yellow]╲[/]",
    "     [bold bright_red]╱[/][bold bright_yellow]▓[/][bold bright_red]█[/][bold bright_yellow]▓[/][bold bright_red]▓[/][bold bright_yellow]▓[/][bold bright_red]█[/][bold bright_yellow]▓[/][bold bright_red]╲[/]",
    "    [bold yellow]╱[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold yellow]╲[/]",
    "     [bold bright_yellow]╲[/][bold bright_red]▓[/] [bold bright_yellow]╲[/][bold bright_red]▓[/][bold bright_yellow]▓[/][bold bright_red]╱[/] [bold bright_red]▓[/][bold bright_yellow]╱[/]",
    "      [bold bright_red]⚡[/] [bold bright_yellow]╲[/][bold bright_red]▓[/][bold bright_yellow]╱[/] [bold bright_red]⚡[/]",
    "         [bold bright_yellow]▓[/]",
]

PHOENIX_PLAIN = [
    "         .✦.         ",
    "        .╱▲╲.        ",
    "       ╱▓▓▓╲       ",
    "      ╱▓█▓█▓╲      ",
    "     ╱▓█▓▓▓█▓╲     ",
    "    ╱▓█▓█▓█▓█▓╲    ",
    "     ╲▓ ╲▓▓╱ ▓╱     ",
    "      ⚡ ╲▓╱ ⚡      ",
    "         ▓          ",
]


def _startup_animation():
    frames = [
        "         [bold bright_red].[/][bold yellow]✦[/][bold bright_red].[/]",
        "        [bold yellow].[/][bold bright_red]╱[/][bold bright_yellow]▲[/][bold bright_red]╲[/][bold yellow].[/]",
        "       [bold bright_yellow]╱[/][bold bright_red]▓[/][bold bright_yellow]▓[/][bold bright_red]▓[/][bold bright_yellow]╲[/]",
        "      [bold yellow]╱[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold bright_yellow]█[/][bold bright_red]▓[/][bold yellow]╲[/]",
        f"      [bold bright_red]{APP_NAME}[/]",
    ]
    with Live(console=console, refresh_per_second=12, transient=True) as live:
        for f in frames:
            live.update(Text.from_markup(f"\n{f}\n"))
            time.sleep(0.12)
        time.sleep(0.2)


def _short_path(p: Path) -> str:
    """Shorten a path for display: ~/foo/bar instead of /Users/name/foo/bar."""
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def print_banner(model, sparkle, provider_name="ollama", animate=True):
    console.print()
    if animate:
        _startup_animation()

    cwd = _short_path(Path.cwd())

    for line in PHOENIX_ART:
        console.print(Text.from_markup(f"  {line}"))

    banner = Text()
    banner.append("\n  ━" * 1, style="dim")
    banner.append("━" * 25, style="dim")
    banner.append("\n  ⚡ ", style="bold bright_yellow")
    banner.append(APP_NAME, style="bold bright_red")
    banner.append(f"  v{APP_VERSION}\n", style="dim")
    banner.append("  ━" * 1, style="dim")
    banner.append("━" * 25 + "\n", style="dim")
    banner.append("\n  Path     ", style="dim")
    banner.append(f"{cwd}\n", style="bold white")
    banner.append("  Model    ", style="dim")
    banner.append(f"{model}\n", style="bold bright_cyan")
    banner.append("  Provider ", style="dim")
    banner.append(f"{provider_name}\n\n", style="bold white")
    banner.append("  /help", style="bold cyan")
    banner.append(" for commands   ", style="dim")
    banner.append("Ctrl+D", style="bold")
    banner.append(" to exit\n", style="dim")
    console.print(banner)


def print_user_message(msg):
    console.print()
    console.print(f"  [bold bright_cyan]> {msg}[/]")
    console.print()


async def stream_response(provider: BaseProvider, messages: list, user_input: str,
                          sparkle: Sparkle, model: str,
                          file_ctx: FileContextManager | None = None,
                          system_prompt: str = "") -> str:
    """Single-agent streaming response with smart buffer. Returns accumulated text."""
    content = user_input
    if file_ctx:
        ctx_block = file_ctx.build_context_block()
        if ctx_block:
            content = ctx_block + content
    messages.append({"role": "user", "content": content})
    enriched_prompt = build_system_prompt(system_prompt)
    buf = StreamBuffer()
    cancelled = False
    sparkle.set_state("thinking")

    with Live(console=console, refresh_per_second=15, vertical_overflow="visible") as live:
        try:
            got_first = False

            async def thinking_loop():
                while not got_first:
                    sparkle.tick()
                    char, style = sparkle.rich_str()
                    live.update(Text.from_markup(f"  [{style}]{char}[/] [dim]Thinking...[/dim]"))
                    await asyncio.sleep(0.08)

            task = asyncio.create_task(thinking_loop())

            async for token in provider.chat_stream(
                messages=messages, model=model, system_prompt=enriched_prompt,
            ):
                if not got_first:
                    got_first = True
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    sparkle.set_state("responding")

                should_render = buf.add(token)
                if should_render:
                    sparkle.tick()
                    char, style = sparkle.rich_str()
                    header = Text.from_markup(f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                    md = Markdown(buf.text, code_theme="monokai")
                    live.update(Group(header, md))

            # Final render with complete text
            if got_first:
                final_text = buf.flush()
                sparkle.tick()
                char, style = sparkle.rich_str()
                header = Text.from_markup(f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                md = Markdown(final_text, code_theme="monokai")
                live.update(Group(header, md))
            else:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except KeyboardInterrupt:
            cancelled = True

    accumulated = buf.text
    if cancelled:
        console.print("  [warning]Generation cancelled.[/warning]")
    elif accumulated:
        messages.append({"role": "assistant", "content": accumulated})

    sparkle.set_state("idle")
    console.print()
    return accumulated


async def demo_response(user_input, sparkle):
    demo_text = (
        f"This is **demo mode** \u2014 no Ollama connection needed.\n\n"
        f"You said: *{user_input}*\n\n"
        f"```python\nprint('Hello from Hivemind!')\n```\n\n"
        f"Try: `/help`, `/agent list`, `/swarm explain binary search`."
    )
    accumulated = ""
    sparkle.set_state("thinking")

    with Live(console=console, refresh_per_second=12, vertical_overflow="visible") as live:
        for _ in range(10):
            sparkle.tick()
            char, style = sparkle.rich_str()
            live.update(Text.from_markup(f"  [{style}]{char}[/] [dim]Thinking...[/dim]"))
            await asyncio.sleep(0.08)

        sparkle.set_state("responding")
        for ch in demo_text:
            accumulated += ch
            sparkle.tick()
            char, style = sparkle.rich_str()
            header = Text.from_markup(f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
            md = Markdown(accumulated, code_theme="monokai")
            live.update(Group(header, md))
            await asyncio.sleep(0.012)

    sparkle.set_state("idle")
    console.print()
    return accumulated


# ═══ Swarm UI ════════════════════════════════════════════════════════════════

STATUS_ICONS = {
    TaskStatus.PENDING:    ("\u25cb", "dim"),
    TaskStatus.THINKING:   ("\u280b", "bold bright_yellow"),
    TaskStatus.RESPONDING: ("\u2726", "bold bright_green"),
    TaskStatus.DONE:       ("\u2713", "bold bright_green"),
    TaskStatus.ERROR:      ("\u2717", "bold red"),
}


async def swarm_response(user_task: str, registry: AgentRegistry, sparkle: Sparkle,
                         demo_mode: bool = False):
    task_statuses: dict[str, tuple[str, TaskStatus, str]] = {}

    async def on_status(task_id, agent_name, status, detail=""):
        preview = detail[:80].replace("\n", " ") if detail else ""
        task_statuses[task_id] = (agent_name, status, preview)

    def build_status_panel():
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("s", width=2)
        table.add_column("agent", width=16)
        table.add_column("task", ratio=1)
        table.add_column("preview", ratio=2, style="dim")

        for task_id, (agent_name, status, preview) in task_statuses.items():
            agent = registry.get(agent_name)
            color = agent.color if agent else "white"
            icon_char, icon_style = STATUS_ICONS.get(status, ("?", "dim"))
            table.add_row(
                Text(icon_char, style=icon_style),
                Text(agent_name, style=f"bold {color}"),
                Text(task_id, style="dim"),
                Text(preview, style="dim"),
            )
        return Panel(table, title="[bold white] Swarm Progress [/]",
                     border_style="bright_magenta", padding=(1, 1))

    print_user_message(user_task)

    # Phase 1: Planning
    sparkle.set_state("thinking")
    console.print(Text.from_markup("  [bold bright_magenta]\u273b[/] [dim]Orchestrator planning...[/dim]\n"))

    if demo_mode:
        plan, results, synthesis = await demo_swarm(user_task, registry, on_status=on_status)
    else:
        runner = SwarmRunner(registry=registry, on_status=on_status)
        try:
            plan = await runner.plan(user_task)
        except (ValueError, json.JSONDecodeError) as e:
            console.print(f"  [error]Planning failed: {e}[/]\n")
            sparkle.set_state("idle")
            return

        # Phase 2: Show plan
        console.print("  [bold]Execution Plan:[/]")
        for t in plan.subtasks:
            agent = registry.get(t.agent_name)
            color = agent.color if agent else "white"
            deps = f" [dim](after: {', '.join(t.depends_on)})[/dim]" if t.depends_on else ""
            console.print(f"    [{color}]{t.agent_name}[/] \u2192 {t.task}{deps}")
        console.print()

        # Phase 3: Execute with live status
        for t in plan.subtasks:
            task_statuses[t.id] = (t.agent_name, TaskStatus.PENDING, t.task[:60])

        with Live(build_status_panel(), console=console, refresh_per_second=8) as live:
            async def refresh_loop():
                while True:
                    live.update(build_status_panel())
                    await asyncio.sleep(0.125)

            refresh_task = asyncio.create_task(refresh_loop())
            try:
                results = await runner.execute(plan)
            finally:
                refresh_task.cancel()
                try:
                    await refresh_task
                except asyncio.CancelledError:
                    pass

        # Status summary
        console.print()
        for t in plan.subtasks:
            agent = registry.get(t.agent_name)
            color = agent.color if agent else "white"
            icon = "\u2713" if t.status == TaskStatus.DONE else "\u2717"
            ist = "bold bright_green" if t.status == TaskStatus.DONE else "bold red"
            console.print(f"  [{ist}]{icon}[/] [{color}]{t.agent_name}[/] [dim]{t.id}[/dim]")
        console.print()

        # Phase 4: Streaming synthesis
        sparkle.set_state("thinking")
        synthesis = ""
        with Live(console=console, refresh_per_second=12, vertical_overflow="visible") as live:
            got_first = False

            async def synthesis_thinking_loop():
                while not got_first:
                    sparkle.tick()
                    char, style = sparkle.rich_str()
                    live.update(Text.from_markup(
                        f"  [{style}]{char}[/] [dim]Synthesizing results...[/dim]"))
                    await asyncio.sleep(0.08)

            anim_task = asyncio.create_task(synthesis_thinking_loop())

            async for token in runner.synthesize_stream(user_task, results, plan):
                if not got_first:
                    got_first = True
                    anim_task.cancel()
                    try:
                        await anim_task
                    except asyncio.CancelledError:
                        pass
                    sparkle.set_state("responding")

                synthesis += token
                sparkle.tick()
                live.update(Panel(
                    Markdown(synthesis, code_theme="monokai"),
                    title="[bold bright_magenta] \u273b Swarm Result [/]",
                    border_style="bright_magenta",
                    padding=(1, 2),
                ))

            if not got_first:
                got_first = True
                anim_task.cancel()
                try:
                    await anim_task
                except asyncio.CancelledError:
                    pass

    # Display final result (demo mode uses non-streaming)
    if demo_mode:
        sparkle.set_state("responding")
        console.print(Panel(
            Markdown(synthesis, code_theme="monokai"),
            title="[bold bright_magenta] \u273b Swarm Result [/]",
            border_style="bright_magenta",
            padding=(1, 2),
        ))
    console.print()
    sparkle.set_state("idle")


def print_goodbye():
    console.print()
    console.print(Text.from_markup(
        "  [bold bright_magenta]\u273b[/] [dim]Goodbye! See you next time.[/dim]\n"
    ))


def print_connection_error():
    console.print()
    err = Text()
    err.append("  \u273b ", style="bold red")
    err.append("Cannot connect to Ollama\n\n", style="bold red")
    err.append("  1. ", style="bold white")
    err.append("Install  ", style="white")
    err.append("https://ollama.com\n", style="bold cyan")
    err.append("  2. ", style="bold white")
    err.append("Start   ", style="white")
    err.append("ollama serve\n", style="bold cyan")
    err.append("  3. ", style="bold white")
    err.append("Pull    ", style="white")
    err.append("ollama pull llama3.2\n", style="bold cyan")
    console.print(Panel(err, border_style="red", padding=(1, 1)))
    console.print()


# ═══ Main ════════════════════════════════════════════════════════════════════

async def main():
    demo_mode = "--demo" in sys.argv
    piped = not sys.stdin.isatty()

    provider_config = load_provider_config()
    provider_name = "ollama"
    provider = get_provider(provider_name, provider_config)
    model = DEFAULT_MODEL
    system_prompt = DEFAULT_SYSTEM_PROMPT

    # ── Pipe / One-shot mode ────────────────────────────────────────────
    if piped:
        input_text = sys.stdin.read().strip()
        if not input_text:
            cli_args = [a for a in sys.argv[1:] if not a.startswith("--")]
            if cli_args:
                input_text = " ".join(cli_args)
            else:
                print("Error: no input provided.", file=sys.stderr)
                sys.exit(1)

        if not demo_mode:
            connected = await provider.check_connection()
            if not connected:
                print("Error: cannot connect to Ollama.", file=sys.stderr)
                sys.exit(1)
            models = await provider.list_models()
            if models and model not in models:
                model = models[0]

        if demo_mode:
            print(f"[demo] You asked: {input_text[:100]}")
        else:
            await run_oneshot(input_text, provider, model, system_prompt)
        return

    # ── Interactive mode ────────────────────────────────────────────────
    sparkle = Sparkle()
    registry = AgentRegistry()
    messages: list[dict] = []
    file_ctx = FileContextManager()
    undo_mgr = UndoManager()
    image_ctx = ImageContext()
    token_tracker = TokenTracker()
    response_history = ResponseHistory()
    alias_mgr = AliasManager()
    multiline_state = {"enabled": False}

    if not demo_mode:
        connected = await provider.check_connection()
        if not connected:
            print_connection_error()
            sys.exit(1)
        models = await provider.list_models()
        if models and model not in models:
            model = models[0]

    print_banner(model, sparkle, provider_name=provider_name, animate=True)

    if demo_mode:
        console.print(
            "  [bold yellow]~ Demo mode ~[/] "
            "[dim]Ollama not required. Type anything to explore the UI.[/dim]\n"
        )

    completer = WordCompleter(list(SLASH_COMMANDS.keys()), sentence=True)
    kb = KeyBindings()

    @kb.add(Keys.Escape, Keys.Enter)
    def _alt_enter(event):
        """In normal mode: insert newline. In multiline mode: submit."""
        if multiline_state["enabled"]:
            event.current_buffer.validate_and_handle()
        else:
            event.current_buffer.insert_text("\n")

    @kb.add(Keys.Enter)
    def _enter(event):
        """In normal mode: submit. In multiline mode: insert newline."""
        if multiline_state["enabled"]:
            event.current_buffer.insert_text("\n")
        else:
            event.current_buffer.validate_and_handle()

    def get_prompt():
        sparkle.tick()
        cwd_name = Path.cwd().name or "/"
        parts = [
            (sparkle.pt_style(), f"  {sparkle.pt_char()} "),
            ("fg:#c084fc bold", APP_NAME),
            ("fg:#888888", f" {cwd_name}"),
        ]
        n_files = len(file_ctx.files)
        if n_files:
            parts.append(("fg:#888888", f" [{n_files} file{'s' if n_files > 1 else ''}]"))
        if image_ctx.has_pending:
            parts.append(("fg:#888888", f" [img:{image_ctx.pending_name}]"))
        if multiline_state["enabled"]:
            parts.append(("fg:#888888", " [multi]"))
        parts.append(("", " > "))
        return parts

    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=completer,
        key_bindings=kb,
        multiline=False,
        refresh_interval=0.5,
    )

    while True:
        try:
            sparkle.set_state("idle")
            user_input = await session.prompt_async(get_prompt)
        except KeyboardInterrupt:
            continue
        except EOFError:
            print_goodbye()
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # ── Alias expansion ────────────────────────────────────────────
        user_input = alias_mgr.expand(user_input)

        # ── Shell integration: ! and !! ─────────────────────────────────
        if user_input.startswith("!!") and len(user_input) > 2:
            shell_cmd = user_input[2:].strip()
            console.print(f"\n  [bold bright_cyan]$ {shell_cmd}[/]")
            output, exit_code = run_shell(shell_cmd)
            display_shell_output(output, exit_code)

            if output.strip() and not demo_mode:
                shell_prompt = (
                    f"I ran this shell command:\n```\n$ {shell_cmd}\n```\n\n"
                    f"Output (exit code {exit_code}):\n```\n{output.strip()}\n```\n\n"
                    f"Please analyze or explain this output."
                )
                undo_mgr.clear_redo()
                token_tracker.record_input(
                    [{"role": "user", "content": shell_prompt}], provider_name)
                response_text = await stream_response(
                    provider, messages, shell_prompt, sparkle, model, file_ctx,
                    system_prompt)
                if response_text:
                    token_tracker.record_output(response_text, provider_name)
                    response_history.push(response_text)
                blocks = extract_python_blocks(response_text)
                if blocks:
                    await auto_run_blocks(
                        blocks, sparkle, session, messages, provider, model,
                        system_prompt)
            continue

        if user_input.startswith("!") and len(user_input) > 1:
            shell_cmd = user_input[1:].strip()
            console.print(f"\n  [bold bright_cyan]$ {shell_cmd}[/]")
            output, exit_code = run_shell(shell_cmd)
            display_shell_output(output, exit_code)
            continue

        # ── Slash commands ──────────────────────────────────────────────
        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            command = parts[0].lower()

            if command == "/swarm":
                task = parts[1] if len(parts) > 1 else ""
                if not task:
                    console.print("\n  [dim]Usage: /swarm <task>[/dim]\n")
                    continue
                await swarm_response(task, registry, sparkle, demo_mode)
                continue

            should_exit, model, system_prompt, provider, provider_name = (
                await handle_command(
                    user_input, provider, provider_name, provider_config, messages,
                    model, sparkle, registry, session, file_ctx, system_prompt,
                    undo_mgr, image_ctx, token_tracker, response_history,
                    alias_mgr, multiline_state))
            if should_exit:
                print_goodbye()
                break
            continue

        # ── Normal chat ─────────────────────────────────────────────────
        undo_mgr.clear_redo()

        # Check for pending image → build multimodal message
        pending_image = image_ctx.take()
        if pending_image and not demo_mode:
            print_user_message(f"[image] {user_input}")
            msg = build_image_message(user_input, pending_image)
            messages.append(msg)
            token_tracker.record_input([msg], provider_name)

            buf = StreamBuffer()
            sparkle.set_state("thinking")
            with Live(console=console, refresh_per_second=15,
                      vertical_overflow="visible") as live:
                got_first = False
                async for token in provider.chat_stream(
                    messages=messages, model=model, system_prompt=system_prompt,
                ):
                    if not got_first:
                        got_first = True
                        sparkle.set_state("responding")
                    if buf.add(token):
                        sparkle.tick()
                        char, style = sparkle.rich_str()
                        header = Text.from_markup(
                            f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                        md = Markdown(buf.text, code_theme="monokai")
                        live.update(Group(header, md))
                if got_first:
                    final = buf.flush()
                    sparkle.tick()
                    char, style = sparkle.rich_str()
                    header = Text.from_markup(
                        f"  [{style}]{char}[/] [bold bright_green]Assistant[/]\n")
                    live.update(Group(header, Markdown(final, code_theme="monokai")))

            response_text = buf.text
            if response_text:
                messages.append({"role": "assistant", "content": response_text})
                token_tracker.record_output(response_text, provider_name)
                response_history.push(response_text)
            sparkle.set_state("idle")
            console.print()
        else:
            print_user_message(user_input)
            if demo_mode:
                response_text = await demo_response(user_input, sparkle)
            else:
                token_tracker.record_input(
                    [{"role": "user", "content": user_input}], provider_name)
                response_text = await stream_response(
                    provider, messages, user_input, sparkle, model, file_ctx,
                    system_prompt)
                if response_text:
                    token_tracker.record_output(response_text, provider_name)

            response_history.push(response_text)

        # Auto-run: detect Python code blocks and offer to execute
        blocks = extract_python_blocks(response_text)
        if blocks:
            await auto_run_blocks(
                blocks, sparkle, session, messages, provider, model, system_prompt)

        # Auto-compact warning when context gets large
        if not demo_mode and auto_should_compact(messages):
            tokens_est = _estimate_tokens(messages)
            console.print(
                f"  [warning]Context is large ({len(messages)} msgs, ~{tokens_est:,} tokens). "
                f"Use /compact to summarize older messages.[/]\n")


def _entry():
    """Synchronous entry point for the console script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print_goodbye()
