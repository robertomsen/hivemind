#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# Hivemind Installer — macOS / Linux
#
# Creates an isolated environment at ~/.hivemind-env/ and links the
# `hivemind` command into your PATH. No source code is exposed.
#
# Usage:
#   ./install.sh              Install or upgrade Hivemind
#   ./install.sh --uninstall  Remove Hivemind completely
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

INSTALL_DIR="$HOME/.hivemind-env"
BIN_DIR="/usr/local/bin"
BIN_NAME="hivemind"
MIN_PYTHON="3.12"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Colors ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { echo -e "  ${CYAN}▸${RESET} $1"; }
ok()    { echo -e "  ${GREEN}✓${RESET} $1"; }
err()   { echo -e "  ${RED}✗${RESET} $1" >&2; }
dim()   { echo -e "  ${DIM}$1${RESET}"; }

# ── Banner ────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}${CYAN}⚡ Hivemind Installer${RESET}"
echo ""

# ── Uninstall ─────────────────────────────────────────────────────────
if [ "${1:-}" = "--uninstall" ]; then
    info "Uninstalling Hivemind..."

    # Remove from /usr/local/bin
    if [ -L "$BIN_DIR/$BIN_NAME" ] || [ -f "$BIN_DIR/$BIN_NAME" ]; then
        if [ -w "$BIN_DIR/$BIN_NAME" ]; then
            rm -f "$BIN_DIR/$BIN_NAME"
        elif sudo -n true 2>/dev/null; then
            sudo rm -f "$BIN_DIR/$BIN_NAME"
        fi
        ok "Removed $BIN_DIR/$BIN_NAME"
    fi

    # Remove from ~/.local/bin
    LOCAL_BIN="$HOME/.local/bin/$BIN_NAME"
    if [ -f "$LOCAL_BIN" ]; then
        rm -f "$LOCAL_BIN"
        ok "Removed $LOCAL_BIN"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
        ok "Removed $INSTALL_DIR"
    fi

    echo ""
    ok "Hivemind uninstalled."
    dim "Config files remain at ~/.hivemind/ (delete manually if desired)"
    echo ""
    exit 0
fi

# ── Check Python ──────────────────────────────────────────────────────
find_python() {
    for cmd in python3.14 python3.13 python3.12 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            if python3 -c "exit(0 if tuple(map(int, '$ver'.split('.'))) >= tuple(map(int, '$MIN_PYTHON'.split('.'))) else 1)" 2>/dev/null; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(find_python) || {
    err "Python $MIN_PYTHON+ is required but not found."
    dim "Install from: https://www.python.org/downloads/"
    echo ""
    exit 1
}

PYTHON_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Found Python $PYTHON_VER ($PYTHON)"

# ── Find wheel or source ─────────────────────────────────────────────
WHL=$(ls "$SCRIPT_DIR"/dist/hivemind_ai-*.whl 2>/dev/null | head -1 || true)

if [ -n "$WHL" ]; then
    INSTALL_SRC="$WHL"
    info "Installing from wheel: $(basename "$WHL")"
elif [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    INSTALL_SRC="$SCRIPT_DIR"
    info "Installing from source directory"
else
    err "No wheel found in dist/ and no pyproject.toml found."
    dim "Run ./build.sh first, or run from the project directory."
    echo ""
    exit 1
fi

# ── Create isolated environment ──────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
    info "Upgrading existing installation..."
else
    info "Creating isolated environment at $INSTALL_DIR..."
fi

"$PYTHON" -m venv "$INSTALL_DIR" --clear
ok "Virtual environment ready"

# ── Install package ──────────────────────────────────────────────────
info "Installing Hivemind and dependencies..."
"$INSTALL_DIR/bin/pip" install --upgrade pip setuptools -q 2>/dev/null
"$INSTALL_DIR/bin/pip" install "$INSTALL_SRC" -q 2>/dev/null

# Verify the hivemind entry point exists
if [ ! -f "$INSTALL_DIR/bin/hivemind" ]; then
    err "Installation failed: hivemind binary not found"
    exit 1
fi

VERSION=$("$INSTALL_DIR/bin/python3" -c "import hivemind; print(hivemind.__version__)" 2>/dev/null || echo "unknown")
ok "Hivemind v$VERSION installed"

# ── Compile to bytecode & remove .py source ──────────────────────────
info "Compiling to bytecode (removing source)..."
# Find the installed package in the venv's site-packages directly
HIVEMIND_PKG=$(find "$INSTALL_DIR/lib" -type d -name "hivemind" -path "*/site-packages/hivemind" | head -1)
if [ -n "$HIVEMIND_PKG" ] && [ -d "$HIVEMIND_PKG" ]; then
    # -b flag: write .pyc files next to .py (not in __pycache__)
    # This lets Python import them after .py removal
    "$INSTALL_DIR/bin/python3" -m compileall -b -q "$HIVEMIND_PKG/" 2>/dev/null
    # Remove .py source files — the adjacent .pyc files are importable
    find "$HIVEMIND_PKG" -maxdepth 1 -name "*.py" -type f -delete
    # Clean __pycache__ (redundant now)
    rm -rf "$HIVEMIND_PKG/__pycache__"
    ok "Source code removed — only bytecode remains"
fi

# ── Link to PATH ─────────────────────────────────────────────────────
write_wrapper() {
    local target="$1"
    cat > "$target" << 'WRAPPER_EOF'
#!/usr/bin/env bash
exec "$HOME/.hivemind-env/bin/hivemind" "$@"
WRAPPER_EOF
    chmod +x "$target"
}

LINKED=false

# Try /usr/local/bin first (global, may need sudo)
if [ -w "$BIN_DIR" ]; then
    info "Linking hivemind to $BIN_DIR/$BIN_NAME..."
    write_wrapper "$BIN_DIR/$BIN_NAME"
    LINKED=true
elif sudo -n true 2>/dev/null; then
    info "Linking hivemind to $BIN_DIR/$BIN_NAME..."
    sudo bash -c "$(declare -f write_wrapper); write_wrapper '$BIN_DIR/$BIN_NAME'"
    LINKED=true
fi

# Fallback: ~/.local/bin (no sudo needed)
if [ "$LINKED" = false ]; then
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
    info "Linking hivemind to $BIN_DIR/$BIN_NAME..."
    write_wrapper "$BIN_DIR/$BIN_NAME"
    LINKED=true

    # Ensure ~/.local/bin is in PATH
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
        SHELL_NAME=$(basename "$SHELL")
        case "$SHELL_NAME" in
            zsh)  RC="$HOME/.zshrc" ;;
            bash) RC="$HOME/.bashrc" ;;
            *)    RC="$HOME/.profile" ;;
        esac
        if ! grep -q 'hivemind-env' "$RC" 2>/dev/null; then
            echo "" >> "$RC"
            echo '# Hivemind' >> "$RC"
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
            dim "Added ~/.local/bin to PATH in $(basename "$RC")"
            dim "Run: source $RC  (or restart your terminal)"
        fi
    fi
fi

ok "Command 'hivemind' available at $BIN_DIR/$BIN_NAME"

# ── Done ──────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}${GREEN}⚡ Hivemind installed successfully!${RESET}"
echo ""
dim "Run:        hivemind"
dim "Demo mode:  hivemind --demo"
dim "Uninstall:  $SCRIPT_DIR/install.sh --uninstall"
echo ""
