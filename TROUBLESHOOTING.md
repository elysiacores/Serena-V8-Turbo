# Serena V8 — Installation & Troubleshooting Guide

> If you already have Serena installed, follow these steps to migrate to V8 without breaking anything.

---

## Table of Contents

1. [Quick Check: What version am I running?](#1-quick-check)
2. [Migrating from Serena to V8](#2-migrating)
3. [Tunnel Configuration](#3-tunnel-config)
4. [Common Problems & Fixes](#4-common-problems)
5. [Diagnostic Commands](#5-diagnostic-commands)
6. [Rollback Plan](#6-rollback)
7. [FAQ](#7-faq)

---

## 1. Quick Check: What version am I running?

Run this command:

```bash
python3 -c "import serena; print(serena.__version__)"
```

| Output | Meaning | Action |
|--------|---------|--------|
| `1.7.0` | Serena original | Migrate to V8 |
| `8.0.0-dev.1` | V8 installed but not loaded | Check Python path |
| `8.0.0-dev.1` + `get_v8_identity` works | V8 active | No action needed |
| `ModuleNotFoundError` | Not installed | Install V8 |

### Check V8 runtime loaded:
```bash
python3 -c "
import serena
print(f'Version: {serena.__version__}')
print(f'V8: {hasattr(serena, \"get_v8_identity\")}')
if hasattr(serena, 'get_v8_identity'):
    import json
    print(json.dumps(serena.get_v8_identity(), indent=2))
"
```

Expected output when V8 is active:
```
Version: 8.0.0-dev.1
V8: True
{
  "name": "Serena V8",
  "version": "8.0.0-dev.1",
  "build": "2026-09-18",
  "commit": "v8-phase1",
  ...
}
```

---

## 2. Migrating from Serena to V8

### Step 1: Backup your current Serena
```bash
# Find where Serena is installed
python3 -c "import serena; print(serena.__file__)"

# Backup (copy the entire package directory)
SERENA_PATH=$(python3 -c "import serena, os; print(os.path.dirname(serena.__file__))")
cp -r "$SERENA_PATH" "$SERENA_PATH.backup_$(date +%Y%m%d)"
```

### Step 2: Install V8
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
```

### Step 3: Verify installation
```bash
python3 -c "import serena; print(f'V8: {serena.get_v8_identity()}')"
```

### Step 4: Check tunnel configs still work
```bash
# Test with a project
serena start-mcp-server --project /path/to/your/project --tool-timeout 100 --log-level INFO
# Press Ctrl+C after it starts successfully
```

### What happens to my old Serena?
- The old Serena package is **overwritten** by V8
- Your old config files remain unchanged
- Your tunnel profiles remain compatible
- Your `.serena/` directory is preserved

---

## 3. Tunnel Configuration

### If you use tunnel-client with Serena:

Before (Serena 1.7):
```yaml
mcp:
  commands:
    - command: "/home/user/.local/bin/serena start-mcp-server --project /path"
```

After (Serena V8):
```yaml
# SAME COMMAND — V8 is a drop-in replacement
mcp:
  commands:
    - command: "/home/user/.local/bin/serena start-mcp-server --project /path"
```

### To use V8 with uv:
```yaml
mcp:
  commands:
    - command: "/home/user/.local/share/uv/tools/serena-agent/bin/serena start-mcp-server --project /path"
```

### Verify tunnel is using V8:
```bash
# Check tunnel process
ps aux | grep 'tunnel-client\|serena' | grep -v grep

# Check tunnel logs
journalctl --user -u workspace-b-tunnel.service -n 50 --no-pager | grep -i "v8\|version"
```

---

## 4. Common Problems & Fixes

### Problem: `serena --version` still shows 1.7.0

**Cause:** Python is importing the wrong serena package.

**Fix:**
```bash
# Check which serena is being used
which serena
python3 -c "import serena; print(serena.__file__)"

# Reinstall V8
pip uninstall serena-agent -y
pip install git+https://github.com/elysiacores/serena-v8.git

# If using uv
uv pip uninstall serena-agent
uv pip install git+https://github.com/elysiacores/serena-v8.git
```

### Problem: V8 runtime not loaded (stats are empty)

**Cause:** V8 runtime module not in Python path.

**Fix:**
```bash
# Check if v8_runtime exists
python3 -c "from serena.v8_runtime import get_v8_identity; print('OK')"

# If it fails, reinstall from source
git clone https://github.com/elysiacores/serena-v8.git
cd serena-v8
pip install -e .
```

### Problem: V8 installed but tunnel stats show 0 hits

**Cause:** Tunnel is running old serena process.

**Fix:**
```bash
# Kill old tunnel processes
pkill -f 'tunnel-client' 2>/dev/null
pkill -f 'serena start-mcp' 2>/dev/null

# Restart tunnels
systemctl --user restart workspace-a-tunnel.service
systemctl --user restart workspace-b-tunnel.service
# ... restart all your tunnel services

# Verify V8 is running
cat ~/.serena-v8/stats.json
```

### Problem: ModuleNotFoundError: serena_v8

**Cause:** V8 package not included in build.

**Fix:**
```bash
# Reinstall with V8 extras
pip install git+https://github.com/elysiacores/serena-v8.git
```

### Problem: Stats file exists but always shows 0 entries

**Cause:** V8 runtime loads but requests aren't being tracked.

**Fix:**
```bash
# Test V8 runtime directly
python3 -c "
import time
from serena.v8_runtime import v8_measure, _v8_telemetry

with v8_measure('test_tool', project='/tmp') as stage:
    stage('queue')
    time.sleep(0.01)
    stage('cache')
    time.sleep(0.05)
    stage('lsp')
    stage('serialize')

print(f'Telemetry: {_v8_telemetry.stats()}')
"

# If this works but tunnel doesn't — the tunnel is running old Serena
```

---

## 5. Diagnostic Commands

### Full diagnostic:
```bash
python3 << 'EOF'
import sys
import os
import json

print("=== V8 Diagnostic ===")

# 1. Python path
print(f"\n1. Python: {sys.executable}")
print(f"   Path: {sys.path[:3]}")

# 2. Serena location
try:
    import serena
    print(f"\n2. Serena: {serena.__file__}")
    print(f"   Version: {serena.__version__}")
except ImportError:
    print("\n2. Serena: NOT INSTALLED")
    sys.exit(1)

# 3. V8 runtime
v8_loaded = hasattr(serena, 'get_v8_identity')
print(f"\n3. V8 Runtime: {'LOADED' if v8_loaded else 'NOT LOADED'}")
if v8_loaded:
    identity = serena.get_v8_identity()
    print(f"   Version: {identity.get('version')}")
    print(f"   Commit: {identity.get('commit')}")
    print(f"   Uptime: {identity.get('uptime_seconds')}s")

# 4. V8 cache
try:
    from serena.symbol import _v8_symbol_cache
    print(f"\n4. Symbol Cache: {_v8_symbol_cache.stats()}")
except Exception as e:
    print(f"\n4. Symbol Cache: ERROR - {e}")

# 5. V8 telemetry
try:
    from serena.v8_runtime import _v8_telemetry
    print(f"\n5. Telemetry: {_v8_telemetry.stats()}")
except Exception as e:
    print(f"\n5. Telemetry: ERROR - {e}")

# 6. V8 stats file
stats_file = os.path.expanduser("~/.serena-v8/stats.json")
if os.path.exists(stats_file):
    with open(stats_file) as f:
        d = json.load(f)
    print(f"\n6. Stats file: {json.dumps(d, indent=2)[:500]}")
else:
    print(f"\n6. Stats file: NOT FOUND at {stats_file}")

# 7. Serena processes
import subprocess
result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
serena_procs = [l for l in result.stdout.split('\n') if 'serena' in l.lower() and 'grep' not in l.lower()]
print(f"\n7. Serena processes: {len(serena_procs)}")
for p in serena_procs[:3]:
    parts = p.split()
    if len(parts) >= 11:
        print(f"   PID={parts[1]} CPU={parts[2]}% MEM={parts[3]}% CMD={' '.join(parts[10:60])}")

print("\n=== End Diagnostic ===")
EOF
```

### Quick check:
```bash
# One-liner status
python3 -c "
import serena, json
d = {'version': serena.__version__, 'v8': hasattr(serena, 'get_v8_identity')}
if d['v8']:
    d['identity'] = serena.get_v8_identity()
    from serena.v8_runtime import _v8_telemetry, get_memory_info
    d['telemetry'] = _v8_telemetry.stats()
    d['memory'] = get_memory_info()
print(json.dumps(d, indent=2))
"
```

---

## 6. Rollback Plan

If V8 causes issues and you need to go back:

```bash
# Step 1: Uninstall V8
pip uninstall serena-agent -y

# Step 2: Reinstall original Serena
pip install serena-agent

# Step 3: Restore backup (if needed)
SERENA_PATH=$(python3 -c "import serena, os; print(os.path.dirname(serena.__file__))")
cp -r "$SERENA_PATH.backup_$(date +%Y%m%d)/serena"/* "$SERENA_PATH/serena/"

# Step 4: Restart tunnels
systemctl --user restart workspace-b-tunnel.service workspace-a-tunnel.service

# Step 5: Verify
python3 -c "import serena; print(serena.__version__)"
# Should show 1.7.0
```

---

## 7. FAQ

### Q: Do I need to uninstall Serena first?
A: No. V8 overwrites Serena. Uninstalling first is optional but recommended for clean migration.

### Q: Will my tunnel configs work unchanged?
A: Yes. V8 is a drop-in replacement. Same command, same arguments.

### Q: Where does V8 store its data?
A: 
- `~/.serena-v8/stats.json` — runtime statistics
- `~/.serena-v8/symbol_index.db` — persistent symbol index
- `~/.serena-v8/cache.db` — search result cache

### Q: How do I know V8 is actually running?
A: Run:
```bash
python3 -c "import serena; print('V8' if hasattr(serena, 'get_v8_identity') else 'Original Serena')"
```

### Q: Does V8 work with Claude/Cursor/Hermes?
A: Yes. Any MCP client that works with Serena will work with V8.

### Q: How do I see what's slow?
A: Check V8 stats:
```bash
cat ~/.serena-v8/stats.json | python3 -m json.tool
```

### Q: V8 shows version 8.0.0-dev.1 but stats are empty?
A: The V8 runtime is loading but requests aren't passing through it. Restart your tunnels:
```bash
pkill -f 'serena start-mcp'
systemctl --user restart <your-tunnel-service>
```

### Q: Can I run V8 and Serena side-by-side?
A: Not recommended — they share the same package name. Use separate virtualenvs if needed.

---

## Quick Reference

| Task | Command |
|------|---------|
| Check version | `python3 -c "import serena; print(serena.__version__)"` |
| Check V8 loaded | `python3 -c "import serena; print(hasattr(serena, 'get_v8_identity'))"` |
| View stats | `cat ~/.serena-v8/stats.json` |
| Restart tunnels | `systemctl --user restart <tunnel-service>` |
| Kill old processes | `pkill -f 'serena start-mcp'` |
| Run diagnostic | See [Diagnostic Commands](#5-diagnostic-commands) |
| Rollback | See [Rollback Plan](#6-rollback) |

---

## For AI Agents: Common Error Patterns

### Pattern: `serena.__version__ == "1.7.0"` after V8 install
**Root cause:** Python path order — original serena found before V8
**Fix:** Uninstall original serena first, then install V8

### Pattern: V8 installed but stats.json has 0 entries
**Root cause:** Tunnel running stale process from before V8 install
**Fix:** Kill all `serena start-mcp` processes and restart tunnels

### Pattern: `ModuleNotFoundError: serena_v8`
**Root cause:** `serena_v8` package not in build targets
**Fix:** Reinstall from latest GitHub (fixed in commit `4ed9ad0`)

### Pattern: V8 runtime import fails with `ModuleNotFoundError: solidlsp`
**Root cause:** `solidlsp` not bundled in V8
**Fix:** Reinstall from latest GitHub (fixed in commit `26c9635`)

### Pattern: Stats file exists but timestamp doesn't update
**Root cause:** V8 runtime not loaded — stats file is from previous test
**Fix:** Verify V8 runtime is loaded, restart tunnels
