"""Conversation session persistence — save, load, list, delete."""

import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

CONFIG_DIR = Path.home() / ".hivemind"
SESSIONS_DIR = CONFIG_DIR / "sessions"

MAX_SESSIONS = 50
MAX_NAME_LENGTH = 40
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_\- ]+$")


@dataclass
class SessionMeta:
    name: str
    model: str
    message_count: int
    created_at: float
    updated_at: float


@dataclass
class Session:
    name: str
    model: str
    messages: list[dict]
    created_at: float
    updated_at: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            name=data["name"],
            model=data["model"],
            messages=data["messages"],
            created_at=data.get("created_at", 0),
            updated_at=data.get("updated_at", 0),
        )


def _ensure_dir():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _session_path(name: str) -> Path:
    # Sanitize name to safe filename
    safe = name.strip().replace(" ", "_").lower()
    return SESSIONS_DIR / f"{safe}.json"


def _validate_name(name: str) -> str | None:
    """Returns error message or None."""
    if not name.strip():
        return "Session name cannot be empty."
    if len(name) > MAX_NAME_LENGTH:
        return f"Name too long (max {MAX_NAME_LENGTH} chars)."
    if not _SAFE_NAME_RE.match(name):
        return "Name can only contain letters, numbers, spaces, hyphens, and underscores."
    return None


def save_session(name: str, model: str, messages: list[dict]) -> tuple[bool, str]:
    """Save conversation to disk. Returns (success, message)."""
    error = _validate_name(name)
    if error:
        return False, error

    if not messages:
        return False, "No messages to save."

    _ensure_dir()

    path = _session_path(name)
    now = time.time()

    # If updating existing session, preserve created_at
    created_at = now
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            created_at = existing.get("created_at", now)
        except (json.JSONDecodeError, OSError):
            pass

    session = Session(
        name=name.strip(),
        model=model,
        messages=messages,
        created_at=created_at,
        updated_at=now,
    )
    path.write_text(json.dumps(session.to_dict(), indent=2, ensure_ascii=False))
    return True, f"Saved: '{name}' ({len(messages)} messages)"


def load_session(name: str) -> tuple[Session | None, str]:
    """Load a conversation from disk. Returns (session, error)."""
    _ensure_dir()
    path = _session_path(name)
    if not path.exists():
        return None, f"Session '{name}' not found."

    try:
        data = json.loads(path.read_text())
        session = Session.from_dict(data)
        return session, ""
    except (json.JSONDecodeError, KeyError) as e:
        return None, f"Corrupted session file: {e}"


def list_sessions() -> list[SessionMeta]:
    """List all saved sessions, sorted by most recent."""
    _ensure_dir()
    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            sessions.append(SessionMeta(
                name=data["name"],
                model=data.get("model", "?"),
                message_count=len(data.get("messages", [])),
                created_at=data.get("created_at", 0),
                updated_at=data.get("updated_at", 0),
            ))
        except (json.JSONDecodeError, KeyError):
            continue

    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def delete_session(name: str) -> tuple[bool, str]:
    """Delete a saved session. Returns (success, message)."""
    _ensure_dir()
    path = _session_path(name)
    if not path.exists():
        return False, f"Session '{name}' not found."
    path.unlink()
    return True, f"Deleted: '{name}'"
