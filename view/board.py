#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/board.py · 看板：轮次/成本曲线 + 失败分布（只吃 run.json）

用法：

    python -m view.board --run runs/fx-rework01/run.json --out out/board.html
    python -m view.board --run runs/fx-rework01/run.json --json
    python -m view.board --self-test

口径来源：`contract/trace.md`
- §2.4 轮次明细落在 `steps[].rounds_detail`（`seq` / `tokens` / `cost.usd` / `cost.est` / `rounds`）；
- §5.2 断言结果只有 `pass/fail/timeout/unacked` 四种；`timeout` 与 `unacked` 是「没验完」，
  必须与 `fail` 分开画、分开口径说；
- §5.4 `cost.usd` 是**下限**（已知部分之和），必须带 `complete` / `unknown_rows`；
- §4 `0` 只表示真的 0，`null` 表示不适用，`"unknown"` 表示拿不到 —— 画图上同理：
  **拿不到的轮次不许落成 0 美元的柱子**，要画成「未知」的标记并单独数出来；
- §8.1 没有步骤（`steps: 0`）时不许画成「全部通过」，也不许写 `0/0`、`0%`，整块写「无数据」。

边界（contract/slices.md §2）：本文件只读 `runs/**/run.json`。
`.plan/**` 一次都不许读 —— 入口第一件事就是 `refuse_plan_path()`，宁可拒跑也不越界。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path

from .dimensions import NOT_SUPPORTED, build_dimensions
from .export import (
    COST_DIGITS,
    REWORK_TOTAL_NOTE,
    UNKNOWN,
    ToolError,
    emit_error,
    fixed,
    is_int,
    is_num,
    load_document,
    norm_bool,
    norm_int,
    norm_num,
    pick,
    refuse_plan_path,
    rework_total,
    run_section,
    steps_of,
    sub,
    write_bytes_out,
    write_stdout_bytes,
)

#: 看板本身没有版本契约，跟着 `run.json` 的 `schema` 走
BOARD_SCHEMA = 1

#: 自检夹具：导出三件套那两份编造样本（形状照 §2 抄，数字是编的），不挂 `runs/`
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "export"

#: §5.2 只有 pass 是「验过了」；其余四种各自一句话，画图与表头都用这套顺序
RESULT_ORDER = ("fail", "timeout", "unacked", "unknown")
RESULT_LABELS = {
    "fail": "fail（破了）",
    "timeout": "timeout（没验完）",
    "unacked": "unacked（没人工确认）",
    "unknown": "unknown（拿不到）",
}
RESULT_COLORS = {
    "fail": "#c0392b",
    "timeout": "#c9a227",
    "unacked": "#6c5ce7",
    "unknown": "#7f8c8d",
}
PASS_COLOR = "#d5dbdb"

#: 图尺寸（写死，保证同一份输入渲染出同一串字节，§7.5 的口径）
CHART_W = 880
CHART_H = 260
PAD_L = 64
PAD_R = 104
PAD_T = 18
PAD_B = 46


# --------------------------------------------------------------------------- #
# 出口：人话一律 stderr，stdout 只放产物/JSON（trace.md §7.1）
# --------------------------------------------------------------------------- #
def note(message):
    sys.stderr.write("[board] " + message + "\n")


def dumps(document):
    """唯一的 JSON 序列化口径：`ensure_ascii=false`、`indent=1`、末尾恰好一个 `\\n`。"""
    return json.dumps(document, ensure_ascii=False, indent=1, allow_nan=False) + "\n"


# --------------------------------------------------------------------------- #
# 取数：只从 run.json 里取，缺就是 `"unknown"`（§4）
# --------------------------------------------------------------------------- #
def _seq_key(value):
    """轮次排序键（§7.5 数组按稳定键排序：rounds 按 `seq`）。"""
    return (0, value) if is_int(value) else (1, 0)


def rounds_of(document):
    """跨步的轮次级序列（按 `step.n`、`seq` 升序），形状照 §2.4。

    每个元素都保留三态：`cost` 可能是 number / `None`（真的不产生费用）/ `"unknown"`。
    """
    rows = []
    for step in steps_of(run_section(document)):
        number = norm_int(pick(step, "n"))
        title = pick(step, "title")
        raw = step.get("rounds_detail")
        items = [item for item in (raw or []) if isinstance(item, dict)]
        for item in sorted(items, key=lambda item: _seq_key(pick(item, "seq"))):
            cost = sub(item, "cost")
            rows.append(
                {
                    "step": number,
                    "title": title,
                    "seq": norm_int(pick(item, "seq")),
                    "at": pick(item, "at"),
                    "input": norm_int(pick(item, "input")),
                    "output": norm_int(pick(item, "output")),
                    "reasoning": norm_int(pick(item, "reasoning")),
                    "cache_read": norm_int(pick(item, "cache_read")),
                    "cache_write": norm_int(pick(item, "cache_write")),
                    "cost": norm_num(pick(cost, "usd")),
                    "cost_est": norm_bool(pick(cost, "est")),
                    "rounds": norm_int(pick(item, "rounds")),
                    "note": pick(item, "note"),
                    "src": pick(item, "src"),
                }
            )
    return rows


def failures_of(document):
    """所有 `result != "pass"` 的断言（§5.2：fail 是破了，timeout/unacked 是没验完）。"""
    rows = []
    for step in steps_of(run_section(document)):
        number = norm_int(pick(step, "n"))
        title = pick(step, "title")
        for item in sub(step, "asserts").get("items") or []:
            if not isinstance(item, dict):
                continue
            result = item.get("result", UNKNOWN)
            if result == "pass":
                continue
            rows.append(
                {
                    "step": number,
                    "title": title,
                    "expr": item.get("expr", UNKNOWN),
                    "kind": item.get("kind", UNKNOWN),
                    "result": result,
                    "detail": item.get("detail", ""),
                }
            )
    return rows


def failure_counts(document):
    """按步统计：`pass` 与四种非 pass 各几条（分母是这步的全部断言）。"""
    table = []
    for step in steps_of(run_section(document)):
        counts = {"pass": 0, "fail": 0, "timeout": 0, "unacked": 0, "unknown": 0}
        for item in sub(step, "asserts").get("items") or []:
            if not isinstance(item, dict):
                continue
            result = item.get("result", UNKNOWN)
            counts[result if result in counts else "unknown"] += 1
        table.append(
            {
                "step": norm_int(pick(step, "n")),
                "title": pick(step, "title"),
                "counts": counts,
                "total": sum(counts.values()),
                "non_pass": sum(counts[key] for key in RESULT_ORDER),
            }
        )
    return table


def summarize(document):
    """一份给机器看的摘要：`board.__m` 与 `cli report` 共用同一张「数」的表。

    所有值都守 §4 三态（缺就是 `"unknown"`，真的 0 才是 0），并且显式说明
    「画得出来的轮次 / 画不出来的轮次」各几条 —— 免得看板把缺数据画成省钱。
    """
    run = run_section(document)
    steps = steps_of(run)
    rows = rounds_of(document)
    tokens = sub(run, "tokens")
    cost = sub(run, "cost")

    known_cost = [row for row in rows if is_num(row["cost"])]
    null_cost = [row for row in rows if row["cost"] is None]
    unknown_cost = [row for row in rows if row["cost"] is UNKNOWN]

    non_pass = failures_of(document)
    by_result = {key: 0 for key in RESULT_ORDER}
    for row in non_pass:
        by_result[row["result"] if row["result"] in by_result else "unknown"] += 1

    asserts_total = 0
    asserts_passed = 0
    for step in steps:
        item_total = norm_int(pick(sub(step, "asserts"), "total"))
        item_passed = norm_int(pick(sub(step, "asserts"), "passed"))
        if is_int(item_total):
            asserts_total += item_total
        if is_int(item_passed):
            asserts_passed += item_passed

    return {
        "run_id": pick(run, "id"),
        "verdict": pick(run, "verdict"),
        "degraded": norm_bool(pick(run, "degraded")),
        "steps": len(steps),
        "rounds": norm_int(pick(run, "rounds")),
        "rounds_rows": len(rows),
        "rounds_with_cost": len(known_cost),
        "rounds_cost_null": len(null_cost),
        "rounds_cost_unknown": len(unknown_cost),
        "tokens": {
            "input": norm_int(pick(tokens, "input")),
            "output": norm_int(pick(tokens, "output")),
            "reasoning": norm_int(pick(tokens, "reasoning")),
            "cache_read": norm_int(pick(tokens, "cache_read")),
            "cache_write": norm_int(pick(tokens, "cache_write")),
            "complete": norm_bool(pick(tokens, "complete")),
        },
        "cost": {
            "usd": norm_num(pick(cost, "usd")),
            "complete": norm_bool(pick(cost, "complete")),
            "basis": pick(cost, "basis"),
            "unknown_rows": norm_int(pick(cost, "unknown_rows")),
        },
        "asserts": {
            "passed": asserts_passed,
            "total": asserts_total,
            "non_pass": len(non_pass),
            "by_result": by_result,
        },
        # §7.4 operator 追加：run 级「返工（合计）」= Σ step.rework（int 严格传播，§4）
        "rework": rework_total(run),
        "usable": bool(steps) and bool(rows or non_pass),
    }


def exit_code_for(document):
    """看板是查询：有东西可画 → 0；`steps: 0` 或一条轮次/断言都拿不到 → 3（§8.1）。"""
    return 0 if summarize(document)["usable"] else 3


# --------------------------------------------------------------------------- #
# 格式化：三态各自写法，绝不用 0 冒充缺失（§4）
# --------------------------------------------------------------------------- #
def esc(value):
    """HTML 转义（看板里所有文本都过这里，包括从 run.json 抄来的标题）。"""
    text = value if isinstance(value, str) else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def text_of(value):
    """`None` → `null`（不适用）、`"unknown"` → `unknown`（拿不到）、真的 0 → `0`（§4）。"""
    if value is None:
        return "null"
    if value is UNKNOWN:
        return UNKNOWN
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def cost_text(cost):
    """§5.4：`cost.usd` 是下限 —— 数出来时带 `>=`，并永远把 `complete` / `unknown_rows` 摆出来。"""
    usd = cost.get("usd") if isinstance(cost, dict) else UNKNOWN
    complete = cost.get("complete") if isinstance(cost, dict) else UNKNOWN
    basis = cost.get("basis") if isinstance(cost, dict) else UNKNOWN
    unknown_rows = cost.get("unknown_rows") if isinstance(cost, dict) else UNKNOWN
    if is_num(usd):
        head = ">= %s USD" % fixed(usd, COST_DIGITS)
    else:
        head = text_of(usd)
    marks = "complete: %s / basis: %s / unknown_rows: %s" % (
        text_of(complete),
        text_of(basis),
        text_of(unknown_rows),
    )
    return {"head": head, "marks": marks}


def _round_label(row):
    return "s%s.r%s" % (text_of(row["step"]), text_of(row["seq"]))


def _round_state(row):
    """一轮的成本状态：`known` / `null`（真的不花钱）/ `unknown`（拿不到）。"""
    if is_num(row["cost"]):
        return "known"
    if row["cost"] is None:
        return "null"
    return "unknown"


# --------------------------------------------------------------------------- #
# 图 1：轮次 / 成本曲线
# --------------------------------------------------------------------------- #
def chart_rounds(rows):
    """逐轮成本柱 + 累计成本折线（累计是**已知部分之和**，即下限，§5.4）。"""
    lines = []
    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B
    base_y = PAD_T + plot_h
    count = len(rows)
    slot = float(plot_w) / count if count else float(plot_w)
    bar_w = min(36.0, slot * 0.62)

    known = [row["cost"] for row in rows if is_num(row["cost"])]
    ymax = max(known) if known else 0.0
    if ymax <= 0:
        ymax = 1.0            # 只有 0 / null / unknown 时留一条底线，但标尺上不写假的「省钱」
    cum = []
    running = 0.0
    for row in rows:
        if is_num(row["cost"]):
            running += row["cost"]
        cum.append(running)
    cum_max = max(cum) if cum else 0.0
    if cum_max <= 0:
        cum_max = 1.0

    lines.append(
        '<svg class="chart" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
        'aria-label="逐轮成本与累计成本">' % (CHART_W, CHART_H, CHART_W, CHART_H)
    )
    lines.append(
        "<defs><pattern id=\"hatch\" width=\"6\" height=\"6\" patternTransform=\"rotate(45)\" "
        "patternUnits=\"userSpaceOnUse\">"
        '<rect width="6" height="6" fill="#f4f6f7"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="#7f8c8d" stroke-width="2"/>'
        "</pattern></defs>"
    )
    # 标尺：0 / 一半 / 满格（cost 6 位定点，§7.5）
    for tick in (0.0, 0.5, 1.0):
        y = base_y - tick * plot_h
        lines.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e5e8e8" stroke-width="1"/>'
            % (PAD_L, y, PAD_L + plot_w, y)
        )
        lines.append(
            '<text x="%d" y="%.1f" class="axis" text-anchor="end">%s</text>'
            % (PAD_L - 8, y + 4, fixed(tick * ymax, COST_DIGITS))
        )
    lines.append(
        '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#95a5a6" stroke-width="1"/>'
        % (PAD_L, base_y, PAD_L + plot_w, base_y)
    )

    for index, row in enumerate(rows):
        centre = PAD_L + slot * (index + 0.5)
        state = _round_state(row)
        if state == "known":
            height = (row["cost"] / ymax) * plot_h if ymax else 0.0
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#2e86c1">'
                "<title>%s cost=%s USD</title></rect>"
                % (
                    centre - bar_w / 2,
                    base_y - height,
                    bar_w,
                    height,
                    esc(_round_label(row)),
                    fixed(row["cost"], COST_DIGITS),
                )
            )
        elif state == "null":
            # §4：null = 这一轮确实不产生费用（如 amend 记账行），画成虚线零柱 + `null`
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="none" '
                'stroke="#566573" stroke-dasharray="3 3" stroke-width="1">'
                "<title>%s cost=null（不产生费用）</title></rect>"
                % (centre - bar_w / 2, base_y - 10, bar_w, 10, esc(_round_label(row)))
            )
            lines.append(
                '<text x="%.1f" y="%.1f" class="mark" text-anchor="middle">null</text>'
                % (centre, base_y - 14)
            )
        else:
            # 拿不到就是拿不到：画整列斜纹 + `?`，**高度不代表 0 美元**
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="url(#hatch)" '
                'stroke="#7f8c8d" stroke-width="1">'
                "<title>%s cost=unknown（拿不到，不是 0）</title></rect>"
                % (centre - bar_w / 2, PAD_T, bar_w, plot_h, esc(_round_label(row)))
            )
            lines.append(
                '<text x="%.1f" y="%.1f" class="mark" text-anchor="middle">?</text>'
                % (centre, PAD_T + 14)
            )

    # 累计折线（右轴那套不做，单独按自己的最大值缩放，标题里说明口径）
    points = []
    for index, value in enumerate(cum):
        centre = PAD_L + slot * (index + 0.5)
        y = base_y - (value / cum_max) * plot_h
        points.append("%.1f,%.1f" % (centre, y))
    if len(points) > 1:
        lines.append(
            '<polyline points="%s" fill="none" stroke="#1a5276" stroke-width="2" '
            'stroke-dasharray="6 3"/>' % " ".join(points)
        )
    for index, row in enumerate(rows):
        x = PAD_L + slot * (index + 0.5)
        step = max(1, count // 10) if count > 10 else 1
        if index % step == 0 or index == count - 1:
            lines.append(
                '<text x="%.1f" y="%.1f" class="axis" text-anchor="middle" '
                'transform="rotate(-30 %.1f %.1f)">%s</text>'
                % (x, base_y + 16, x, base_y + 16, esc(_round_label(row)))
            )
    lines.append("</svg>")

    legend = [
        '<p class="legend">',
        '<span class="sw" style="background:#2e86c1"></span>逐轮 cost（USD，宿主原值）',
        '<span class="sw sw-dash"></span>累计已知部分（虚线，右轴不另标；上限 %s USD）'
        % fixed(cum_max, COST_DIGITS),
        '<span class="sw" style="background:url(#hatch)"></span>cost=unknown（拿不到，不是 0）',
        '<span class="sw sw-null"></span>cost=null（这一轮真的不产生费用）',
        "</p>",
    ]
    return lines + legend


# --------------------------------------------------------------------------- #
# 图 2：失败分布
# --------------------------------------------------------------------------- #
def chart_failures(step_counts):
    """按步画的堆叠条：`pass` 与 fail/timeout/unacked/unknown 各占多宽。

    一个人都没失败时不画「0 失败」的功劳，直接写「（无失败断言）」。
    """
    if not step_counts:
        return ['<p class="empty">无数据（steps: 0）：没有断言可分布 —— 不画「零比零」，不写「零百分比」。</p>']
    if not any(row["non_pass"] for row in step_counts):
        return ['<p class="empty">（无失败断言）：每一步的断言结果都是 pass。</p>']

    label_w = 200
    bar_w = CHART_W - label_w - 300
    row_h = 30
    height = row_h * len(step_counts) + 24
    lines = [
        '<svg class="chart" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
        'aria-label="按步的断言结果分布">' % (CHART_W, height, CHART_W, height)
    ]
    for index, row in enumerate(step_counts):
        y = 6 + index * row_h
        counts = row["counts"]
        total = row["total"] or 0
        lines.append(
            '<text x="%d" y="%.1f" class="axis" text-anchor="end">step %s %s</text>'
            % (label_w - 10, y + 16, esc(text_of(row["step"])), esc(str(row["title"]))) 
        )
        if total == 0:
            lines.append(
                '<text x="%d" y="%.1f" class="mark">（无断言 / total=null：不画分母，也不写「零比零」）</text>'
                % (label_w, y + 16)
            )
            continue
        x = float(label_w)
        for key, colour in (("pass", PASS_COLOR),) + tuple(
            (key, RESULT_COLORS[key]) for key in RESULT_ORDER
        ):
            value = counts.get(key, 0)
            if value <= 0:
                continue
            width = float(bar_w) * value / total
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="18" fill="%s">'
                "<title>step %s %s: %d/%d</title></rect>"
                % (
                    x,
                    y + 4,
                    width,
                    colour,
                    esc(text_of(row["step"])),
                    esc(RESULT_LABELS.get(key, key)),
                    value,
                    total,
                )
            )
            x += width
        lines.append(
            '<text x="%.1f" y="%.1f" class="axis">非 pass %d/%d（断言合计 %d 条）</text>'
            % (x + 8, y + 17, row["non_pass"], total, total)
        )
    lines.append("</svg>")
    legend = ['<p class="legend">']
    legend.append('<span class="sw" style="background:%s"></span>pass' % PASS_COLOR)
    for key in RESULT_ORDER:
        legend.append(
            '<span class="sw" style="background:%s"></span>%s'
            % (RESULT_COLORS[key], esc(RESULT_LABELS[key]))
        )
    legend.append("</p>")
    return lines + legend


# --------------------------------------------------------------------------- #
# 表：直接拼 HTML 单元格（每个单元格过一次 `esc()`，不经过 md 再转，避免二次转义）
# --------------------------------------------------------------------------- #
def _table_html(headers, rows, empty_note=""):
    lines = ["<table><thead><tr>"]
    lines.extend("<th>%s</th>" % header for header in headers)
    lines.append("</tr></thead><tbody>")
    if rows:
        for row in rows:
            lines.append("<tr>" + "".join("<td>%s</td>" % cell for cell in row) + "</tr>")
    elif empty_note:
        lines.append('<tr><td colspan="%d">%s</td></tr>' % (len(headers), empty_note))
    lines.append("</tbody></table>")
    return lines


ROUND_HEADERS = (
    "步", "轮", "时间", "in", "out", "reasoning", "cache_read", "cache_write",
    "rounds", "cost (USD)", "src", "note",
)
FAILURE_HEADERS = ("步", "断言", "类型", "结果", "明细")


def table_rounds_html(rows):
    """逐轮明细表：拿不到的写 `unknown`，不适用的写 `null`，真的 0 写 `0`（§4）。"""
    body = []
    for row in rows:
        if is_num(row["cost"]):
            cost = fixed(row["cost"], COST_DIGITS) + ("（est）" if row["cost_est"] is True else "")
        else:
            cost = text_of(row["cost"])
        body.append(
            [
                esc(text_of(row["step"])),
                esc(text_of(row["seq"])),
                esc(text_of(row["at"])),
                esc(text_of(row["input"])),
                esc(text_of(row["output"])),
                esc(text_of(row["reasoning"])),
                esc(text_of(row["cache_read"])),
                esc(text_of(row["cache_write"])),
                esc(text_of(row["rounds"])),
                esc(cost),
                esc(text_of(row["src"])),
                esc(text_of(row["note"])),
            ]
        )
    return _table_html(
        ROUND_HEADERS,
        body,
        "无数据：`steps[].rounds_detail` 为空，没有轮次可画 —— 不写 0 轮，也不画 0 美元柱。",
    )


def table_failures_html(rows):
    """失败断言清单：fail 是破了，timeout/unacked 是没验完，unknown 是拿不到（§5.2）。"""
    body = []
    for row in rows:
        body.append(
            [
                esc(text_of(row["step"])),
                esc(str(row["expr"])),
                esc(text_of(row["kind"])),
                esc(text_of(row["result"])),
                esc(str(row["detail"])),
            ]
        )
    return _table_html(FAILURE_HEADERS, body, "（无失败断言）：每一步的断言结果都是 pass。")


# --------------------------------------------------------------------------- #
# 组装 HTML
# --------------------------------------------------------------------------- #
STYLE = """
:root { color-scheme: light; }
body { margin: 24px auto; max-width: 960px; padding: 0 16px;
       font: 14px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; color: #1c2833; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; border-bottom: 1px solid #e5e8e8; padding-bottom: 4px; }
p.meta, p.note { color: #566573; margin: 4px 0; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.card { border: 1px solid #e5e8e8; border-radius: 6px; padding: 8px 12px; min-width: 148px; }
.card b { display: block; font-size: 18px; font-weight: 600; }
.card span { color: #566573; font-size: 12px; }
.chart { display: block; margin: 8px 0; }
.legend { color: #566573; font-size: 12px; margin: 2px 0 0; }
.legend .sw { display: inline-block; width: 12px; height: 12px; margin: 0 4px 0 12px;
              vertical-align: -2px; border-radius: 2px; }
.legend .sw-dash { background: none; border-top: 2px dashed #1a5276; height: 0; }
.legend .sw-null { background: none; border: 1px dashed #566573; }
.axis { fill: #566573; font-size: 11px; font-family: Consolas, monospace; }
.mark { fill: #7f8c8d; font-size: 11px; }
.empty { color: #566573; border: 1px dashed #bdc3c7; border-radius: 6px; padding: 10px 12px; }
.bad { color: #c0392b; }
table { border-collapse: collapse; width: 760px; font-size: 12px; }
th, td { border: 1px solid #e5e8e8; padding: 4px 6px; text-align: left; vertical-align: top; }
th { background: #f8f9f9; font-weight: 600; }
code { font-family: Consolas, monospace; }
ul { padding-left: 20px; }
"""


def _cards(summary):
    cost = cost_text(summary["cost"])
    tokens = summary["tokens"]
    if summary["steps"]:
        asserts_card = "%s/%s" % (summary["asserts"]["passed"], summary["asserts"]["total"])
        non_pass_card = text_of(summary["asserts"]["non_pass"])
        # 有步骤时才画返工合计；`steps: 0` 时「0 次返工」会被读成「没返工」，写「无数据」
        rework_card = text_of(summary["rework"])
    else:
        # §8.1：一步都没有时不许写 0/0，也不许让「0 条非 pass」看起来像「全过」
        asserts_card = "无数据（steps: 0）"
        non_pass_card = "无数据（steps: 0）"
        rework_card = "无数据（steps: 0）"
    cards = [
        ("verdict", text_of(summary["verdict"])),
        ("轮次", text_of(summary["rounds"])),
        ("tokens（in + out）", "%s + %s" % (text_of(tokens["input"]), text_of(tokens["output"]))),
        ("cost", cost["head"]),
        ("断言 passed/total", asserts_card),
        ("非 pass 断言", non_pass_card),
        ("返工（合计）", rework_card),
        ("degraded", text_of(summary["degraded"])),
    ]
    lines = ['<div class="cards">']
    for label, value in cards:
        lines.append(
            '<div class="card"><b>%s</b><span>%s</span></div>' % (esc(value), esc(label))
        )
    lines.append("</div>")
    lines.append(
        '<p class="note">cost %s（%s）。有 %s 条轮次的 cost 拿不到、%s 条确实不产生费用；'
        "拿不到的轮次画成斜纹，不做 0 美元柱。</p>"
        % (
            esc(cost["head"]),
            esc(cost["marks"]),
            text_of(summary["rounds_cost_unknown"]),
            text_of(summary["rounds_cost_null"]),
        )
    )
    lines.append(
        '<p class="note">返工（合计）= %s（%s）。'
        "它是 step 级返工的加总，不是单独一个字段 —— 没有这一行不等于没有返工。</p>"
        % (esc(text_of(summary["rework"])), esc(REWORK_TOTAL_NOTE))
    )
    return lines


def _degraded_block(document):
    run = run_section(document)
    reasons = [item for item in (run.get("degraded_reasons") or []) if isinstance(item, str)]
    lines = ["<h2>退化与来源（run.degraded_reasons / run.sources）</h2>"]
    if reasons:
        lines.append('<ul class="bad">')
        for reason in reasons:
            lines.append("<li><code>%s</code></li>" % esc(reason))
        lines.append("</ul>")
    else:
        lines.append('<p class="note">degraded_reasons 为空：这次采集没有缺口。</p>')
    sources = [item for item in (run.get("sources") or []) if isinstance(item, dict)]
    if sources:
        lines.append("<table><thead><tr><th>kind</th><th>present</th><th>readable</th><th>note</th></tr></thead><tbody>")
        for item in sources:
            lines.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    esc(text_of(item.get("kind", UNKNOWN))),
                    esc(text_of(item.get("present", UNKNOWN))),
                    esc(text_of(item.get("readable", UNKNOWN))),
                    esc(text_of(item.get("note", ""))),
                )
            )
        lines.append("</tbody></table>")
    return lines


def _dimension_of(dimensions, key):
    section = dimensions.get(key) if isinstance(dimensions, dict) else None
    return section if isinstance(section, dict) else {}


def _values_or_unknown(mapping, keys):
    return {key: (mapping.get(key, UNKNOWN) if isinstance(mapping, dict) else UNKNOWN) for key in keys}


def _cannot_support_note(dimension):
    """每块第一行写该维「不能支撑什么」（dimensions.md §6 原话照抄，不改语义）。"""
    items = NOT_SUPPORTED.get(dimension) or ()
    text = " ".join(items) if items else "unknown"
    html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(text))
    html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)
    return '<p class="note">不能支撑什么（contract/dimensions.md §6）：%s</p>' % html


# --------------------------------------------------------------------------- #
# 图 3 + 图 4：四维接入（轮间隔分布 / 覆盖表证据级分布）—— 并列，不合成总分
# --------------------------------------------------------------------------- #
def chart_round_gap(dimensions):
    """轮间隔分布：`round_gap.{p50,p90,max}` 原值，不打折、不补数（dimensions.md §1.4）。"""
    gap = _dimension_of(dimensions, "performance").get("round_gap")
    values = _values_or_unknown(gap, ("p50", "p90", "max"))
    labels = ("p50", "p90", "max")
    if not any(is_num(values[key]) for key in labels):
        return ['<p class="empty">无数据：measure.json 的 round_gap 一条样本都没有（不画 0 秒柱）。</p>']
    known = [values[key] for key in labels if is_num(values[key])]
    vmax = max(known) if known and max(known) > 0 else 1.0

    left, right, top, row_h = 96, 190, 22, 34
    height = top + row_h * len(labels) + 26
    plot_w = CHART_W - left - right
    base_x = float(left)
    lines = [
        '<svg class="chart" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
        'aria-label="轮间隔分布（p50 / p90 / max，秒）">' % (CHART_W, height, CHART_W, height),
        '<defs><pattern id="gap-hatch" width="6" height="6" patternTransform="rotate(45)" '
        'patternUnits="userSpaceOnUse"><rect width="6" height="6" fill="#f4f6f7"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="#7f8c8d" stroke-width="2"/>'
        "</pattern></defs>",
    ]
    for index, key in enumerate(labels):
        y = top + index * row_h
        value = values[key]
        lines.append(
            '<text x="%d" y="%.1f" class="axis" text-anchor="end">%s</text>'
            % (left - 10, y + 18, esc(key))
        )
        if is_num(value):
            width = plot_w * value / vmax if vmax else 0.0
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="18" fill="#1f618d">'
                "<title>round_gap.%s = %s 秒</title></rect>"
                % (base_x, y + 4, max(width, 1.0), esc(key), fixed(value, COST_DIGITS))
            )
            lines.append(
                '<text x="%.1f" y="%.1f" class="axis">%s 秒</text>'
                % (base_x + max(width, 1.0) + 8, y + 18, fixed(value, COST_DIGITS))
            )
        else:
            # 拿不到 / 样本不足：不画 0 秒柱，写「无数据」+ 原因
            reason = gap.get("p90_reason") if isinstance(gap, dict) else None
            reason_text = (
                "（p90_reason: %s）" % reason if key == "p90" and reason else ""
            )
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="18" fill="url(#gap-hatch)">'
                "<title>round_gap.%s = 无数据</title></rect>"
                % (base_x, y + 4, min(plot_w * 0.25, 120.0), esc(key))
            )
            lines.append(
                '<text x="%.1f" y="%.1f" class="mark">无数据%s</text>'
                % (base_x + min(plot_w * 0.25, 120.0) + 8, y + 18, esc(reason_text))
            )
    lines.append(
        '<text x="%d" y="%d" class="axis">n = %s（样本 = 同一步内相邻轮次的 gap_s 条数；首轮不占位）</text>'
        % (left, height - 8, esc(text_of(_values_or_unknown(gap, ("n",))["n"])))
    )
    lines.append("</svg>")
    lines.append(
        '<p class="legend">只给 p50 / p90 / max，不给均值；样本 0&lt;n&lt;5 时 p90 不给数'
        "（too_few_samples），不硬凑。</p>"
    )
    return lines


COVERAGE_LEVELS = ("mechanical", "semantic", "none", "not_applicable")
COVERAGE_COLORS = {
    "mechanical": "#1f618d",
    "semantic": "#c9a227",
    "none": "#c0392b",
    "not_applicable": "#7f8c8d",
}


def chart_coverage_levels(dimensions):
    """覆盖表证据级分布：四类**计数**并列（不是分数、不是百分比、不合成总分）。"""
    coverage = _dimension_of(dimensions, "coverage")
    counts = coverage.get("counts") if isinstance(coverage.get("counts"), dict) else {}
    values = _values_or_unknown(counts, COVERAGE_LEVELS)
    if not any(is_int(values[key]) for key in COVERAGE_LEVELS):
        return ['<p class="empty">无数据：coverage.json 的覆盖表拿不到（不画 0 计数）。</p>']
    known = [values[key] for key in COVERAGE_LEVELS if is_int(values[key])]
    vmax = max(known) if known and max(known) > 0 else 1.0

    left, right, top, row_h = 150, 150, 22, 32
    height = top + row_h * len(COVERAGE_LEVELS) + 26
    plot_w = CHART_W - left - right
    base_x = float(left)
    lines = [
        '<svg class="chart" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
        'aria-label="覆盖表证据级分布（四类计数）">' % (CHART_W, height, CHART_W, height)
    ]
    for index, key in enumerate(COVERAGE_LEVELS):
        y = top + index * row_h
        value = values[key]
        lines.append(
            '<text x="%d" y="%.1f" class="axis" text-anchor="end">%s</text>'
            % (left - 10, y + 18, esc(key))
        )
        if is_int(value):
            width = plot_w * value / vmax if vmax else 0.0
            lines.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="18" fill="%s">'
                "<title>%s: %d 条</title></rect>"
                % (base_x, y + 4, max(width, 1.0), COVERAGE_COLORS[key], esc(key), value)
            )
            lines.append(
                '<text x="%.1f" y="%.1f" class="axis">%d 条</text>'
                % (base_x + max(width, 1.0) + 8, y + 18, value)
            )
        else:
            lines.append(
                '<text x="%.1f" y="%.1f" class="mark">无数据</text>' % (base_x, y + 18)
            )
    lines.append(
        '<text x="%d" y="%d" class="axis">合计 %s 条；未登记条款 delta %s（clauses_total %s / registered %s）</text>'
        % (
            left,
            height - 8,
            esc(text_of(_values_or_unknown(counts, ("total",))["total"])),
            esc(text_of(_values_or_unknown(coverage.get("unregistered_clauses"), ("delta",))["delta"])),
            esc(text_of(_values_or_unknown(coverage.get("unregistered_clauses"), ("clauses_total",))["clauses_total"])),
            esc(text_of(_values_or_unknown(coverage.get("unregistered_clauses"), ("registered",))["registered"])),
        )
    )
    lines.append("</svg>")
    lines.append(
        '<p class="legend">覆盖表只出四类计数与逐条证据级：semantic 不算通过，'
        "not_applicable 不是 0 分。</p>"
    )
    if coverage.get("discriminating_power") not in (None, UNKNOWN):
        lines.append(
            '<p class="note">判据覆盖表自述：%s</p>'
            % esc(text_of(coverage["discriminating_power"]))
        )
    return lines


def table_interactivity_html(dimensions):
    """交互性：三个场景的 turns_to_done / 结论（`interact.json` 原值）。"""
    section = _dimension_of(dimensions, "interactivity")
    rows = [item for item in (section.get("scenarios") or []) if isinstance(item, dict)]
    body = []
    for item in rows:
        body.append(
            [
                esc(text_of(item.get("id", UNKNOWN))),
                esc(text_of(item.get("turns_to_done", UNKNOWN))),
                esc(text_of(item.get("verdict", UNKNOWN))),
            ]
        )
    return _table_html(
        ("场景", "turns_to_done", "结论"),
        body,
        "无数据：interact.json 里没有场景记录（不写 0 轮、不写 0 场景通过）。",
    )


def table_modalities_html(dimensions):
    """多模态登记表：`measured` / `not_applicable` 原值；后者写「未测」，不是 0 分。"""
    section = _dimension_of(dimensions, "modalities")
    rows = [item for item in (section.get("rows") or []) if isinstance(item, dict)]
    body = []
    for item in rows:
        body.append(
            [
                esc(text_of(item.get("modality", UNKNOWN))),
                esc(text_of(item.get("status", UNKNOWN))),
                esc(text_of(item.get("reason", UNKNOWN))),
                esc(text_of(item.get("evidence", UNKNOWN))),
            ]
        )
    return _table_html(
        ("modality", "status", "reason", "evidence"),
        body,
        "无数据：measure.json 里没有模态登记表。",
    )


def _dimensions_block(dimensions):
    """四维接入：轮间隔分布 + 覆盖表证据级分布（内联 SVG）+ 交互/多模态登记表。"""
    if not isinstance(dimensions, dict):
        dimensions = {}
    lines = ["<h2>四维并列：轮间隔分布（性能）</h2>"]
    lines.append(_cannot_support_note("performance"))
    lines.extend(chart_round_gap(dimensions))
    lines.append("<h2>四维并列：覆盖表证据级分布（判据覆盖）</h2>")
    lines.append(_cannot_support_note("coverage"))
    lines.extend(chart_coverage_levels(dimensions))
    lines.append("<h2>四维并列：交互性（三场景）</h2>")
    lines.append(_cannot_support_note("interactivity"))
    lines.extend(table_interactivity_html(dimensions))
    lines.append(
        '<p class="note">三个场景成本合计（interact.json 原值）：%s USD。</p>'
        % esc(text_of(_dimension_of(dimensions, "interactivity").get("cost", UNKNOWN)))
    )
    lines.append("<h2>四维并列：多模态登记</h2>")
    lines.append(_cannot_support_note("modalities"))
    lines.extend(table_modalities_html(dimensions))
    lines.append(
        '<p class="note">not_applicable 的模态 = 「这一版没送过该模态输入」，'
        "是未测，不是 0 分，也不是「不支持」。</p>"
    )
    return lines


def render_board(document, dimensions=None):
    """返回看板 HTML（末尾恰好一个 `\\n`、不含 `\\r`、不写时间戳/进程 id，§7.5 同口径）。"""
    run = run_section(document)
    summary = summarize(document)
    rows = rounds_of(document)
    step_counts = failure_counts(document)

    head = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>run 看板 · %s</title>" % esc(text_of(summary["run_id"])),
        "<style>%s</style>" % STYLE.strip(),
        "</head>",
        "<body>",
        "<h1>run 看板 · %s</h1>" % esc(text_of(summary["run_id"])),
        '<p class="meta">目标：%s</p>' % esc(text_of(pick(run, "goal"))),
        '<p class="meta">时间：%s → %s ｜ 模型：%s ｜ schema：%s</p>'
        % (
            esc(text_of(pick(run, "started_at"))),
            esc(text_of(pick(run, "finished_at"))),
            esc(_model_text(pick(run, "model"))),
            esc(text_of(pick(document, "schema"))),
        ),
        '<p class="note">口径：contract/trace.md §2.4 / §5.2 / §5.4 / §4 / §8.1。'
        "只吃 run.json，不读 .plan/。缺失写 unknown、不适用写 null、真的 0 才写 0。</p>",
    ]

    body = []
    body.extend(_cards(summary))
    body.extend(_dimensions_block(dimensions))

    body.append("<h2>轮次 / 成本曲线（逐轮 cost 柱 + 累计已知部分折线）</h2>")
    if not rows:
        body.append(
            '<p class="empty">无数据（`steps[].rounds_detail` 为空 / 轮次拿不到）：'
            "没有轮次可画，不画 0 轮、不画 0 美元柱。</p>"
        )
    else:
        body.extend(chart_rounds(rows))

    body.append("<h2>失败分布（每步的断言结果占比）</h2>")
    body.extend(chart_failures(step_counts))

    body.append("<h2>逐轮明细（§2.4）</h2>")
    body.extend(table_rounds_html(rows))
    body.append("<h2>失败断言清单（§5.2）</h2>")
    body.extend(table_failures_html(failures_of(document)))
    body.extend(_degraded_block(document))

    lines = head + body + ["</body>", "</html>"]
    return "\n".join(lines) + "\n"


def _model_text(model):
    if isinstance(model, dict):
        return "%s / %s（mixed: %s）" % (
            text_of(model.get("provider", UNKNOWN)),
            text_of(model.get("id", UNKNOWN)),
            text_of(model.get("mixed", UNKNOWN)),
        )
    return text_of(model)


def encode_board(document, dimensions=None):
    return render_board(document, dimensions).encode("utf-8")


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m view.board",
        description="看板：轮次/成本曲线 + 失败分布（口径 = contract/trace.md；只吃 run.json）",
    )
    parser.add_argument("--run", metavar="RUN_JSON", help="输入 run.json（只读，view/ 只吃这个）")
    parser.add_argument("--out", metavar="PATH", help="落盘 HTML（out/**）")
    parser.add_argument("--json", action="store_true", help="不画图，只吐机器摘要（与 cli report 同一张数）")
    parser.add_argument("--self-test", action="store_true", help="跑 view/fixtures/export/ 夹具自检")
    return parser


def run(argv=None):
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    if not args.run:
        return emit_error(
            ToolError(
                "missing_arg",
                "缺 --run",
                "用法：python -m view.board --run <run.json> [--out out/board.html] [--json]",
                1,
            )
        )

    try:
        refuse_plan_path(args.run)
        document = load_document(args.run)
        code = exit_code_for(document)

        if args.json:
            payload = {"ok": True, "run": run_section(document), "summary": summarize(document)}
            data = dumps(payload).encode("utf-8")
        else:
            data = encode_board(document, build_dimensions(args.run))

        if args.out:
            write_bytes_out(args.out, data)
        write_stdout_bytes(data)

        summary = summarize(document)
        note(
            "steps=%s rounds=%s (rows=%d cost_known=%d cost_unknown=%d) verdict=%s exit=%d"
            % (
                text_of(summary["steps"]),
                text_of(summary["rounds"]),
                summary["rounds_rows"],
                summary["rounds_with_cost"],
                summary["rounds_cost_unknown"],
                text_of(summary["verdict"]),
                code,
            )
        )
        if code == 3:
            note("无数据（steps: 0 或一条轮次/断言都拿不到，§8.1）：照常出图，但不许当成「全部通过」")
        return code

    except ToolError as exc:
        return emit_error(exc)
    except Exception as exc:  # noqa: BLE001 — 工具自身故障：堆栈给人看，stdout 只放 JSON
        traceback.print_exc(file=sys.stderr)
        return emit_error(ToolError("internal_error", "内部异常：%s" % exc, "见 stderr 堆栈", 1))


# --------------------------------------------------------------------------- #
# 自检：只用 view/fixtures/export/ 的编造夹具 + 手写样本，不读 runs/、不读 .plan/
# --------------------------------------------------------------------------- #
#: 有步骤但一条 `rounds_detail` 都没有的样本（钉住「无数据」而不是「0 轮」）
NO_ROUNDS_DOC = {
    "schema": 1,
    "run": {
        "id": "board-no-rounds",
        "root": "D:\\...\\view\\fixtures\\export",
        "started_at": "unknown",
        "finished_at": "unknown",
        "goal": "（示意）有步骤但一条轮次明细都没有",
        "model": "unknown",
        "rounds": "unknown",
        "rounds_coverage": {"rows_total": 0, "rows_with_reqs": 0},
        "tokens": {"input": "unknown", "output": "unknown", "reasoning": "unknown",
                   "cache_read": "unknown", "cache_write": "unknown", "complete": False},
        "cost": {"usd": "unknown", "complete": False, "basis": "unknown", "unknown_rows": 0},
        "verdict": "unknown",
        "sources": [],
        "degraded": True,
        "degraded_reasons": ["rounds.unavailable 一条轮次明细都拿不到"],
        "steps": [
            {
                "n": 1,
                "title": "空步",
                "lane": "view",
                "status": "done",
                "planned_out": "view/x.py",
                "artifacts": [],
                "asserts": {"total": None, "passed": "unknown", "failed": "unknown",
                            "timeout": "unknown", "unacked": "unknown", "items": []},
                "assert_pass": {"first_pass_rate": None, "final_pass_rate": None},
                "exit_code": None,
                "exit_code_src": None,
                "rework": 0,
                "first_failed": "unknown",
                "rounds": "unknown",
                "tokens": {"input": "unknown", "output": "unknown", "reasoning": "unknown",
                           "cache_read": "unknown", "cache_write": "unknown", "complete": False},
                "cost": {"usd": "unknown", "complete": False, "basis": "unknown",
                         "unknown_rows": 0},
                "rounds_detail": [],
            }
        ],
    },
}


def run_self_test():
    checks = []

    def check(name, actual, expected):
        checks.append((actual == expected, name, actual, expected))

    def check_true(name, value):
        check(name, bool(value), True)

    try:
        full_doc = load_document(str(FIXTURES_DIR / "full.json"))
        degen_doc = load_document(str(FIXTURES_DIR / "degenerate.json"))

        # ---- 取数：三态各回各位（§4）
        rows = rounds_of(full_doc)
        check("轮次行数 = 各步 rounds_detail 之和", len(rows), 3)
        check(
            "轮次按 (step, seq) 升序",
            [(row["step"], row["seq"]) for row in rows],
            [(1, 1), (1, 2), (2, 1)],
        )
        check_true("拿不到的轮次 cost 记 unknown（不是 0）", rows[2]["cost"] == UNKNOWN)
        check_true("数字轮次 cost 保持 number", is_num(rows[0]["cost"]))

        # ---- 失败分布：pass 不进分布明细
        fails = failures_of(full_doc)
        check_true("失败清单只收 result != pass", all(row["result"] != "pass" for row in fails))
        counts = {row["step"]: row["counts"] for row in failure_counts(full_doc)}
        check_true(
            "四种非 pass 都各计各的",
            all(key in counts[1] for key in RESULT_ORDER),
        )

        # ---- 摘要：与 run.json 的数一致，缺就 unknown
        summary = summarize(full_doc)
        check("摘要 steps = steps[] 长度", summary["steps"], 2)
        check("摘要 verdict = run.verdict", summary["verdict"], "fail")
        check_true("摘要 usable = true", summary["usable"])
        check_true(
            "摘要 rounds_cost_unknown 数得出来",
            summary["rounds_cost_unknown"] == 1,
        )
        check("缺陷体摘要 steps: 0", summarize(degen_doc)["steps"], 0)
        check_true("缺陷体摘要 usable = false", not summarize(degen_doc)["usable"])

        # ---- §7.4 operator 追加：run 级「返工（合计）」= Σ step.rework（int 严格传播）
        check("摘要 返工（合计）= Σ step.rework", summary["rework"], 2)
        check(
            "返工合计：某步拿不到 → unknown（不许跳过它按剩下的求和）",
            rework_total({"steps": [{"n": 1, "rework": 2}, {"n": 2, "rework": UNKNOWN}]}),
            UNKNOWN,
        )
        check("返工合计：steps: 0 是真 0（能数清，不写 unknown）", rework_total({"steps": []}), 0)

        # ---- 退出码（§8.1）
        check("完整样本退出码", exit_code_for(full_doc), 0)
        check("缺陷体（steps: 0）退出码", exit_code_for(degen_doc), 3)
        check("有条目但没轮次也不当成功", exit_code_for(NO_ROUNDS_DOC), 3)

        # ---- HTML：三态与「无数据」
        page = render_board(full_doc)
        check_true("HTML 有 svg 图", "<svg" in page)
        check_true("HTML 里出现 run.id", esc(text_of(summary["run_id"])) in page)
        check_true("HTML 转义（esc 把 < & > 换成实体）", esc("<a & b>") == "&lt;a &amp; b&gt;")
        check_true("拿不到 cost 的轮次在明细里写 unknown", "<td>unknown</td>" in page)
        check_true("没有 0.000000 冒充缺失", "0.000000 USD（complete: %s" % UNKNOWN not in page)
        check_true("图例声明 unknown 不是 0", "不是 0" in page)
        check_true("看板有「返工（合计）」这一格，且数 == Σ step.rework",
                   "返工（合计）" in page and "<b>2</b><span>返工（合计）</span>" in page)
        check_true("看板写明了返工合计的口径（reqs / 账本）", "reqs" in page or "账本" in page)
        check_true("无 \\r", "\r" not in page)
        check_true("末尾恰好一个 \\n", page.endswith("\n") and not page.endswith("\n\n"))

        degen_page = render_board(degen_doc)
        check_true("缺陷体写「无数据」", "无数据" in degen_page)
        check_true("缺陷体不写 0/0", "0/0" not in degen_page)
        check_true(
            "缺陷体不写 0%（宽高 attr 里的 100% 不算）",
            re.search(r"(?<![\d.])0\s*%", degen_page) is None,
        )

        no_rounds_page = render_board(NO_ROUNDS_DOC)
        check_true("有步骤但没轮次 → 无数据，不画 0 轮", "无数据" in no_rounds_page)
        check_true("有步骤但没轮次 → 一张图都不画", "<svg" not in no_rounds_page)

        # ---- §7.5 同口径：同一份输入重跑字节一致
        check_true(
            "两次渲染字节一致（HTML 与 --json）",
            encode_board(full_doc) == encode_board(full_doc)
            and dumps({"ok": True, "summary": summarize(full_doc)})
            == dumps({"ok": True, "summary": summarize(full_doc)}),
        )
        check_true(
            "看板不写时间戳/进程 id/绝对路径",
            "C:\\" not in page and "D:\\" not in page,
        )

    except ToolError as exc:  # noqa: F841 — 自检不该读坏数据
        checks.append((False, "自检读夹具失败：%s" % exc.message, None, None))

    failed = [item for item in checks if not item[0]]
    for ok, name, actual, expected in checks:
        if not ok:
            sys.stderr.write("[board][self-test] FAIL %s：actual=%r expected=%r\n" % (name, actual, expected))
    sys.stderr.write("[board][self-test] %d 项，失败 %d 项\n" % (len(checks), len(failed)))
    write_stdout_bytes(
        dumps(
            {
                "ok": not failed,
                "self_test": {
                    "checks": len(checks),
                    "failed": len(failed),
                    "failed_names": [item[1] for item in failed],
                    "fixtures": "fixtures/export",
                },
            }
        ).encode("utf-8")
    )
    return 0 if not failed else 1


def main(argv=None):
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
