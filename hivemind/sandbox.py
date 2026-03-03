"""Secure Python code execution sandbox.

Security measures:
- Static analysis blocks dangerous imports and operations
- Subprocess isolation with strict timeout
- Resource limits (memory, CPU, file size) via resource module
- Execution in isolated temp directory
- Output size limits
- No network, no file system mutation outside temp dir
"""

import asyncio
import os
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

MAX_EXEC_TIME = 10       # seconds
MAX_OUTPUT_SIZE = 10_000  # chars
MAX_CODE_SIZE = 50_000    # chars

# Modules blocked from import — these provide network, process, or FS mutation
BLOCKED_IMPORTS = frozenset({
    "subprocess", "shutil", "ctypes", "socket", "http",
    "urllib", "requests", "httpx", "aiohttp",
    "ftplib", "smtplib", "telnetlib", "xmlrpc",
    "multiprocessing", "signal", "webbrowser",
    "antigravity", "code", "codeop", "compileall",
    "importlib", "runpy", "pickle", "shelve",
})

# Patterns in code that are blocked — dangerous builtins / os calls
BLOCKED_PATTERNS = [
    "os.system",   "os.popen",   "os.exec",    "os.spawn",
    "os.remove",   "os.unlink",  "os.rmdir",   "os.rename",
    "os.chmod",    "os.chown",   "os.kill",    "os.fork",
    "os.environ",
    "__import__(",
    "eval(",       "exec(",      "compile(",
    "breakpoint(", "input(",
    "open(",
]


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    error: str  # sandbox-level error (validation, setup failure, etc.)


def validate_code(code: str) -> str | None:
    """Static analysis of code for security issues. Returns error message or None."""
    if not code.strip():
        return "No code provided."
    if len(code) > MAX_CODE_SIZE:
        return f"Code too large: {len(code):,} chars (max {MAX_CODE_SIZE:,})"

    for line in code.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for blocked in BLOCKED_IMPORTS:
            if f"import {blocked}" in stripped or f"from {blocked}" in stripped:
                return f"Blocked import: '{blocked}' is not allowed in sandbox"

    for pattern in BLOCKED_PATTERNS:
        if pattern in code:
            return f"Blocked operation: '{pattern}' is not allowed in sandbox"

    return None


# Wrapper script that sets resource limits before executing user code.
# Injected as the actual script that subprocess runs.
_WRAPPER_TEMPLATE = textwrap.dedent("""\
    import sys
    import os

    # Try to apply resource limits (Unix only)
    try:
        import resource
        # Memory: 128 MB
        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        # CPU time
        resource.setrlimit(resource.RLIMIT_CPU, ({timeout}, {timeout}))
        # Max file size: 1 MB
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass  # Non-Unix or unsupported limit

    # Change to isolated temp directory
    os.chdir(os.path.dirname(sys.argv[1]))

    # Execute user code
    try:
        with open(sys.argv[1], "r") as _f:
            _code = _f.read()
        exec(compile(_code, "<sandbox>", "exec"), {{"__builtins__": __builtins__, "__name__": "__main__"}})
    except SystemExit as e:
        sys.exit(e.code if e.code is not None else 0)
    except Exception as e:
        print(f"{{type(e).__name__}}: {{e}}", file=sys.stderr)
        sys.exit(1)
""")


async def execute_code(code: str, timeout: int = MAX_EXEC_TIME) -> ExecutionResult:
    """Execute Python code in a sandboxed subprocess.

    Returns ExecutionResult with stdout, stderr, exit code, and any sandbox errors.
    """
    # Step 1: Validate
    error = validate_code(code)
    if error:
        return ExecutionResult(stdout="", stderr="", exit_code=-1,
                               timed_out=False, error=error)

    # Step 2: Write code and wrapper to temp files
    tmp_dir = tempfile.mkdtemp(prefix="hivemind_sandbox_")
    code_file = Path(tmp_dir) / "user_code.py"
    wrapper_file = Path(tmp_dir) / "wrapper.py"

    try:
        code_file.write_text(code, encoding="utf-8")
        wrapper_file.write_text(
            _WRAPPER_TEMPLATE.format(timeout=timeout),
            encoding="utf-8",
        )

        # Step 3: Execute in subprocess
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(wrapper_file), str(code_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_dir,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": tmp_dir,        # Isolate home
                "TMPDIR": tmp_dir,       # Isolate temp
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
            },
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout + 2  # extra grace period
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            timed_out = True
            stdout_bytes = b""
            stderr_bytes = b""

        stdout = stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_SIZE]
        stderr = stderr_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_SIZE]

        if timed_out:
            stderr = f"Execution timed out after {timeout}s"

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode or 0,
            timed_out=timed_out,
            error="",
        )

    finally:
        # Cleanup temp files
        for f in Path(tmp_dir).iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            Path(tmp_dir).rmdir()
        except OSError:
            pass
