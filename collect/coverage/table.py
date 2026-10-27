"""collect/coverage/table.py —— 判据覆盖表的核心（`contract/dimensions.md` §2）。

这一层只做两件事：

1. 把 `plan.json` 的每条 `accept` 摊成规格点 `accept:<全局序号>`（1..N，跨步连续编号，
   **不是**步骤内编号，也不是步号）；
2. 给每个规格点标**一个**证据级（`mechanical` / `semantic` / `none` / `not_applicable`），
   证据级的判定规则写死在 `classify_accept()` 里，可复现、不看人脸色。

`clause:<短名>` 来自人工登记的 `spec/points.json`（trace.md 里写明、但没有被任何 accept
覆盖的条款）。

输出只有**计数 + 逐条证据**；本模块里没有、也不许有加权、总分、阈值、百分比。
"""

from __future__ import annotations

import json
import os

#: 证据级（`contract/dimensions.md` §2.1）。顺序即输出顺序。
LEVELS = ("mechanical", "semantic", "none", "not_applicable")

#: 能让规格点升到 `mechanical` 的断言结果：真的判过（过或破）。
#: `timeout` / `unacked` / `"unknown"` 都不算 —— 「没验完」不是「有机械证据」。
GATED_RESULTS = ("pass", "fail")

#: 禁用键名片段（§2.3 的四类字段 + §2.4 的合成分数词）。
#: 只按**键名**判、且按键名的 `_` 分词比对，不对整份 JSON 做子串搜索：
#: `degraded_reasons` 里含 `grade` 这四个字母，但它不是等级字段，不许误伤。
BANNED_KEY_PARTS = ("score", "weight", "grade", "threshold",
                    "rate", "pct", "percent", "ratio")

#: 条款梳理用的规范性措辞（`clauses_total` 的自动口径，见 `count_clauses()`）。
CLAUSE_MARKERS = ("必须", "不许", "不得", "严禁", "禁止", "永不用", "永远",
                  "一律", "只能", "只许", "不可", "绝")

#: `clause:` 条目的必备字段（简报 §五 ⑤）。
POINT_FIELDS = ("id", "clause", "level", "evidence", "reason")


def load_json(path: str) -> dict:
    """只读打开一份 JSON（`contract/trace.md` §3.2 第 1 条：只 `open(..., "r")`）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return json.load(handle)


def dumps(document: dict) -> str:
    """确定性落盘形状：UTF-8 无 BOM、`indent=1`、末尾恰好一个换行（幂等，§七 ⑬）。"""
    return json.dumps(document, ensure_ascii=False, indent=1) + "\n"


# --------------------------------------------------------------------------
# 规格点来源一：plan.json 的 accept
# --------------------------------------------------------------------------

def plan_steps(plan_doc: dict) -> list:
    """取计划步骤数组；容忍 `{"plan": {"steps": [...]}}` 这层信封。"""
    if not isinstance(plan_doc, dict):
        return []
    steps = plan_doc.get("steps")
    if not isinstance(steps, list):
        steps = (plan_doc.get("plan") or {}).get("steps")
    return steps if isinstance(steps, list) else []


def iter_accepts(plan_doc: dict):
    """按计划顺序摊平 `steps[].accept`，给出 `(全局序号, 步号, 断言原文)`。

    全局序号从 1 起、跨步连续 —— 这是 `accept:<序号>` 的唯一编号口径（简报 §三 ④）。
    条目总数由数据决定，**不写死**（本计划在简报写作时是 115，之后经 amend 已变）。
    """
    serial = 0
    for step in plan_steps(plan_doc):
        if not isinstance(step, dict):
            continue
        accepts = step.get("accept")
        if not isinstance(accepts, list):
            continue
        for expr in accepts:
            serial += 1
            yield serial, step.get("n"), expr


def count_accepts(plan_doc: dict) -> int:
    """规格点总数（= `counts.total` 的 accept 部分）。"""
    return sum(1 for _ in iter_accepts(plan_doc))


# --------------------------------------------------------------------------
# 打分结果索引：score.json 的 asserts.by_step[*].items
# --------------------------------------------------------------------------

def assert_index(score_doc: dict) -> dict:
    """`expr -> [{"step", "index", "result"}]`。

    只认 `score.json → asserts → by_step[] → items[]`。`score.json` 是 **run 级** 判分，
    **没有** `steps` 顶层键（简报 §二 点名的坑：那条路是空的）。
    """
    index = {}
    if not isinstance(score_doc, dict):
        return index
    blocks = (score_doc.get("asserts") or {}).get("by_step")
    if not isinstance(blocks, list):
        return index
    for block in blocks:
        if not isinstance(block, dict):
            continue
        items = block.get("items")
        if not isinstance(items, list):
            continue
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            expr = item.get("expr")
            if not isinstance(expr, str):
                continue
            index.setdefault(expr, []).append({
                "step": block.get("step"),
                "index": position,
                "result": item.get("result"),
            })
    return index


def consistency_guard(plan_doc: dict, score_doc: dict) -> dict:
    """一致性守卫（简报 §八 ⑮ ⑰ / dimensions.md §2.2 的 ⚠）。

    规格点必须来自**这次 run 实际执行的那个项目**的 `.plan/plan.json`；
    若 `score.json` 的断言与 plan 的 `accept` 交集不过半（`交集 * 2 < plan.accept`），
    说明两边不是同一个项目 —— 调用方必须报错退出，**不许**照常输出一张几乎全 `none` 的表。

    没有 score（或读不到、里面一条断言都没有）时不比：那是「本次没有判分」，
    不是「两个项目对不上」。返回的字典只用于人读与回溯，不落进覆盖表（不出分数）。
    """
    plan_exprs = [expr for _, _, expr in iter_accepts(plan_doc) if isinstance(expr, str)]
    score_exprs = list(assert_index(score_doc))
    plan_set = set(plan_exprs)
    score_set = set(score_exprs)
    matched = plan_set & score_set

    plan_sample = next((expr for expr in plan_exprs if expr not in score_set),
                       plan_exprs[0] if plan_exprs else "")
    score_sample = next((expr for expr in score_exprs if expr not in plan_set),
                        score_exprs[0] if score_exprs else "")

    comparable = bool(plan_exprs) and bool(score_exprs)
    return {
        "ok": not (comparable and len(matched) * 2 < len(plan_exprs)),
        "plan_total": len(plan_exprs),
        "score_total": len(score_exprs),
        "intersection": len(matched),
        "plan_sample": plan_sample,
        "score_sample": score_sample,
    }


def classify_accept(expr, index: dict) -> dict:
    """⑥ 证据级判定：**原文完全相等**且 `result ∈ {pass, fail}` 才是 `mechanical`。

    没有断言、或断言结果拿不到（timeout / unacked / unknown）→ `none`：
    「没验完」不是机械证据，也不许拿它冒充通过。
    """
    hits = [hit for hit in index.get(expr, []) if hit.get("result") in GATED_RESULTS]
    if not hits:
        return {"level": "none", "evidence": "", "reason": ""}
    hit = hits[0]
    evidence = "asserts.by_step[step=%s].items[%d]: result=%s" % (
        hit["step"], hit["index"], hit["result"])
    return {"level": "mechanical", "evidence": evidence, "reason": ""}


# --------------------------------------------------------------------------
# 规格点来源二：spec/points.json 的 clause
# --------------------------------------------------------------------------

def load_points(spec_path: str) -> list:
    """读人工登记表；字段不全 / 级别非法 / id 重复 → 直接报错（不静默吞）。"""
    document = load_json(spec_path)
    points = document.get("points") if isinstance(document, dict) else None
    if not isinstance(points, list):
        raise ValueError("%s 缺少 points[] 数组" % spec_path)
    seen = set()
    for entry in points:
        if not isinstance(entry, dict):
            raise ValueError("%s 的 points[] 里有非对象条目" % spec_path)
        missing = [name for name in POINT_FIELDS if name not in entry]
        if missing:
            raise ValueError("%s 的条目 %r 缺字段：%s"
                             % (spec_path, entry.get("id"), ",".join(missing)))
        point_id = entry["id"]
        if not isinstance(point_id, str) or not point_id.startswith("clause:"):
            raise ValueError("%s 的 id %r 不是 clause:<短名>" % (spec_path, point_id))
        if point_id in seen:
            raise ValueError("%s 的 id %r 重复" % (spec_path, point_id))
        seen.add(point_id)
        if entry["level"] not in LEVELS:
            raise ValueError("%s 的 %s 级别 %r 不在 %s 里"
                             % (spec_path, point_id, entry["level"], list(LEVELS)))
        if entry["level"] == "not_applicable" and not str(entry.get("reason") or "").strip():
            raise ValueError("%s 的 %s 是 not_applicable 却没写 reason" % (spec_path, point_id))
    return points


def check_point_evidence(points: list, root: str) -> list:
    """核 `evidence` 指向的文件与行号真实存在（简报 §三 ⑤：不许编）。返回问题列表。"""
    problems = []
    for entry in points:
        evidence = str(entry.get("evidence") or "")
        path, _, line_text = evidence.rpartition(":")
        if not path or not line_text.isdigit():
            problems.append("%s 的 evidence %r 不是 <路径>:<行号> 形状" % (entry["id"], evidence))
            continue
        target = path if os.path.isabs(path) else os.path.join(root, path)
        if not os.path.isfile(target):
            problems.append("%s 的 evidence %r 指向的文件不存在" % (entry["id"], evidence))
            continue
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            total = sum(1 for _ in handle)
        if int(line_text) < 1 or int(line_text) > total:
            problems.append("%s 的 evidence %r 超出文件行数 %d" % (entry["id"], evidence, total))
    return problems


# --------------------------------------------------------------------------
# 未登记条款：clauses_total / registered / delta
# --------------------------------------------------------------------------

def count_clauses(trace_text: str) -> int:
    """trace.md 里可枚举的条款数（自动口径，写死、可复算）。

    一条 = **含规范性措辞**（必须/不许/不得/严禁/禁止/永远/一律/只能/只许/不可/绝…）的
    正文行；跳过空行、标题行（`#`）、代码块内、表格分隔行（`|---|`）。
    同一句话在多处重复就按行重复计 —— 这是「可枚举」的口径，不是「唯一语义去重」的口径；
    本数只用于 `delta`，不出任何分数。
    """
    total = 0
    in_fence = False
    for raw in trace_text.split("\n"):
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith("#"):
            continue
        if line.startswith("|") and set(line) <= set("|-: "):
            continue
        if any(marker in line for marker in CLAUSE_MARKERS):
            total += 1
    return total


# --------------------------------------------------------------------------
# 组装 + 自洽校验
# --------------------------------------------------------------------------

def build_table(plan_doc: dict, score_doc: dict, points: list, clauses_total: int) -> dict:
    """覆盖表本体：`counts` + `items` + `unregistered_clauses`（不出总分）。"""
    index = assert_index(score_doc)
    items = []

    for serial, step_n, expr in iter_accepts(plan_doc):
        outcome = classify_accept(expr, index)
        items.append({
            "id": "accept:%d" % serial,
            "source": "accept",
            "prefix": "accept:",
            "level": outcome["level"],
            "expr": expr if isinstance(expr, str) else "",
            "evidence": outcome["evidence"],
            "reason": outcome["reason"],
        })

    for entry in points:
        items.append({
            "id": entry["id"],
            "source": "clause",
            "prefix": "clause:",
            "level": entry["level"],
            "expr": "",
            "evidence": entry["evidence"],
            "reason": str(entry.get("reason") or ""),
        })

    counts = {level: 0 for level in LEVELS}
    for item in items:
        counts[item["level"]] += 1
    counts["total"] = len(items)

    registered = len(points)
    delta = clauses_total - registered
    document = {
        "counts": {
            "mechanical": counts["mechanical"],
            "semantic": counts["semantic"],
            "none": counts["none"],
            "not_applicable": counts["not_applicable"],
            "total": counts["total"],
        },
        "items": items,
        "unregistered_clauses": {
            "clauses_total": clauses_total,
            "registered": registered,
            "delta": delta,
        },
    }
    if delta > 0:
        document["discriminating_power"] = "不具备判别力"
    return document


def iter_keys(node, path=""):
    """递归枚举所有**键名**（含它所在的位置），供禁用键扫描用。"""
    if isinstance(node, dict):
        for key, value in node.items():
            here = "%s/%s" % (path, key) if path else str(key)
            yield here, str(key)
            for pair in iter_keys(value, here):
                yield pair
    elif isinstance(node, list):
        for position, value in enumerate(node):
            for pair in iter_keys(value, "%s[%d]" % (path, position)):
                yield pair


def check_document(document: dict) -> list:
    """覆盖表自洽：counts 与 items 对齐、id 不重、delta 与判别力成对、禁用键不存在。"""
    problems = []
    items = document.get("items")
    counts = document.get("counts") or {}
    if not isinstance(items, list):
        return ["items 不是数组"]

    seen = set()
    tally = {level: 0 for level in LEVELS}
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append("items[%d] 不是对象" % position)
            continue
        point_id = item.get("id")
        if not isinstance(point_id, str) or not point_id:
            problems.append("items[%d] 缺 id" % position)
        elif point_id in seen:
            problems.append("id 重复：%s" % point_id)
        else:
            seen.add(point_id)
        level = item.get("level")
        if level not in LEVELS:
            problems.append("%r 的级别 %r 非法" % (point_id, level))
            continue
        tally[level] += 1
        prefix = item.get("prefix")
        if not isinstance(prefix, str) or not str(point_id).startswith(prefix):
            problems.append("%r 的 prefix=%r 与 id 前缀不一致" % (point_id, prefix))
        if level == "not_applicable" and not str(item.get("reason") or "").strip():
            problems.append("%r 是 not_applicable 却没写 reason" % point_id)
        if level == "mechanical" and not str(item.get("evidence") or "").strip():
            problems.append("%r 是 mechanical 却没写 evidence" % point_id)

    for level in LEVELS:
        if counts.get(level) != tally[level]:
            problems.append("counts.%s=%r 与 items 实际计数 %d 不一致"
                            % (level, counts.get(level), tally[level]))
    if counts.get("total") != len(items):
        problems.append("counts.total=%r 与 items 长度 %d 不一致"
                        % (counts.get("total"), len(items)))

    gap = document.get("unregistered_clauses") or {}
    total_clauses = gap.get("clauses_total")
    registered = gap.get("registered")
    delta = gap.get("delta")
    if not all(isinstance(value, int) for value in (total_clauses, registered, delta)):
        problems.append("unregistered_clauses 三个字段必须都是整数")
    elif delta != total_clauses - registered:
        problems.append("delta=%d 不等于 clauses_total(%d) - registered(%d)"
                        % (delta, total_clauses, registered))
    elif (delta > 0) != ("discriminating_power" in document):
        problems.append("discriminating_power 必须且只能在 delta>0 时出现")

    for where, key in iter_keys(document):
        tokens = [token for token in key.lower().replace("-", "_").split("_") if token]
        hits = [part for part in BANNED_KEY_PARTS if part in tokens]
        if hits:
            problems.append("禁用键名 %s（命中 %r）" % (where, hits[0]))
    return problems
