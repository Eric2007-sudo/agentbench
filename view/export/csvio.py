#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/export/csvio.py · 导出 `csv`（口径 = trace.md §7.3）

一行一个 step，表头顺序固定（照抄契约，不许改）：

```
run_id,step,title,lane,status,rounds,in,out,reasoning,cache_read,cache_write,
cost_usd,cost_complete,cost_basis,asserts_total,asserts_passed,asserts_failed,
asserts_timeout,first_pass_rate,final_pass_rate,exit_code,exit_code_src,
rework,first_failed,verdict
```

单元格三条规矩（§4 / §7.3）：

| JSON 里的值 | CSV 单元格 | 理由 |
|---|---|---|
| `"unknown"`（拿不到） | `unknown` | 字面量，可被 grep 出来、不会被当成 0 |
| `null`（不适用，如 `exit_code: null`、`asserts_total: null`） | 空字符串 `""` | CSV 里必须能和 `unknown` 区分开 |
| 真的 `0` | `0` | 这一步真的没返工 / 这一轮真的不花钱 |

**任何情况下都不许把缺失写成 `0`** —— 那会被读表的人看成「完美」。

每一列取哪一级的数（一行一个 step，所以**每个 step 级列都取这个 step 自己的值**）：

- `run_id` / `verdict` 是 run 级字段，每行**复制**同一份 `run.run_id` / `run.verdict`；
  `verdict` 是「这次 run 的结论」（`run.conclusion`），**不是**「这一步的结论」——
  步级判分看 `asserts_*` / `first_pass_rate` / `exit_code`，别把 run 的结论读成「这步过了」。
- `cost_usd` / `cost_complete` / `cost_basis` 取 `step.cost`，**不是** `run.cost`：run 级聚合
  在 md 摘要里（`fx-rework01` 正好证伪得动 —— `run.cost.complete` 是 `false`（`unknown_rows: 1`），
  但步 1 的 cost 是完整的，把 run 的数抄进这一格就是「把别人的数写进这一格」）。

编码 `utf-8-sig`（Excel 直接开不乱码）、行尾 `\n`、逗号分隔、
含逗号/引号/换行的字段按 RFC4180 加引号。
"""

from __future__ import annotations

from . import (
    COST_DIGITS,
    RATE_DIGITS,
    UNKNOWN,
    fixed,
    is_num,
    norm_int,
    norm_num,
    pick,
    run_section,
    steps_of,
    sub,
)

HEADER = (
    "run_id", "step", "title", "lane", "status",
    "rounds", "in", "out", "reasoning", "cache_read", "cache_write",
    "cost_usd", "cost_complete", "cost_basis",
    "asserts_total", "asserts_passed", "asserts_failed", "asserts_timeout",
    "first_pass_rate", "final_pass_rate",
    "exit_code", "exit_code_src", "rework", "first_failed", "verdict",
)

#: §7.3 编码 utf-8-sig：BOM 由 `encode_csv()` 加在字节层
BOM = "\ufeff"


# --------------------------------------------------------------------------- #
# 单元格
# --------------------------------------------------------------------------- #
def cell_int(value):
    """int 类：`None` → `""`（不适用）；`"unknown"` → `unknown`；别的照抄整数。"""
    if value is None:
        return ""
    if value is UNKNOWN:
        return UNKNOWN
    return str(value)


def cell_cost(value):
    """cost.usd：定点 6 位；`null` = 这一轮确实不产生费用 → `""`。"""
    if value is None:
        return ""
    if not is_num(value):
        return UNKNOWN
    return fixed(value, COST_DIGITS)


def cell_rate(value):
    """比率：定点 6 位；`null` = 分母不适用 → `""`（绝不写 `0`、`0%`）。"""
    if value is None:
        return ""
    if not is_num(value):
        return UNKNOWN
    return fixed(value, RATE_DIGITS)


def cell_bool(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return UNKNOWN


def cell_text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        return UNKNOWN
    return value


def quote(field):
    """RFC4180：含逗号/引号/换行的字段加引号，内部引号翻倍。"""
    if any(ch in field for ch in (",", '"', "\r", "\n")):
        return '"' + field.replace('"', '""') + '"'
    return field


# --------------------------------------------------------------------------- #
# 行
# --------------------------------------------------------------------------- #
def row_for(document, step):
    """一个 step → 一行（列序 = `HEADER`）。"""
    run = run_section(document)
    tokens = sub(step, "tokens")
    cost = sub(step, "cost")
    asserts = sub(step, "asserts")
    assert_pass = sub(step, "assert_pass")
    return [
        cell_text(pick(run, "id")),
        cell_int(norm_int(pick(step, "n"))),
        cell_text(pick(step, "title")),
        cell_text(pick(step, "lane")),
        cell_text(pick(step, "status")),
        cell_int(norm_int(pick(step, "rounds"))),
        cell_int(norm_int(pick(tokens, "input"))),
        cell_int(norm_int(pick(tokens, "output"))),
        cell_int(norm_int(pick(tokens, "reasoning"))),
        cell_int(norm_int(pick(tokens, "cache_read"))),
        cell_int(norm_int(pick(tokens, "cache_write"))),
        cell_cost(norm_num(pick(cost, "usd"))),
        cell_bool(pick(cost, "complete")),
        cell_text(pick(cost, "basis")),
        cell_int(norm_int(pick(asserts, "total"))),
        cell_int(norm_int(pick(asserts, "passed"))),
        cell_int(norm_int(pick(asserts, "failed"))),
        cell_int(norm_int(pick(asserts, "timeout"))),
        cell_rate(norm_num(pick(assert_pass, "first_pass_rate"))),
        cell_rate(norm_num(pick(assert_pass, "final_pass_rate"))),
        cell_int(norm_int(pick(step, "exit_code"))),
        cell_text(pick(step, "exit_code_src")),
        cell_int(norm_int(pick(step, "rework"))),
        cell_bool(pick(step, "first_failed")),
        cell_text(pick(run, "verdict")),
    ]


def rows_for(document):
    """所有 step 的行（steps 按 `n` 升序，§7.5）。"""
    run = run_section(document)
    return [row_for(document, step) for step in steps_of(run)]


def render_csv(document):
    """返回 csv 导出的**文本**（不含 BOM；BOM 在 `encode_csv()` 里加）。"""
    lines = [",".join(HEADER)]
    for row in rows_for(document):
        lines.append(",".join(quote(field) for field in row))
    return "\n".join(lines) + "\n"


def encode_csv(document):
    """csv 导出的字节：`utf-8-sig`（带 BOM）、行尾 `\\n`。"""
    return (BOM + render_csv(document)).encode("utf-8")


__all__ = [
    "BOM",
    "HEADER",
    "cell_bool",
    "cell_cost",
    "cell_int",
    "cell_rate",
    "cell_text",
    "encode_csv",
    "quote",
    "render_csv",
    "row_for",
    "rows_for",
]
