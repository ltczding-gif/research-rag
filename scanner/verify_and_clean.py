#!/usr/bin/env python3
"""
验证笔记与历史记录的一致性，清理幽灵记录

原理：
  - 从每篇笔记的 frontmatter 提取 pdf_0_path / pdf_1_path
  - 按组内顺序无关的稳定规则重算 combined_hash（与 gemini_analyze_pdf.py 逻辑完全一致）
  - 与 processed_history.txt 对比，找出幽灵记录（有历史无笔记）

Usage:
  python verify_and_clean.py           # 只检查，不修改
  python verify_and_clean.py --clean   # 执行清理

Paths come from `scanner/config.py` (override via env vars):
  - NOTES_DIR ← LOCALRAG_NOTES_DIR
  - HISTORY_FILE ← GEMINI_PROCESSED_HISTORY
"""

import os
import re
import sys
import argparse
from datetime import datetime

from config import VAULT_ROOT, PROCESSED_HISTORY_PATH
from _hashing import (
    stable_combined_hash as get_combined_hash,
    legacy_combined_hash as get_legacy_hash,
)

# Resolved at import time so the rest of the script reads identically.
NOTES_DIR    = str(VAULT_ROOT)
HISTORY_FILE = str(PROCESSED_HISTORY_PATH)
BACKUP_DIR   = NOTES_DIR

# 与 dedup_index 的 vault 扫描排除规则对齐：progress/ 下是 gate 备份、
# pipeline 报告、canary 笔记等派生内容，不算真正的笔记。
_EXCLUDED_TOP_DIRS = {"progress"}
_EXCLUDED_PARTS = {".obsidian", "__pycache__", ".stfolder"}


def iter_note_files(root):
    """递归收集 vault 里的 .md 笔记路径（含子目录），排除系统/派生目录。

    旧版只扫顶层目录，而 scanner 和 dedup_index 都按子目录组织/递归扫描，
    导致子目录里的笔记被当成"幽灵记录"误删对应的 ledger 条目。
    """
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        parts = [] if rel == "." else rel.split(os.sep)
        if parts and parts[0] in _EXCLUDED_TOP_DIRS:
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_PARTS and not d.startswith(".tmp")
        ]
        for fn in filenames:
            if fn.endswith(".md"):
                results.append(os.path.join(dirpath, fn))
    return results


# ---------- frontmatter 解析 ----------

def extract_pdf_paths_from_note(note_path):
    """
    从笔记 frontmatter 提取所有 pdf_N_path 字段，按 N 升序返回列表。
    例：[pdf_0_path, pdf_1_path]
    """
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"  ⚠ 读取失败 {os.path.basename(note_path)}: {e}")
        return []

    # 只在 frontmatter（首个 --- 到第二个 ---）内搜索
    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not fm_match:
        return []

    fm = fm_match.group(1)
    paths = {}
    for m in re.finditer(r'pdf_(\d+)_path:\s*(.+)', fm):
        idx  = int(m.group(1))
        path = m.group(2).strip().strip('"').strip("'")
        paths[idx] = path

    return [paths[i] for i in sorted(paths.keys())]

def extract_zotero_parent_key(note_path):
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            content = f.read()
        m = re.search(r'zotero_parent_key:\s*"?([A-Z0-9]{8})"?', content)
        return m.group(1) if m else None
    except:
        return None


# ---------- 主逻辑 ----------

def main():
    parser = argparse.ArgumentParser(description='验证笔记与历史记录一致性')
    parser.add_argument('--clean', action='store_true', help='执行清理（默认只检查）')
    args = parser.parse_args()

    print("=" * 60)
    print("📊 笔记与历史记录一致性验证")
    print("=" * 60)

    # ── 1. 读取历史 ──────────────────────────────────────────────
    print(f"\n[1/3] 读取历史记录")
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        raw_lines = [l.strip() for l in f if l.strip()]

    history_hashes  = set(raw_lines)
    duplicate_count = len(raw_lines) - len(history_hashes)
    print(f"   行数（含重复）: {len(raw_lines)}")
    print(f"   去重后        : {len(history_hashes)} 条")
    if duplicate_count:
        print(f"   重复行        : {duplicate_count} 条（--force 重跑留下的）")

    # ── 2. 扫描笔记，重算 hash ────────────────────────────────────
    print(f"\n[2/3] 扫描笔记并重算 hash: {NOTES_DIR}")

    note_hash_map   = {}   # hash -> note_filename（有效笔记）
    no_paths        = []   # 笔记文件里没有 pdf_*_path 字段
    missing_pdf     = []   # pdf 路径不存在（无法算 hash）
    missing_zpk     = []   # 缺少 zotero_parent_key

    note_paths = sorted(iter_note_files(NOTES_DIR))
    print(f"   共 {len(note_paths)} 篇 .md 文件（递归扫描，排除 progress/ 等派生目录）")

    for note_path in note_paths:
        filename  = os.path.relpath(note_path, NOTES_DIR)
        pdf_paths = extract_pdf_paths_from_note(note_path)
        zpk       = extract_zotero_parent_key(note_path)

        if not zpk:
            missing_zpk.append(filename)

        if not pdf_paths:
            no_paths.append(filename)
            continue

        # 检查所有 pdf 路径是否存在
        missing = [p for p in pdf_paths if not os.path.exists(p)]
        if missing:
            missing_pdf.append((filename, missing))
            continue

        try:
            # 同时登记 stable 和 legacy 两种 hash：2026-03 之前处理的组
            # 在 ledger 里记的是 legacy 值，只按 stable 对比会把它们全部
            # 误判成"幽灵记录"并在 --clean 时永久删除。
            h      = get_combined_hash(pdf_paths)
            legacy = get_legacy_hash(pdf_paths)
            for candidate in {h, legacy}:
                if candidate in note_hash_map and note_hash_map[candidate] != filename:
                    print(f"  ⚠ hash 重复: {filename} ↔ {note_hash_map[candidate]}")
                note_hash_map[candidate] = filename
        except Exception as e:
            print(f"  ⚠ hash 计算失败 {filename}: {e}")
            missing_pdf.append((filename, pdf_paths))

    print(f"   成功计算 hash : {len(note_hash_map)} 篇")
    print(f"   无 pdf 路径   : {len(no_paths)} 篇")
    print(f"   PDF 文件缺失  : {len(missing_pdf)} 篇")
    print(f"   缺 zotero_key : {len(missing_zpk)} 篇")

    # ── 3. 差异分析 ───────────────────────────────────────────────
    print(f"\n[3/3] 差异分析")
    print("-" * 50)

    note_hash_set = set(note_hash_map.keys())

    valid_hashes  = note_hash_set & history_hashes   # 两边都有 → 正常
    ghost_hashes  = history_hashes - note_hash_set   # 历史有笔记无 → 幽灵
    orphan_hashes = note_hash_set - history_hashes   # 笔记有历史无 → 孤儿

    print(f"✓ 正常记录  : {len(valid_hashes)} 条")
    print(f"✗ 幽灵记录  : {len(ghost_hashes)} 条  ← scanner 重跑后可补全")
    print(f"? 孤儿记录  : {len(orphan_hashes)} 条  ← 需人工判断")

    if orphan_hashes:
        print(f"\n   孤儿笔记列表（有笔记但不在历史中）:")
        for h in orphan_hashes:
            print(f"     {note_hash_map[h]}")
        print(f"   可能原因：手动复制、历史被误删、--force 后旧 hash 被清掉")

    if missing_pdf:
        print(f"\n   PDF 路径缺失的笔记（这些笔记无法重算 hash，不计入上方统计）:")
        for fname, paths in missing_pdf[:10]:
            print(f"     {fname}")
            for p in paths:
                print(f"       → {p}")
        if len(missing_pdf) > 10:
            print(f"     ... 还有 {len(missing_pdf)-10} 篇")

    # ── 4. 清理模式 ───────────────────────────────────────────────
    if not args.clean:
        print(f"\n💡 检查完成。使用 --clean 执行清理。")
        return

    if not ghost_hashes:
        print(f"\n✓ 无幽灵记录，无需清理。")
        return

    print(f"\n{'='*60}")
    print("🧹 开始清理")
    print(f"{'='*60}")

    # 备份
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"processed_history_backup_{ts}.txt")
    with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
        orig = f.read()
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(orig)
    print(f"✓ 备份: {backup_path}")

    # 重写历史：只保留有效记录（交集），同时去重
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        for h in sorted(valid_hashes):
            f.write(h + '\n')
    print(f"✓ 历史: {len(raw_lines)} 行 → {len(valid_hashes)} 条（去重+去幽灵）")
    print(f"✓ 移除幽灵: {len(ghost_hashes)} 条")

    # 保存幽灵 hash 供复查
    ghost_file = os.path.join(BACKUP_DIR, f"ghost_hashes_{ts}.txt")
    with open(ghost_file, 'w', encoding='utf-8') as f:
        for h in sorted(ghost_hashes):
            f.write(h + '\n')
    print(f"✓ 幽灵列表: {ghost_file}")

    print(f"\n{'='*60}")
    print("✅ 清理完成！下一步:")
    print(f"   1. 运行 scanner 补全 {len(ghost_hashes)} 篇缺失笔记")
    if missing_zpk:
        print(f"   2. 补填 {len(missing_zpk)} 篇无 zotero_parent_key 的笔记")
    print(f"   3. openclaw memory index --force")
    print(f"   4. python build_pdf_db.py")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
