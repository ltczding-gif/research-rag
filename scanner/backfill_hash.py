#!/usr/bin/env python3
"""
为现有笔记补填 combined_hash 字段到 YAML frontmatter。
不调用任何 API，纯本地操作。

Usage:
  python backfill_hash.py           # 预览，不修改文件
  python backfill_hash.py --write   # 实际写入

NOTES_DIR comes from `scanner/config.py` (override via $LOCALRAG_NOTES_DIR).
"""

import os
import re
import argparse
from datetime import datetime

from config import VAULT_ROOT
from _hashing import stable_combined_hash as get_combined_hash

NOTES_DIR  = str(VAULT_ROOT)
BACKUP_DIR = NOTES_DIR

# 与 verify_and_clean.py / dedup_index 的排除规则保持一致。
_EXCLUDED_TOP_DIRS = {"progress"}
_EXCLUDED_PARTS = {".obsidian", "__pycache__", ".stfolder"}


def iter_note_files(root):
    """递归收集 vault 里的 .md 笔记路径（含子目录），排除系统/派生目录。"""
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

def extract_pdf_paths(note_path):
    """提取 pdf_0_path, pdf_1_path ... 按序返回列表"""
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return [], None, str(e)

    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not fm_match:
        return [], content, "no frontmatter"

    fm = fm_match.group(1)
    paths = {}
    for m in re.finditer(r'pdf_(\d+)_path:\s*(.+)', fm):
        idx  = int(m.group(1))
        path = m.group(2).strip().strip('"').strip("'")
        paths[idx] = path

    return [paths[i] for i in sorted(paths.keys())], content, None

def already_has_hash(note_path):
    """检查 frontmatter 里是否已有 combined_hash 字段"""
    try:
        with open(note_path, 'r', encoding='utf-8') as f:
            content = f.read()
        fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if not fm_match:
            return False
        return bool(re.search(r'^combined_hash:', fm_match.group(1), re.MULTILINE))
    except:
        return False

def insert_hash_into_frontmatter(content, combined_hash):
    """
    把 combined_hash 字段插入到 frontmatter 末尾（--- 之前）。
    插在 zotero_parent_key 后面，如果没有则插在最后一个 pdf_*_path 后面。
    """
    new_line = f"combined_hash: {combined_hash}"

    # 优先插在 zotero_parent_key 后面
    if re.search(r'^zotero_parent_key:', content, re.MULTILINE):
        return re.sub(
            r'(zotero_parent_key:[^\n]*)',
            r'\1\n' + new_line,
            content,
            count=1
        )

    # 其次插在最后一个 pdf_*_path 后面
    last_pdf_path = list(re.finditer(r'(pdf_\d+_path:[^\n]*)', content))
    if last_pdf_path:
        m = last_pdf_path[-1]
        return content[:m.end()] + '\n' + new_line + content[m.end():]

    # 兜底：插在第一个 --- 结束符之前
    return re.sub(r'\n---\n', f'\n{new_line}\n---\n', content, count=1)


# ---------- 主逻辑 ----------

def main():
    parser = argparse.ArgumentParser(description='为笔记补填 combined_hash')
    parser.add_argument('--write', action='store_true', help='实际写入（默认只预览）')
    args = parser.parse_args()

    mode = "写入模式" if args.write else "预览模式（不修改文件）"
    print("=" * 60)
    print(f"🔧 补填 combined_hash  [{mode}]")
    print("=" * 60)

    md_files = sorted(iter_note_files(NOTES_DIR))
    print(f"\n共 {len(md_files)} 篇笔记（递归扫描，排除 progress/ 等派生目录）\n")

    stats = {
        'already_done': 0,
        'success': 0,
        'no_pdf_paths': 0,
        'pdf_missing': 0,
        'hash_error': 0,
    }
    failed = []

    for note_path in md_files:
        filename = os.path.relpath(note_path, NOTES_DIR)

        # 已有 hash，跳过
        if already_has_hash(note_path):
            stats['already_done'] += 1
            continue

        pdf_paths, content, err = extract_pdf_paths(note_path)

        if err or not pdf_paths:
            print(f"  ⚠ 无 pdf 路径: {filename}")
            stats['no_pdf_paths'] += 1
            failed.append((filename, 'no_pdf_paths'))
            continue

        # 检查 PDF 是否存在
        missing = [p for p in pdf_paths if not os.path.exists(p)]
        if missing:
            print(f"  ✗ PDF 缺失: {filename}")
            for p in missing:
                print(f"      {p}")
            stats['pdf_missing'] += 1
            failed.append((filename, 'pdf_missing'))
            continue

        # 计算 hash
        try:
            h = get_combined_hash(pdf_paths)
        except Exception as e:
            print(f"  ✗ hash 计算失败: {filename}  ({e})")
            stats['hash_error'] += 1
            failed.append((filename, str(e)))
            continue

        # 写入或预览
        if args.write:
            new_content = insert_hash_into_frontmatter(content, h)
            with open(note_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"  ✓ {filename}")
        else:
            print(f"  → {filename}  hash: {h[:16]}...")

        stats['success'] += 1

    # 汇总
    print(f"\n{'='*60}")
    print("📊 汇总")
    print(f"{'='*60}")
    print(f"  已有 hash（跳过）: {stats['already_done']}")
    print(f"  {'写入' if args.write else '待写入'}成功    : {stats['success']}")
    print(f"  无 pdf 路径      : {stats['no_pdf_paths']}")
    print(f"  PDF 文件缺失     : {stats['pdf_missing']}")
    print(f"  hash 计算失败    : {stats['hash_error']}")

    if not args.write and stats['success'] > 0:
        print(f"\n💡 预览完成，使用 --write 执行实际写入")

    if failed:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fail_log = os.path.join(BACKUP_DIR, f"backfill_failed_{ts}.txt")
        with open(fail_log, 'w', encoding='utf-8') as f:
            for fname, reason in failed:
                f.write(f"{reason}\t{fname}\n")
        print(f"\n  失败列表已保存: {fail_log}")


if __name__ == "__main__":
    main()
