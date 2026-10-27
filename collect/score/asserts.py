"""collect/score/asserts.py —— ② 断言通过率判分（`contract/trace.md` §5.2）。

口径（写死，不许临时改）：

- **分母** = 该步 `plan.json` 的 `steps[].accept` 条数（声明口径，不随执行结果变化），
  采集后即为 `run.json` 的 `asserts.total`。
- **分子** = `result == "pass"` 的条数。
- `ack:` 未确认 → `result: "unacked"`：**计入分母、不计入分子**（不许因为要人工就剔出去，那样是虚高）。
- `cmd:` 超时 → `result: "timeout"`：计入分母、不计入分子，另计 `asserts.timeout`，与 `fail` 分开报。
- 通过率**不做阈值判定**：「≥90% 就算过」这种字段这里一个都不许有（通过率是观测值）。
- 两个比率都要报：`first_pass_rate`（第一次判定就过的比例）与 `final_pass_rate`（收尾时的比例）。
- 返工 `rework` / `first_failed` 按 §5.2 由首次与后续判定结果导出，口径落在 `run.json`。

分母是 `null`（没有断言）/ `0` 时比率是 `null`，分母是 `"unknown"` 时比率是 `"unknown"`，
**任何情况下不许出现 `0/0`、`NaN`、`0%` 当结论**（`contract/trace.md` §4）。
"""

from __future__ import annotations

from .base import UNKNOWN, is_number, is_unknown, numbers_close, rate

#: 契约允许的全部断言前缀（`contract/slices.md` §5）。
PREFIXES = (
    ("file:exists:", "exists"),
    ("file:absent:", "absent"),
    ("grep:", "grep"),
    ("nogrep:", "nogrep"),
    ("cmd:", "cmd"),
    ("ack:", "ack"),
)

KINDS = tuple(kind for _, kind in PREFIXES)
RESULTS = ("pass", "fail", "timeout", "unacked")

BASIS = (
    "分母=plan.json 的 steps[].accept 条数（声明口径，不随执行结果变）；分子=result==pass；"
    "ack 未确认计入分母不计入分子；cmd 超时单列 timeout（trace.md §5.2）"
)


def prefix_kind(expr):
    """断言原文 → 契约里的 6 种 kind；不属于那 6 种 → `None`（不静默放行）。"""
    text = str(expr or "")
    for prefix, kind in PREFIXES:
        if text.startswith(prefix):
            return kind
    return None


def _item_out(item, notes: list, step_n) -> dict:
    expr = item.get("expr", "")
    declared_kind = prefix_kind(expr)
    kind = item.get("kind") or declared_kind
    result = item.get("result", UNKNOWN)
    detail = item.get("detail", "")
    if declared_kind is None:
        notes.append("asserts.unknown_prefix:step%d %r 前缀不属于 6 种写法，按契约记 fail 不静默放行"
                     % (step_n, expr))
        result = "fail"
    elif kind != declared_kind:
        notes.append("asserts.kind_mismatch:step%d %r 声明 kind=%r，按原文应为 %r，以原文为准"
                     % (step_n, expr, kind, declared_kind))
        kind = declared_kind
    if result not in RESULTS and not is_unknown(result):
        notes.append("asserts.bad_result:step%d %r 的 result=%r 不在 {pass,fail,timeout,unacked} 里"
                     % (step_n, expr, result))
        result = UNKNOWN
    return {"expr": expr, "kind": kind, "result": result, "detail": detail}


def _count(results: list) -> dict:
    counts = {name: 0 for name in RESULTS}
    for value in results:
        if value in counts:
            counts[value] += 1
    counts["unknown"] = sum(1 for value in results if is_unknown(value))
    return counts


def score_step(step, notes: list) -> dict:
    """判一步的断言通过率。"""
    step_n = step.get("n")
    asserts = step.get("asserts") or {}
    raw_items = asserts.get("items")
    raw_items = raw_items if isinstance(raw_items, list) else []
    items = [_item_out(item if isinstance(item, dict) else {}, notes, step_n) for item in raw_items]
    counts = _count([item["result"] for item in items])

    # 分母：声明口径（plan 的 accept 条数），不随执行结果变
    total = asserts.get("total", UNKNOWN)
    if total is not None and not is_unknown(total) and not is_number(total):
        notes.append("asserts.bad_total:step%s total=%r 不是数也不是 unknown" % (step_n, total))
        total = UNKNOWN
    if not is_unknown(total) and total is not None and items and len(items) != total:
        notes.append("asserts.total_mismatch:step%s total=%s 而 items 有 %d 条（分母按声明口径，不跟着结果变）"
                     % (step_n, total, len(items)))
    if not raw_items and asserts.get("total") is None:
        notes.append("asserts.none:step%s 计划里没有断言 → total/pass_rate 记 null（不适用，不是 0 条全过）"
                     % (step_n,))

    passed = counts["pass"] if items else asserts.get("passed", UNKNOWN)
    if items and passed != asserts.get("passed", passed):
        notes.append("asserts.passed_mismatch:step%s run.json 记 passed=%r，按 items 数出来是 %d"
                     % (step_n, asserts.get("passed"), passed))
    failed = counts["fail"] if items else asserts.get("failed", UNKNOWN)
    timeout = counts["timeout"] if items else asserts.get("timeout", UNKNOWN)
    unacked = counts["unacked"] if items else asserts.get("unacked", UNKNOWN)

    # 收尾通过率：能从 items 直接数出来，就以数出来的为准（声明分母 + 实际结果）
    final_rate = rate(passed, total) if items else rate(asserts.get("passed", UNKNOWN), total)
    reported_final = (step.get("assert_pass") or {}).get("final_pass_rate", UNKNOWN)
    if is_number(final_rate) and is_number(reported_final) and not numbers_close(final_rate, reported_final):
        notes.append("asserts.final_pass_rate_mismatch:step%s run.json 记 %r，按 passed/total 算是 %r"
                     % (step_n, reported_final, final_rate))

    first_rate = (step.get("assert_pass") or {}).get("first_pass_rate", UNKNOWN)
    if not is_number(first_rate) and not is_unknown(first_rate) and first_rate is not None:
        notes.append("asserts.bad_first_pass_rate:step%s %r 不是数也不适用" % (step_n, first_rate))

    rework = step.get("rework", UNKNOWN)
    first_failed = step.get("first_failed", UNKNOWN)

    return {
        "step": step_n,
        "title": step.get("title", UNKNOWN),
        "total": total,
        "passed": passed,
        "failed": failed,
        "timeout": timeout,
        "unacked": unacked,
        "unknown": counts["unknown"],
        "first_pass_rate": first_rate,
        "first_pass_basis": "run.json 的 assert_pass.first_pass_rate（首次判定只留汇总量）",
        "final_pass_rate": final_rate,
        "final_pass_basis": "本次判分按 passed/total 重数（分母=声明口径）",
        "rework": rework,
        "first_failed": first_failed,
        "items": items,
    }


def _sum_field(by_step, name):
    """整 run 的计数求和：任一子项 `"unknown"` → `"unknown"`；全不适用 → `None`。"""
    if not by_step:
        return None
    values = [entry[name] for entry in by_step]
    if any(is_unknown(value) for value in values):
        return UNKNOWN
    numeric = [value for value in values if is_number(value)]
    if not numeric:
        return None
    return sum(numeric)


def _sum_totals(by_step) -> dict:
    """整 run 的分子分母与两个比率：严格传播，任一子项 unknown 就 unknown。"""
    total_sum = _sum_field(by_step, "total")
    passed_sum = _sum_field(by_step, "passed")

    # 首过率的整 run 口径：Σ(passed_first) / Σ(total)，passed_first = round(rate * total)
    first_sum = 0
    first_ok = True
    for entry in by_step:
        value, total = entry["first_pass_rate"], entry["total"]
        if value is None:
            continue
        if is_unknown(value) or not is_number(value) or is_unknown(total) or not is_number(total):
            first_ok = False
            break
        first_sum += int(round(value * total))
    first_rate = rate(first_sum, total_sum) if first_ok else UNKNOWN

    return {
        "total": total_sum,
        "passed": passed_sum,
        "first_pass_rate": first_rate,
        "final_pass_rate": rate(passed_sum, total_sum),
    }


def score(run_doc) -> dict:
    """整份 `run.json` 的断言段。"""
    notes = []
    steps = (run_doc.get("run") or {}).get("steps") or []
    by_step = [score_step(step, notes) for step in steps]
    summary = _sum_totals(by_step)
    counts = {
        "total": summary["total"],
        "passed": summary["passed"],
        "failed": _sum_field(by_step, "failed"),
        "timeout": _sum_field(by_step, "timeout"),
        "unacked": _sum_field(by_step, "unacked"),
    }
    return {
        "basis": BASIS,
        "thresholds": False,
        "threshold_note": "通过率不做判定：没有「≥N% 算过」这种阈值（trace.md §5.2）",
        "kinds_allowed": list(KINDS),
        "counts": counts,
        "first_pass_rate": summary["first_pass_rate"],
        "final_pass_rate": summary["final_pass_rate"],
        "rework_total": sum(entry["rework"] for entry in by_step if is_number(entry["rework"]))
        if any(is_number(entry["rework"]) for entry in by_step) else UNKNOWN,
        "by_step": by_step,
        "notes": notes,
    }
