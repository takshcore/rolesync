"""
RoleSync syntax & structure checker — validates all 7 modules + main orchestrator.
"""
import ast
import json
import pathlib
import sys

FILES = [
    "f:/agent/desktop_app.py",
    "f:/agent/hotkey_ingestor.py",
    "f:/agent/rag_engine.py",
    "f:/agent/llm_reasoner.py",
    "f:/agent/doc_compiler.py",
    "f:/agent/auto_submit.py",
    "f:/agent/lifecycle_watchdog.py",
    "f:/agent/main.py",
    "f:/agent/master_portfolio.json",
    "f:/agent/profile.json",
]

print("=" * 80)
print(f"{'FILE':<32} {'STATUS':<8} {'LINES':<8} {'SIZE':<12}")
print("=" * 80)

all_ok = True
total_lines = 0

for path in FILES:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"{p.name:<32} {'MISSING':<8}")
        all_ok = False
        continue

    size = p.stat().st_size
    lines = len(p.read_bytes().splitlines())
    total_lines += lines

    if path.endswith(".py"):
        try:
            ast.parse(p.read_text(encoding="utf-8"))
            print(f"{p.name:<32} {'VALID':<8} {lines:<8} {f'{size:,} B':<12}")
        except SyntaxError as e:
            print(f"{p.name:<32} {'ERROR':<8} {lines:<8} Line {e.lineno}: {e.msg}")
            all_ok = False
    elif path.endswith(".json"):
        try:
            json.loads(p.read_text(encoding="utf-8"))
            print(f"{p.name:<32} {'VALID':<8} {lines:<8} {f'{size:,} B':<12}")
        except Exception as e:
            print(f"{p.name:<32} {'ERROR':<8} {lines:<8} {e}")
            all_ok = False

print("=" * 80)
print(f"Total Lines of Pure Python & Config: {total_lines:,}")
print("Result: " + ("ALL MODULES FULLY VALIDATED ✓" if all_ok else "ERRORS DETECTED ✗"))
print("=" * 80)

sys.exit(0 if all_ok else 1)
