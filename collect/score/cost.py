"""collect/score/cost.py —— ④ 成本与轮次判分（`contract/trace.md` §5.4）。

**轮次**：单位 = 一次 assistant 请求，`rounds = Σ 账本行的 reqs`；缺 `reqs` 的行按 0 计（沿 tollgate 口径）。
但 `rows_with_reqs == 0` 且 `rows_total > 0` 时 → `rounds: "unknown"`（一条都没记，凭什么叫「0 轮」）。

**成本**：`cost.usd` 是**下限**（`>=` 真实花费），永远带 `complete` 与 `unknown_rows` 一起给；
取值优先级 宿主 `data.cost`（`host`）> 本地价目重算（`estimate`，上界）> 都没有（`unknown`）；
两种口径混在一份 run 里 → `basis: "mixed"`，并且要在 `degraded_reasons` 里点明「含估算值」。

**没有价目就不许算钱**：拿 token 数乘一个编出来的均价等于编数字，明令禁止。
本判分器读不到任何价目表，因此 `pricing.table` 恒为 `"none"`、`estimated_rows` 只统计
`run.json` 里已有的 `cost.est: true`（口径来源是采集侧），自己一次乘法都不做。
"""

from __future__ import annotations

from .base import UNKNOWN, is_number, is_unknown, numbers_close, round_places, sum_known

TOKEN_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")

BASIS = (
    "cost.usd 是下限（>= 真实花费），永远与 complete / unknown_rows 一起读；"
    "口径优先级 host > estimate > mixed > unknown（trace.md §5.4）"
)

ROUNDS_BASIS = (
    "rounds = Σ 账本行的 reqs（缺 reqs 的行按 0 计）；rows_with_reqs==0 且 rows_total>0 → unknown，"
    "不许报 0 轮（trace.md §5.4）"
)

PRICING_POLICY = "没有价目就不许算钱：本判分器读不到价目表，绝不用均价乘 token 凑金额（trace.md §5.4）"


def _tokens_of(source) -> dict:
    tokens = source.get("tokens") or {}
    out = {field: tokens.get(field, UNKNOWN) for field in TOKEN_FIELDS}
    out["complete"] = bool(tokens.get("complete", False))
    return out


def _round_rows(step) -> list:
    rows = step.get("rounds_detail")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def cost_of_step(step) -> dict:
    """一步的成本：已知部分之和 + 完整性标记（成本口径是「下限」，允许部分求和）。"""
    declared = step.get("cost") or {}
    rows = _round_rows(step)
    known = 0.0
    known_rows = 0
    unknown_rows = 0
    null_rows = 0
    estimated = 0
    for row in rows:
        cost = row.get("cost") or {}
        value = cost.get("usd", UNKNOWN)
        if cost.get("est") is True:
            estimated += 1
        if value is None:
            null_rows += 1          # amend 记账行：确实不产生费用（null，不是 unknown）
        elif is_unknown(value):
            unknown_rows += 1
        elif is_number(value):
            known += float(value)
            known_rows += 1
        else:
            unknown_rows += 1

    if rows:
        usd = round_places(known) if known_rows else UNKNOWN
        complete = unknown_rows == 0
        basis = declared.get("basis", UNKNOWN)
        row_total = len(rows)
    else:
        usd = declared.get("usd", UNKNOWN)
        complete = bool(declared.get("complete", False))
        basis = declared.get("basis", UNKNOWN)
        if is_number(declared.get("unknown_rows")):
            unknown_rows = declared["unknown_rows"]
        row_total = UNKNOWN

    entry = {
        "step": step.get("n"),
        "title": step.get("title", UNKNOWN),
        "usd": usd,
        "complete": bool(complete),
        "basis": basis,
        "unknown_rows": unknown_rows,
        "round_rows": row_total,
        "rows_known": known_rows,
        "rows_null": null_rows,
        "is_lower_bound": (is_number(usd) and not complete),
        "rounds": step.get("rounds", UNKNOWN),
        "tokens": _tokens_of(step),
    }
    if estimated:
        entry["estimated_rows"] = estimated
    return entry


def rounds_of(run_doc) -> dict:
    """整 run 的轮次：拿账本覆盖率一起看，别让「0 轮」冒充「不知道」。"""
    run = run_doc.get("run") or {}
    coverage = run.get("rounds_coverage") or {}
    rows_total = coverage.get("rows_total", UNKNOWN)
    rows_with_reqs = coverage.get("rows_with_reqs", UNKNOWN)
    declared = run.get("rounds", UNKNOWN)

    steps = run.get("steps") or []
    per_step = [step.get("rounds", UNKNOWN) for step in steps]
    recomputed = sum_known(per_step) if per_step else None

    reason = ""
    rounds = declared
    if is_number(rows_with_reqs) and is_number(rows_total) and rows_with_reqs == 0 and rows_total > 0:
        rounds = UNKNOWN
        reason = ("rows_with_reqs=0 而 rows_total=%d：一条 reqs 都没记，不能报「0 轮」，记 unknown"
                  % rows_total)
    elif is_unknown(rows_with_reqs) or is_unknown(rows_total):
        reason = "账本覆盖率拿不到（rounds_coverage 里有 unknown），轮次可信度未知"
    if is_number(recomputed) and is_number(rounds) and recomputed != rounds:
        reason = (reason + "；" if reason else "") + (
            "按各步 rounds 求和是 %s，与 run.rounds=%s 不一致（以账本口径为准，记进 notes）"
            % (recomputed, rounds))

    return {
        "rounds": rounds,
        "rounds_coverage": {"rows_total": rows_total, "rows_with_reqs": rows_with_reqs},
        "recomputed_from_steps": recomputed,
        "reason": reason,
    }


def compare_cost(before: dict, after: dict) -> dict:
    """成本变化（`contract/trace.md` §6）：两侧 `cost.usd` 都是真数才给 delta，否则都 `"unknown"`。

    任一侧 `complete:false` → 附 `is_lower_bound: true`（下限之间的差仍是下限）。
    """
    before = before or {}
    after = after or {}
    before_usd = before.get("usd", UNKNOWN)
    after_usd = after.get("usd", UNKNOWN)
    before_complete = bool(before.get("complete", False))
    after_complete = bool(after.get("complete", False))
    out = {
        "before": {"usd": before_usd, "complete": before_complete},
        "after": {"usd": after_usd, "complete": after_complete},
        "delta_usd": UNKNOWN,
        "delta_pct": UNKNOWN,
        "direction": UNKNOWN,
        "is_lower_bound": not (before_complete and after_complete),
    }
    if is_number(before_usd) and is_number(after_usd):
        delta = float(after_usd) - float(before_usd)
        out["delta_usd"] = round_places(delta)
        if before_usd != 0:
            out["delta_pct"] = round_places(delta / float(before_usd) * 100.0)
        else:
            out["delta_pct"] = None      # 分母 0：不给百分比（不许 NaN）
        if numbers_close(before_usd, after_usd):
            out["direction"] = "flat"
        else:
            out["direction"] = "up" if delta > 0 else "down"
    else:
        out["reason"] = "两侧 cost.usd 不都是真数 → 不相减，delta 记 unknown（trace.md §6）"
    return out


def score(run_doc) -> dict:
    """整份 `run.json` 的成本与轮次段。"""
    run = run_doc.get("run") or {}
    steps = run.get("steps") or []
    by_step = [cost_of_step(step) for step in steps]

    declared = run.get("cost") or {}
    known = 0.0
    known_rows = 0
    unknown_rows = 0
    null_rows = 0
    estimated_rows = 0
    for entry in by_step:
        if is_number(entry["usd"]):
            known += float(entry["usd"])
            known_rows += entry["rows_known"]
        unknown_rows += entry["unknown_rows"] if is_number(entry["unknown_rows"]) else 0
        null_rows += entry["rows_null"]
        estimated_rows += entry.get("estimated_rows", 0)

    usd = round_places(known) if known_rows else declared.get("usd", UNKNOWN)
    if steps:
        complete = bool(declared.get("complete", False)) and unknown_rows == 0 and all(
            entry["complete"] for entry in by_step)
    else:
        complete = bool(declared.get("complete", False))
    basis = declared.get("basis", UNKNOWN)
    notes = []
    if is_number(usd) and is_number(declared.get("usd")) and not numbers_close(usd, declared.get("usd")):
        notes.append("cost.usd_mismatch:run.json 记 %r，按各轮 cost 求和是 %r（以明细求和为准）"
                     % (declared.get("usd"), usd))
    if estimated_rows and basis == "host":
        basis = "mixed"
        notes.append("cost.basis:mixed 含估算值（%d 行 cost.est），是上界不是真账" % estimated_rows)
    if is_number(usd) and not complete:
        notes.append("cost.is_lower_bound:true cost.usd=%s 只是下限（%d 行拿不到 cost）" % (usd, unknown_rows))

    rounds = rounds_of(run_doc)
    if rounds["reason"]:
        notes.append("rounds:%s" % rounds["reason"])

    return {
        "basis": basis,
        "rule": BASIS,
        "usd": usd,
        "complete": complete,
        "unknown_rows": unknown_rows,
        "round_rows": len([row for step in steps for row in _round_rows(step)]) or UNKNOWN,
        "rows_known": known_rows,
        "rows_null": null_rows,
        "is_lower_bound": (is_number(usd) and not complete),
        "pricing": {
            "table": "none",
            "policy": PRICING_POLICY,
            "estimated_rows": estimated_rows,
            "never_estimated_by_this_scorer": True,
        },
        "rounds": rounds["rounds"],
        "rounds_coverage": rounds["rounds_coverage"],
        "rounds_basis": ROUNDS_BASIS,
        "recomputed_rounds_from_steps": rounds["recomputed_from_steps"],
        "tokens": _tokens_of(run),
        "by_step": by_step,
        "notes": notes,
    }
