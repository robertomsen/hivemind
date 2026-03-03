#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# Hivemind Build Script
# Packages Hivemind into a distributable .whl file
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=$(python3 -c "import ast; f=open('$SCRIPT_DIR/hivemind/__init__.py'); t=f.read(); f.close(); print([s.value.value for s in ast.parse(t).body if isinstance(s, ast.Assign) and any(t.id=='__version__' for t in s.targets)][0])")

echo "╔══════════════════════════════════════╗"
echo "║  Hivemind Build — v${VERSION}               ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Clean previous builds
rm -rf "$SCRIPT_DIR/dist/" "$SCRIPT_DIR/build/" "$SCRIPT_DIR"/*.egg-info

# ── Build wheel ──────────────────────────────────────────────────────
echo "  [1/2] Building wheel..."
cd "$SCRIPT_DIR"
python3 -m pip wheel . --no-deps --wheel-dir "$SCRIPT_DIR/dist/" -q

# ── Done ─────────────────────────────────────────────────────────────
echo "  [2/2] Done."

WHL=$(ls "$SCRIPT_DIR/dist/"*.whl 2>/dev/null | head -1)
if [ -n "$WHL" ]; then
    SIZE=$(du -h "$WHL" | cut -f1 | xargs)
    echo ""
    echo "  ✓ Built: dist/$(basename "$WHL") (${SIZE})"
    echo ""
    echo "  To install:"
    echo "    ./install.sh"
    echo ""
    echo "  To distribute (no source code exposed):"
    echo "    Send dist/$(basename "$WHL") + install.sh + install.cmd"
else
    echo ""
    echo "  ✗ Build failed."
    exit 1
fi
