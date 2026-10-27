"""性能五标量的采集：`wall_s` / `steps[].wall_s` / `rounds[].gap_s` / `round_gap` / `turns` / `out_tokens_per_wall_s`。

口径来源：`contract/dimensions.md` §1（唯一原料三处、六个字段、unknown 规则、分位数）、
§5（OTel 名字映射）。本模块**只读**三处来源：

1. `run.json` 的 `run.started_at` / `run.finished_at`（delta 直接相减）；
2. 同名目录下的 `.plan/ledger.jsonl` 相邻行 `ts`；
3. 宿主库 `read_rounds` 给出的 `at`（账本 `ts` 缺时兜底）。

两条铁律（dimensions.md §1.3）：

- `ttft_s` 与 `tool_calls` **恒为 `"unknown"`**，不许出现数字。宿主库没有首 token 时刻、
  没有工具调用次数；`wall_s / rounds` 是「平均每轮全程耗时」，拿它冒充 TTFT 就是编数字。
- 缺字段一律 `"unknown"`，不写 0、不写 `null` 冒充（trace.md §4）。

三条消歧（dimensions.md §1.4 运维补记 2026-10-27）：

- `gap_s` 列表长度 = **轮次数 − 1**；首轮之前没有间隔，不占位、不写 `"unknown"`。
- `round_gap.n` = **间隔条数**，不是轮次数。
- `too_few_samples`（样本干净但少）与 `unknown_sample`（样本里真有算不出的）分开。
"""

import sys

sys.dont_write_bytecode = True

import json
import os

from . import base

# 结构上拿不到的两个量：写死的 `"unknown"`，任何输入都不许改（dimensions.md §1.3）
TTFT_UNKNOWN = base.UNKNOWN
TOOL_CALLS_UNKNOWN = base.UNKNOWN

# 样本集定义（dimensions.md §1.4 要求把口径一起输出；放在 run 级，不塞进 round_gap 里）
ROUND_GAP_DEFINITION = (
    "样本 = 同一步内、seq 连续的相邻两条轮次的 gap_s（长度 = 轮次数 − 1，首轮不占位）；"
    "聚合行 / seq 断裂 / 任一端缺时间 → 该条 unknown，整步按严格传播处理"
)
TURNS_DEFINITION = "有起点时刻（可解析 at）的 round 条数；不是 Σ reqs（dimensions.md §1.5）"
WALL_SCOPE = "run"

HONESTY = [
    "wall 时间含网络与中转站排队，不能当模型速度。",
    "样本是 n=1 夹具、单机串行，没有置信区间。",
]

LEDGER_REL = os.path.join(".plan", "ledger.jsonl")


# ---------------------------------------------------------------- 原始来源


def load_run_doc(path):
    """读 `run.json`；不存在 / 不是 JSON / 没有 `run` 对象 → 抛 `ValueError`（由 CLI 转成错误体）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        raise ValueError("run.json 不存在: %s" % path)
    except OSError as exc:
        raise ValueError("run.json 读不到: %s (%s)" % (path, exc))
    except ValueError as exc:
        raise ValueError("run.json 不是合法 JSON: %s (%s)" % (path, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("run"), dict):
        raise ValueError("run.json 缺少 run 对象: %s" % path)
    return doc


def run_dir_of(run_path):
    """`run.json` 同名目录（`.plan/ledger.jsonl` 的相对根）。"""
    return os.path.dirname(os.path.abspath(run_path))


def read_ledger(run_dir):
    """读 `.plan/ledger.jsonl`。

    返回 `(rows, present, reasons)`；`rows` 是保持文件顺序的 dict 列表。
    坏行跳过并记 `ledger.bad_json:<行号>`（trace.md §8.2），其余行照常用。
    """
    if not run_dir:
        return [], False, []
    path = os.path.join(run_dir, LEDGER_REL)
    rows = []
    reasons = []
    if not os.path.isfile(path):
        return rows, False, reasons
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().split("\n")
    except OSError as exc:
        return rows, True, ["ledger.unreadable:%s" % exc]
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            reasons.append("ledger.bad_json:%d" % index)
            continue
        if not isinstance(row, dict):
            reasons.append("ledger.bad_json:%d" % index)
            continue
        rows.append(row)
    return rows, True, reasons


def ledger_session(rows):
    """账本行里出现过的 `session` id（按出现顺序去重）—— 宿主库兜底时间源要用它。"""
    seen = []
    for row in rows:
        session = row.get("session")
        if isinstance(session, str) and session and session not in seen:
            seen.append(session)
    return seen


def read_host_rounds(sessions, db_path=None):
    """宿主库逐轮 `at`（只读）。拿不到就返回空列表 + reason，绝不编。

    `sessions` 来自账本的 `session` 列（dimensions.md §1.1 第 3 条只用它的 `at`
    做兜底时间源）。多个 session 时合并后按时间升序。
    """
    from . import _hostdb

    reasons = []
    collected = []
    if not sessions:
        return [], "host_db.no_session"
    for session in sessions:
        rounds, reason = _hostdb.read_session_rounds(session, db_path)
        if reason:
            reasons.append(reason)
        else:
            collected.extend(rounds)
    if not collected:
        return [], reasons[0] if reasons else "host_db.session_missing"
    collected.sort(key=lambda item: base.to_epoch_ms(item.get("at")) or 0.0)
    return collected, None


# ---------------------------------------------------------------- 归一化


def _step_rows(rows):
    """账本行按 `step` 分组（保持步号升序、步内文件顺序）。没有 `step` 的行按 unknown 留一组。"""
    grouped = {}
    for index, row in enumerate(rows):
        step = base.int_or_unknown(row.get("step"))
        grouped.setdefault(step, []).append((index, row))
    return grouped


def _round_at(row, host_rounds, host_index):
    """一轮的起点时刻：账本 `ts` 优先，缺了用宿主 `at` 兜底（dimensions.md §1.1 第 3 条）。"""
    raw = row.get("ts")
    if isinstance(raw, str) and base.parse_iso(raw) is not None:
        return raw, "ledger"
    if host_rounds and host_index < len(host_rounds):
        fallback = host_rounds[host_index].get("at")
        if isinstance(fallback, str) and base.parse_iso(fallback) is not None:
            return fallback, "host"
    return base.UNKNOWN, "unknown"


def step_rounds_from_run(run):
    """run.json 里摊平的轮次（保持文件顺序），外加「每个 step 是否带 `rounds_detail`」。

    返回 `(items, steps_meta)`：`items` 为 `{step, seq, at, rounds, index}`；
    `steps_meta` 为 `{step: {"detail_present": bool}}`（没有 `rounds_detail` 字段的步也要记账）。
    """
    items = []
    steps_meta = {}
    steps = run.get("steps")
    if not isinstance(steps, list):
        return items, steps_meta
    for step in steps:
        if not isinstance(step, dict):
            continue
        n = base.int_or_unknown(step.get("n"))
        detail = step.get("rounds_detail")
        meta = {"detail_present": isinstance(detail, list), "declared_rounds": base.int_or_unknown(step.get("rounds"))}
        steps_meta[n] = meta
        if not isinstance(detail, list):
            continue
        for index, item in enumerate(detail):
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    "step": n,
                    "seq": base.int_or_unknown(item.get("seq")),
                    "at": item.get("at") if isinstance(item.get("at"), str) else base.UNKNOWN,
                    "rounds": base.int_or_unknown(item.get("rounds")),
                    "index": index,
                }
            )
    return items, steps_meta


def step_rounds_from_ledger(ledger_rows, db_path=None):
    """账本兜底：每个 step 的轮次（`ts` 优先、宿主 `at` 次之）。`seq` 取行序（账本没有 seq 列）。"""
    reasons = []
    host_rounds, host_reason = read_host_rounds(ledger_session(ledger_rows), db_path)
    items = []
    steps_meta = {}
    grouped = _step_rows(ledger_rows)
    for step, rows in sorted(grouped.items(), key=lambda kv: (not base.is_number(kv[0]), kv[0])):
        steps_meta[step] = {"detail_present": True, "declared_rounds": base.UNKNOWN}
        for offset, (_index, row) in enumerate(rows, start=1):
            at, src = _round_at(row, host_rounds, offset - 1)
            if src == "unknown":
                reasons.append("round.at_unknown")
            rounds = row.get("rounds", row.get("reqs"))
            items.append(
                {
                    "step": step,
                    "seq": offset,
                    "at": at,
                    "rounds": base.int_or_unknown(rounds),
                    "index": offset - 1,
                }
            )
    if host_reason and host_rounds:
        reasons.append(host_reason)
    return items, steps_meta, reasons


def _ordered(items):
    """按 `seq` 升序；`seq` 拿不到时保持文件顺序。"""
    return sorted(
        items,
        key=lambda it: (0, it["seq"], it["index"]) if base.is_number(it["seq"]) else (1, 0, it["index"]),
    )


def _gap(prev, cur):
    """相邻两条轮次的间隔；拿不到就是 `"unknown"`（严格传播，不跳过）。"""
    if not (base.is_number(prev.get("seq")) and base.is_number(cur.get("seq"))):
        return base.UNKNOWN
    if cur["seq"] != prev["seq"] + 1:
        return base.UNKNOWN
    if prev.get("rounds") != 1 or cur.get("rounds") != 1:
        # 聚合行（一轮覆盖多条请求）没有「逐轮起点」，相邻间隔不成立
        return base.UNKNOWN
    return base.delta_s(cur.get("at"), prev.get("at"))


def round_gaps(step_items):
    """逐轮 `gap_s`：同一步内、`seq` 连续才相减；聚合行 → `"unknown"`。

    `step_items`：`{step_n: [{"seq", "at", "rounds"}, ...]}`（已按 seq 升序）。
    返回 `{step_n: [gap, ...]}`，**长度 = 轮次数 − 1**（首轮之前没有间隔，不占位）。
    """
    out = {}
    for step, items in step_items.items():
        out[step] = [_gap(prev, cur) for prev, cur in zip(items, items[1:])]
    return out


def step_wall(items):
    """`steps[].wall_s`：该 step 第一条轮次时间 → 最后一条轮次时间。

    返回 `(value, reason)`：
    - 无轮次 → `(None, None)`（结构上不适用，不是 unknown、更不是 0.0）；
    - 只有 1 条轮次 → `("unknown", "fewer_than_two_rounds")`（一个时间点没有长度）；
    - 任一端时间拿不到 → `("unknown", "endpoint_unknown")`；
    - 否则 → `(秒数, None)`。
    """
    if not items:
        return None, None
    if len(items) < 2:
        return base.UNKNOWN, "fewer_than_two_rounds"
    ats = [item.get("at") for item in items]
    if any(base.to_epoch_ms(at) is None for at in ats):
        return base.UNKNOWN, "endpoint_unknown"
    return base.delta_s(ats[-1], ats[0]), None


def out_tokens_per_wall_s(tokens_output, wall_s):
    """`run.tokens.output ÷ wall_s`（工具输出速率，不是模型 token/s）。"""
    if base.is_unknown(wall_s) or not base.is_number(wall_s):
        return base.UNKNOWN, "wall_unknown"
    if wall_s == 0:
        return base.UNKNOWN, "zero_wall"
    if base.is_unknown(tokens_output) or not base.is_number(tokens_output):
        return base.UNKNOWN, "tokens_output_unknown"
    return base.round6(tokens_output / wall_s), None


def run_round_gap(gaps_by_step):
    """run 级 `round_gap`：**不跨 step 聚合**（dimensions.md §1.4）。

    - 只有 1 个 step 出样本 → 直接用它的样本（`n` = 间隔条数）；
    - ≥2 个 step 都出样本 → 三者全 `"unknown"` + `round_gap_reason: "multi_step_no_aggregate"`；
    - 一个都不出样本 → `n: 0` + 三个 `null`（不适用）。
    """
    contributors = [gaps for gaps in gaps_by_step.values() if gaps]
    if not contributors:
        return base.percentiles([])
    if len(contributors) == 1:
        return base.percentiles(contributors[0])
    return {
        "n": sum(len(gaps) for gaps in contributors),
        "p50": base.UNKNOWN,
        "p90": base.UNKNOWN,
        "max": base.UNKNOWN,
        "round_gap_reason": "multi_step_no_aggregate",
    }


# ---------------------------------------------------------------- 主入口


def measure(run_doc, run_dir=None, db_path=None):
    """从一份 run.json（+ 同名目录账本 + 宿主库）算出 measure 段。纯函数、不写盘。"""
    run = run_doc.get("run") or {}
    reasons = []

    started = run.get("started_at")
    finished = run.get("finished_at")
    wall_reason = None
    if base.is_unknown(started) or base.is_unknown(finished):
        wall_s = base.UNKNOWN
        wall_reason = "endpoint_unknown"
    else:
        wall_s = base.delta_s(finished, started)
        if not base.is_number(wall_s):
            wall_reason = "endpoint_unparseable"
    if wall_reason:
        reasons.append("wall_s:%s" % wall_reason)

    tokens = run.get("tokens") if isinstance(run.get("tokens"), dict) else {}
    speed, speed_reason = out_tokens_per_wall_s(tokens.get("output"), wall_s)
    if speed == base.UNKNOWN:
        reasons.append("out_tokens_per_wall_s:%s" % speed_reason)

    items, steps_meta = step_rounds_from_run(run)
    detail_used = "run.json"

    # 账本兜底：run.json 没带任何轮次、但同名目录里有账本时（dimensions.md §1.1 第 2 条）
    ledger_rows, ledger_present, ledger_reasons = read_ledger(run_dir)
    reasons.extend(ledger_reasons)
    ledger_used = False
    if not items and ledger_present and ledger_rows:
        ledger_items, ledger_meta, ledger_item_reasons = step_rounds_from_ledger(ledger_rows, db_path)
        if ledger_items:
            ledger_used = True
            detail_used = "ledger"
            items = ledger_items
            steps_meta = ledger_meta
            reasons.extend(ledger_item_reasons)

    step_items = {step: [] for step in steps_meta}
    for item in items:
        step_items.setdefault(item["step"], []).append(item)
    for step in step_items:
        step_items[step] = _ordered(step_items[step])

    gaps_by_step = round_gaps(step_items)
    round_gap = run_round_gap(gaps_by_step)

    # turns = 可得 at 的 round 条数（dimensions.md §1.2 / §1.5）
    known_at = sum(1 for item in items if base.to_epoch_ms(item.get("at")) is not None)
    missing_detail = any(not meta["detail_present"] for meta in steps_meta.values())
    if items:
        turns = known_at if known_at else base.UNKNOWN
        if not known_at:
            reasons.append("turns:no_round_at")
    elif detail_used == "run.json" and not missing_detail:
        turns = 0  # 来源在、确实没有轮次（合法 0）
    else:
        turns = base.UNKNOWN
        reasons.append("turns:unknown")

    steps = []
    for step in sorted(step_items, key=lambda s: (not base.is_number(s), s)):
        step_list = step_items[step]
        meta = steps_meta.get(step) or {}
        wall, why = step_wall(step_list)
        if not meta.get("detail_present", True):
            wall, why = base.UNKNOWN, "rounds_detail_missing"
        step_rounds = [item["rounds"] for item in step_list]
        steps.append(
            {
                "n": step,
                "wall_s": wall,
                "wall_s_reason": why,
                "turns": sum(1 for item in step_list if base.to_epoch_ms(item.get("at")) is not None),
                "rounds": sum(step_rounds, 0) if step_rounds and all(base.is_number(r) for r in step_rounds) else base.UNKNOWN,
                "gap_s": gaps_by_step.get(step, []),
                "round_gap": base.percentiles(gaps_by_step.get(step, [])),
            }
        )

    if any(base.is_unknown(s.get("wall_s")) for s in steps):
        reasons.append("steps.wall_s:unknown")
    if any(not base.is_number(s.get("rounds")) for s in steps):
        reasons.append("steps.rounds:unknown")

    unique = []
    for reason in reasons:
        if reason not in unique:
            unique.append(reason)

    payload = {
        "schema": 1,
        "run_id": run.get("id") if isinstance(run.get("id"), str) else base.UNKNOWN,
        "scope": WALL_SCOPE,
        "wall_s": wall_s,
        "wall_s_source": "run.finished_at - run.started_at",
        "ttft_s": TTFT_UNKNOWN,
        "ttft_reason": "structurally_unavailable",
        "tool_calls": TOOL_CALLS_UNKNOWN,
        "tool_calls_reason": "structurally_unavailable",
        "turns": turns,
        "turns_definition": TURNS_DEFINITION,
        "rounds": base.int_or_unknown(run.get("rounds")),
        "out_tokens_per_wall_s": speed,
        "out_tokens_unknown_reason": speed_reason,
        "round_gap": round_gap,
        "round_gap_definition": ROUND_GAP_DEFINITION,
        "steps": steps,
        "otel_map": {
            "wall_s": {"otl": "gen_ai.invoke_agent.duration", "scope": WALL_SCOPE},
            "ttft_s": {"otl": "gen_ai.server.time_to_first_token"},
            "tool_calls": {"otl": "gen_ai.invoke_agent.tool_calls"},
        },
        "honesty": list(HONESTY),
        "unknown_reasons": unique,
        "sources": {
            "ledger_present": ledger_present,
            "ledger_rows": len(ledger_rows),
            "ledger_used_for_rounds": ledger_used,
            "rounds_read_from": detail_used,
        },
    }
    if wall_reason:
        payload["wall_s_reason"] = wall_reason
    return payload
