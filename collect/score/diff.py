"""collect/score/diff.py —— ③ 产物 diff 判分（`contract/trace.md` §5.3）。

基准 = 上一次运行的 `run.json`（或基线夹具）里同一路径的 `artifacts`；本次 = 这次采集的 `artifacts`。
按路径对齐，判定只有四种：

| 判定 | 规则 |
|---|---|
| 新增 `added` | 本次 `exists:true`，基准里没有该路径或 `exists:false` |
| 删除 `removed` | 基准 `exists:true`，本次 `exists:false` 或路径消失 |
| 修改 `modified` | 两侧 `exists:true` 且 `sha256` 不同 |
| 未变 `unchanged` | 两侧 `exists:true` 且 `sha256` 相同 |

三条硬规矩：

1. **只看 `sha256`，不看 mtime**（这批文件刚写出来，mtime 全是新的，按 mtime 判会全报「修改」）。
2. **基准缺失 → 整段 `"unknown"`**，绝不报「0 新增 0 删除」（那会被读成「没变化」）。
3. 浮点容差 `|a-b| <= max(1e-9, 1e-6 * max(|a|,|b|))` 只作用于**结构里的数值**；
   文本与产物本身仍以 `sha256` 为准，不做近似文本 diff（容差不许改成近似文本比对）。
"""

from __future__ import annotations

import json
import os

from .base import UNKNOWN, is_number, is_unknown, numbers_close, tolerance_block

#: 文本 diff 的体积上限（写死常量，超了就只给 hash + bytes，并记进 notes）。
MAX_TEXT_BYTES = 2 * 1024 * 1024

STATUSES = ("added", "removed", "modified", "unchanged")

BASIS = "按产物路径对齐，只比 sha256（不看 mtime）；基准=上一次 run.json / 基线夹具（trace.md §5.3）"


def artifact_index(run_doc) -> dict:
    """`run.json` → {产物路径: {step, exists, bytes, sha256}}。"""
    steps = (run_doc.get("run") or {}).get("steps") or []
    index = {}
    for step in steps:
        for artifact in step.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            path = artifact.get("path")
            if not path:
                continue
            index[path] = {
                "step": step.get("n"),
                "exists": bool(artifact.get("exists")),
                "bytes": artifact.get("bytes"),
                "sha256": artifact.get("sha256"),
            }
    return index


def _read_text(root, relpath):
    """只读打开产物（`open(..., "r")`），拿不到就返回 `(None, 原因)`。"""
    if not root:
        return None, "没有 run.root，读不到文件内容"
    full = os.path.join(root, relpath)
    try:
        size = os.path.getsize(full)
    except OSError as exc:
        return None, "读不到（%s）" % exc.__class__.__name__
    if size > MAX_TEXT_BYTES:
        return None, "超过文本 diff 上限 MAX_TEXT_BYTES=%d，只比 sha256 + bytes" % MAX_TEXT_BYTES
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(), ""
    except OSError as exc:
        return None, "读不到（%s）" % exc.__class__.__name__


def _normalize_lines(text: str) -> list:
    """`\\n` 归一化行尾 + 忽略末尾空行（`contract/trace.md` §5.3）。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def text_diff_stat(before_text: str, after_text: str) -> dict:
    """文本文件的 `diff_stat`：`added_lines` / `removed_lines` / `hunks`。"""
    import difflib

    before = _normalize_lines(before_text)
    after = _normalize_lines(after_text)
    added = removed = hunks = 0
    for line in difflib.unified_diff(before, after, lineterm="", n=3):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {"added_lines": added, "removed_lines": removed, "hunks": hunks}


def _shallow_equal(a, b) -> bool:
    """递归比较两个 JSON 结构：数值走固定容差，别的必须严格相等。"""
    if is_number(a) and is_number(b):
        return numbers_close(a, b)
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return False
        return all(_shallow_equal(a[key], b[key]) for key in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_shallow_equal(x, y) for x, y in zip(a, b))
    return a == b


def numeric_equal_within_tol(before_text, after_text):
    """两侧都是 JSON 时，判断「差异是否只在容差内的数值抖动」。

    只作为**旁注**：状态仍按 `sha256` 判（文本比对上以 hash 为准）。
    """
    try:
        return _shallow_equal(json.loads(before_text), json.loads(after_text))
    except (ValueError, TypeError):
        return None


def _blank_summary() -> dict:
    """基准缺失时的汇总：全 `"unknown"`，**不许写 0**。"""
    return {status: UNKNOWN for status in STATUSES}


def _empty_section(reason: str, baseline_meta) -> dict:
    return {
        "basis": BASIS,
        "status": "unknown",
        "reason": reason,
        "baseline": baseline_meta,
        "summary": _blank_summary(),
        "items": [],
        "uncomparable": [],
        "tolerance": tolerance_block(),
    }


def compare(current_doc, baseline_doc=None, baseline_meta=None) -> dict:
    """本次 vs 基准的产物 diff 段。"""
    if baseline_doc is None:
        return _empty_section(
            "baseline.missing 没有基准（第一次跑）→ 整段 unknown；不报「0 新增 0 删除」（那会被读成没变化）",
            baseline_meta,
        )

    current = artifact_index(current_doc)
    baseline = artifact_index(baseline_doc)
    current_root = (current_doc.get("run") or {}).get("root")
    baseline_root = (baseline_doc.get("run") or {}).get("root")

    summary = {status: 0 for status in STATUSES}
    items, uncomparable = [], []
    for path in sorted(set(current) | set(baseline)):
        after = current.get(path)
        before = baseline.get(path)
        item = {
            "path": path,
            "step": (after or before or {}).get("step"),
            "before_sha256": (before or {}).get("sha256") if before else None,
            "after_sha256": (after or {}).get("sha256") if after else None,
            "bytes_before": (before or {}).get("bytes") if before else None,
            "bytes_after": (after or {}).get("bytes") if after else None,
        }
        notes = []

        before_exists = bool(before and before.get("exists"))
        after_exists = bool(after and after.get("exists"))

        if after_exists and not before_exists:
            status = "added"
        elif before_exists and not after_exists:
            status = "removed"
        elif before_exists and after_exists:
            before_sha, after_sha = before.get("sha256"), after.get("sha256")
            if is_unknown(before_sha) or is_unknown(after_sha) or not before_sha or not after_sha:
                status = "unknown"
                notes.append("产物读不到：sha256 是 \"unknown\"，这一条不参与判定（trace.md §5.3）")
            elif before_sha == after_sha:
                status = "unchanged"
            else:
                status = "modified"
        else:
            status = "unknown"
            notes.append("两侧都 exists:false（路径被声明但没产物），本条无可比内容")
            uncomparable.append({"path": path, "reason": "两侧都不存在"})

        if is_number(item["bytes_before"]) and is_number(item["bytes_after"]):
            item["bytes_delta"] = item["bytes_after"] - item["bytes_before"]

        if status == "modified":
            before_text, before_note = _read_text(baseline_root, path)
            after_text, after_note = _read_text(current_root, path)
            if before_text is not None and after_text is not None:
                item["diff_stat"] = text_diff_stat(before_text, after_text)
                equal_within_tol = numeric_equal_within_tol(before_text, after_text)
                if equal_within_tol is not None:
                    item["numeric_equal_within_tol"] = equal_within_tol
                    if equal_within_tol:
                        notes.append("两侧都是 JSON，差异只在浮点容差内；状态仍按 sha256 记 modified（不做近似文本 diff）")
            else:
                item["diff_stat"] = None
                notes.append("不做文本 diff：%s%s" % (before_note or "", (
                    "；" + after_note if after_note else "")))

        if is_unknown(item["before_sha256"]) or is_unknown(item["after_sha256"]):
            uncomparable.append({"path": path, "reason": "sha256:unknown 产物读不到，这一条不参与判定"})

        item["status"] = status
        if notes:
            item["notes"] = notes
        if status in summary:
            summary[status] += 1
        items.append(item)

    return {
        "basis": BASIS,
        "status": "compared",
        "reason": "基准可读，按路径逐条比 sha256",
        "baseline": baseline_meta,
        "summary": summary,
        "items": items,
        "uncomparable": uncomparable,
        "tolerance": tolerance_block(),
    }
