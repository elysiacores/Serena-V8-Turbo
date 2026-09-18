# Serena V8 — Next-Generation Semantic Coding Runtime

> Drop-in replacement for [Serena](https://github.com/oraios/serena) — ติดตั้งครั้งเดียว ใช้ได้ทันที ไม่ต้องติดตั้ง Serena ก่อน

---

## สิ่งที่ V8 ทำให้ (vs Serena ต้นฉบับ)

| ความสามารถ | Serena ต้นฉบับ | V8 |
|------------|----------------|-----|
| ค้นหาสัญลักษณ์ (find_symbol) | ~2.5s ทุกครั้ง | ~0.005s หลังครั้งแรก |
| แคชผลการค้นหา | ❌ ไม่มี | ✅ TTL 30นาที, 500 รายการ |
| กู้คืน LSP ที่พัง | ❌ ต้อง restart เอง | ✅ รีสตาร์ทอัตโนมัติ < 3วินาที |
| จัดการหน่วยความจำ LSP | ❌ ไม่จำกัด | ✅ พัก LSP ที่ไม่ใช้ + เตือนเมื่อเกิน |
| ติดตามประสิทธิภาพ | ❌ ไม่มี | ✅ P50/P95/P99 + แบ่ง stage |
| ดัชนีสัญลักษณ์ถาวร | ❌ ไม่มี | ✅ SQLite เก็บข้ามการ restart |
| รวมคำสั่ง (composite tools) | ❌ ไม่มี | ✅ ใช้ 1 คำสั่งแทน 3-4 คำสั่ง |

---

## วิธีติดตั้ง

### วิธี 1: pip (ทั่วไป)
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
```

### วิธี 2: uv (เร็วกว่า)
```bash
uv pip install git+https://github.com/elysiacores/serena-v8.git
```

### วิธี 3: ติดตั้งแบบ dev (แก้โค้ดเองได้)
```bash
git clone https://github.com/elysiacores/serena-v8.git
cd serena-v8
pip install -e .
```

> **สำคัญ:** ❌ **ไม่ต้องติดตั้ง Serena ก่อน** — V8 มีทุกอย่างครบ (รวม solidlsp, interprompt)  
> ❌ **ไม่ต้องตั้งค่าอะไรเพิ่ม** — ใช้คำสั่งเดียวกับ Serena ทุกอย่าง

---

## วิธีใช้งาน

### คำสั่งเดียวกับ Serena (ใช้แทน serena ได้เลย)
```bash
# รัน MCP server
serena start-mcp-server --project /path/to/project

# ใช้กับ tunnel-client (ใน config)
command: "serena start-mcp-server --project /path/to/project"

# ดูเวอร์ชัน
serena --version
# → 8.0.0-dev.1

# ดูสถานะ V8
serena-v8 status

# ดูสถิติ
cat ~/.serena-v8/stats.json
```

### ตัวอย่างสถิติที่ปรากฏ
```json
{
  "timestamp": 1789725324.638,
  "projects": {
    "/home/user/project": {
      "cache_entries": 42,
      "requests": 128,
      "p50_ms": 230.0,
      "p95_ms": 445.38,
      "errors": 0
    }
  }
}
```

---

## สถาปัตยากร V8 มีอะไรบ้าง

```
ChatGPT / Hermes / Claude
        │
        ▼
┌─────────────────┐
│ MCP Tunnel      │  ← มีอยู่แล้ว ไม่ต้องแก้
│ (tunnel-client) │
└────────┬────────┘
         │ stdio
         ▼
┌─────────────────┐
│ V8 Core Daemon  │  ← V8 เพิ่ม (รันค้างไว้ รีใช้ LSP เดิม)
│ - Project state │
│ - Symbol index  │
│ - Cache (L1→L2) │
│ - LSP lifecycle │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌───────┐ ┌───────────┐
│ LSP   │ │ SQLite    │
│ Pool  │ │ Index     │
└───────┘ └───────────┘
```

---

## ไฟล์ config ของ V8

| ไฟล์ | วัตถุประสงค์ |
|------|------------|
| `~/.serena-v8/stats.json` | สถิติรันไทม์ (เขียนทุก 10 วินาที) |
| `~/.serena-v8/symbol_index.db` | ดัชนีสัญลักษณ์ถาวร (SQLite) |
| `~/.serena-v8/cache.db` | แคชผลการค้นหา (SQLite) |
| `~/.serena-v8/daemon.sock` | V8 Core Daemon (unix socket) |

---

## การย้ายจาก Serena มา V8

### กรณีมี Serena อยู่แล้ว
```bash
# 1. ถอน Serena เดิม (ถ้าต้องการ)
pip uninstall serena-agent

# 2. ติดตั้ง V8
pip install git+https://github.com/elysiacores/serena-v8.git

# 3. ตรวจสอบ
serena --version
# → 8.0.0-dev.1 ← สำเร็จ!

# 4. tunnel config เดิม ใช้ได้เลย ไม่ต้องแก้
```

### กรณีเริ่มใหม่
```bash
pip install git+https://github.com/elysiacores/serena-v8.git
# ใช้ได้ทันที ไม่ต้องตั้งค่า
```

---

## การทดสอบ

```bash
# ทดสอบว่า V8 ทำงาน
python3 -c "
import serena
print(f'Version: {serena.__version__}')
print(f'V8 Identity: {serena.get_v8_identity()}')
"

# ทดสอบแคช
python3 -c "
from serena.symbol import _v8_symbol_cache
_v8_symbol_cache.put('test', ['symbol1'])
print(f'Cache hit: {_v8_symbol_cache.get(\"test\")}')
print(f'Stats: {_v8_symbol_cache.stats()}')
"

# ทดสอบ LSP Manager
python3 -c "
from serena_v8.lsp_manager import LSPLifecycleManager
mgr = LSPLifecycleManager()
mgr.register('/proj', 'go', ['gopls', 'serve'])
print(mgr.stats())
"

# ทดสอบ Multi-Tier Cache
python3 -c "
from serena_v8.cache import MultiTierCache
cache = MultiTierCache()
cache.put('/proj', 'find_symbol', {'name': 'App'}, {'result': 'ok'})
result, tier = cache.get('/proj', 'find_symbol', {'name': 'App'})
print(f'Tier: {tier}, Result: {result}')
"
```

---

## การแก้ปัญหา

### ปัญหา: `serena --version` ยังเป็น 1.7.0
แก้: รัน `pip install git+https://github.com/elysiacores/serena-v8.git` อีกครั้ง

### ปัญหา: `ModuleNotFoundError: serena_v8`
แก้: V8 runtime ไม่ได้โหลด ลอง:
```bash
pip install -e ~/SuperProjects/serena-v8-fork
```

### ปัญหา: tunnel config ไม่เปลี่ยน
แก้: แก้ไฟล์ config ของ tunnel-client ชี้มา `serena` (จาก V8)
```yaml
mcp:
  commands:
    - command: "serena start-mcp-server --project /path"
```

---

## License

MIT (เข้ากันได้กับ Serena เวอร์ชั่น MIT)
