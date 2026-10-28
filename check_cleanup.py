# -*- coding: utf-8 -*-
"""项目文件体检：引用关系、体积、最后修改时间"""
import sys, io, re, ast, pathlib, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = pathlib.Path(".")
py_files = sorted(ROOT.glob("*.py"))

# 1) 每个 py 的 import 关系
imports = {}
for f in py_files:
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        imports[f.name] = set()
        continue
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    imports[f.name] = mods

local = {f.stem for f in py_files}
print("=== 被谁引用 ===")
for f in py_files:
    used_by = [name for name, mods in imports.items() if f.stem in mods and name != f.name]
    marker = "核心" if used_by else ("入口" if not (imports[f.name] & local) else "孤立")
    print(f"  {f.name:<24} <- {', '.join(used_by) if used_by else '（无人引用）'}")

print("\n=== 各文件大小 / 修改时间 ===")
for f in py_files:
    st = f.stat()
    print(f"  {f.name:<24} {st.st_size/1024:6.1f}KB  {time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))}")

print("\n=== 目录体积 ===")
for d in ["probe", "captcha_assets", "results", "logs", "__pycache__"]:
    p = ROOT / d
    if not p.exists():
        print(f"  {d:<16} 不存在")
        continue
    files = list(p.rglob("*"))
    size = sum(x.stat().st_size for x in files if x.is_file())
    print(f"  {d:<16} {len([x for x in files if x.is_file()]):>4} 个文件  {size/1024/1024:6.2f}MB")

print("\n=== 其他文件 ===")
for f in sorted(ROOT.iterdir()):
    if f.is_file() and f.suffix not in (".py",):
        print(f"  {f.name:<40} {f.stat().st_size/1024:7.1f}KB")
