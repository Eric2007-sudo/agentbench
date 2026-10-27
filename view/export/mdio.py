#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/export/mdio.py · 导出 `md`（口径 = trace.md §7.4）

三张表，**顺序固定**：

1. Run 摘要：id / 时间 / 模型 / 轮次 / in+out tokens / cost（下限带 `>=`、完整成本带 `=`，
   都标 complete）/ verdict / **返工（合计）**（= Σ `step.rework`，口径写在同一个格子里）；
2. Step 表：`n` / 标题 / 路 / 状态 / 轮次 / tokens / cost / 断言 `passed/total` / 首过率 /
   exit code / 返工；
3. 失败断言表：`step` / `expr` / `result` / `detail`（无失败则整表写一行 `（无失败断言）`）。

md 里也守 §4 的空值纪律：`"unknown"`（拿不到）写 `unknown`，`null`（不适用）写 `null`，
真的 0 写 `0` —— 不许为了「好看」把缺失抹成 0。

两条排版硬约束：
- 单元格里不出现换行（所有空白折叠成单个空格），这样任何一行都不会以 `[` 之类的东西开头
  （`nogrep:^\\[@out/fx.md` 钉的就是「md 里混进了进度行」）；
- 不写导出时间戳、不写路径、不写进程 id（§7.5 同一份输入重跑字节相同）。
"""

from __future__ import annotations

from ..dimensions import NOT_SUPPORTED
from . import (
    COST_DIGITS,
    RATE_DIGITS,
    REWORK_TOTAL_NOTE,
    UNKNOWN,
    fixed,
    is_int,
    is_num,
    norm_bool,
    norm_int,
    norm_num,
    pick,
    rework_total,
    run_section,
    steps_of,
    sub,
)

#: §5.2：只有 `result == "pass"` 才算「验过了」，其余（fail/timeout/unacked/unknown）都进失败断言表


# --------------------------------------------------------------------------- #
# 单元格
# --------------------------------------------------------------------------- #
def md_cell(value):
    """单元格安全化：折叠空白（不出续行）+ 转义表格分隔符 `|`。"""
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text.replace("|", "\\|")


def md_scalar(value):
    """§4 三态：`null` 是不适用、`unknown` 是拿不到、`0` 是真的 0。"""
    if value is None:
        return "null"
    if value is UNKNOWN:
        return UNKNOWN
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_num(value):
        return str(value) if not isinstance(value, float) else fixed(value, RATE_DIGITS)
    return str(value)


def md_int(value):
    return md_scalar(norm_int(value))


def md_rate(value):
    value = norm_num(value)
    if value is None or value is UNKNOWN:
        return md_scalar(value)
    return fixed(value, RATE_DIGITS)


def md_tokens(tokens):
    """`in / out / reasoning / cache_read / cache_write`，逐个守三态。"""
    return " / ".join(
        "%s %s" % (label, md_int(pick(tokens, key)))
        for label, key in (
            ("in", "input"),
            ("out", "output"),
            ("reasoning", "reasoning"),
            ("cache_read", "cache_read"),
            ("cache_write", "cache_write"),
        )
    )


def md_cost(cost):
    """cost 单元格：口径是「下限」时带 `>=`，完整成本带 `=`，两种都标 complete（§5.4）。

    - `complete: false`（有行拿不到 cost）→ `>= 0.009100 USD`：这是**下限**，不是全账；
    - `complete: true`（每一行都有 cost）→ `= 0.009100 USD`：数得清就别再写成「至少」，
      否则读者没法区分「确实这么多」和「只知道这么多」。
    """
    usd = norm_num(pick(cost, "usd"))
    complete = norm_bool(pick(cost, "complete"))
    if usd is None:
        head = "null"                      # 已知不产生费用
    elif usd is UNKNOWN:
        head = UNKNOWN                     # 拿不到，不许算成 0
    else:
        head = "%s %s USD" % ("=" if complete is True else ">=", fixed(usd, COST_DIGITS))
    marks = "complete: %s / basis: %s / unknown_rows: %s" % (
        md_scalar(complete),
        md_scalar(pick(cost, "basis")),
        md_scalar(norm_int(pick(cost, "unknown_rows"))),
    )
    return "%s（%s）" % (head, marks)


def md_model(model):
    if isinstance(model, dict):
        provider = md_scalar(pick(model, "provider"))
        identifier = md_scalar(pick(model, "id"))
        mixed = md_scalar(pick(model, "mixed"))
        return "%s / %s（mixed: %s）" % (provider, identifier, mixed)
    return md_scalar(model)


def md_asserts(asserts):
    """断言列：`passed/total`，两侧各自守三态。"""
    return "%s/%s" % (md_int(pick(asserts, "passed")), md_int(pick(asserts, "total")))


# --------------------------------------------------------------------------- #
# 三张表
# --------------------------------------------------------------------------- #
def _summary_block(run):
    coverage = sub(run, "rounds_coverage")
    return [
        "## 1. Run 摘要",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
        "| id | %s |" % md_cell(md_scalar(pick(run, "id"))),
        "| 时间 | %s → %s |"
        % (md_cell(md_scalar(pick(run, "started_at"))), md_cell(md_scalar(pick(run, "finished_at")))),
        "| 模型 | %s |" % md_cell(md_model(pick(run, "model"))),
        "| 轮次 | %s（rows_total %s / rows_with_reqs %s） |"
        % (
            md_cell(md_scalar(norm_num(pick(run, "rounds")))),
            md_cell(md_scalar(norm_int(pick(coverage, "rows_total")))),
            md_cell(md_scalar(norm_int(pick(coverage, "rows_with_reqs")))),
        ),
        "| tokens（in + out） | %s + %s |"
        % (
            md_cell(md_int(pick(sub(run, "tokens"), "input"))),
            md_cell(md_int(pick(sub(run, "tokens"), "output"))),
        ),
        "| cost | %s |" % md_cell(md_cost(sub(run, "cost"))),
        "| verdict | %s |" % md_cell(md_scalar(pick(run, "verdict"))),
        # §7.4 operator 追加：run 级「返工（合计）」。口径必须与数写在同一行 ——
        # `rounds` 有 run 级合计、`rework` 只有 step 级时，读者会自己把 Step 表加一遍，
        # 而「没有这一行」看起来就像「没有返工」。
        "| 返工（合计） | %s（%s） |"
        % (md_cell(md_int(rework_total(run))), md_cell(REWORK_TOTAL_NOTE)),
        "",
    ]


def _steps_block(document):
    lines = [
        "## 2. Step 表",
        "",
        "| n | 标题 | 路 | 状态 | 轮次 | tokens | cost | 断言 | 首过率 | exit code | 返工 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    run = run_section(document)
    steps = steps_of(run)
    if not steps:
        # steps: 0 是「真的 0 步」（§8.1），不是 unknown；但也不许写成「全部通过」，
        # 也不许留空单元格 —— 整行显式写 null，让「没有这回事」看得见、数得清。
        lines.append(
            "| （无步骤） | null | null | null | null | null | null | null | null | null | null |"
        )
    for step in steps:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                md_cell(md_int(pick(step, "n"))),
                md_cell(md_scalar(pick(step, "title"))),
                md_cell(md_scalar(pick(step, "lane"))),
                md_cell(md_scalar(pick(step, "status"))),
                md_cell(md_int(pick(step, "rounds"))),
                md_cell(md_tokens(sub(step, "tokens"))),
                md_cell(md_cost(sub(step, "cost"))),
                md_cell(md_asserts(sub(step, "asserts"))),
                md_cell(md_rate(pick(sub(step, "assert_pass"), "first_pass_rate"))),
                md_cell(md_int(pick(step, "exit_code"))),
                md_cell(md_int(pick(step, "rework"))),
            )
        )
    lines.append("")
    return lines


def _failures_block(document):
    lines = [
        "## 3. 失败断言表",
        "",
        "只有 result 不是 pass 的断言才进这张表：fail 是「破了」，timeout 与 unacked 是「没验完」"
        "（§5.2 要求与 fail 分开说），unknown 是「拿不到」。",
        "",
        "| step | expr | result | detail |",
        "| --- | --- | --- | --- |",
    ]
    rows = []
    for step in steps_of(run_section(document)):
        number = md_int(pick(step, "n"))
        for item in sub(step, "asserts").get("items") or []:
            if not isinstance(item, dict):
                continue
            result = item.get("result", UNKNOWN)
            if result == "pass":
                continue
            rows.append(
                "| %s | %s | %s | %s |"
                % (
                    md_cell(number),
                    md_cell(md_scalar(item.get("expr", UNKNOWN))),
                    md_cell(md_scalar(result)),
                    md_cell(md_scalar(item.get("detail", ""))),
                )
            )
    if rows:
        lines.extend(rows)
    else:
        lines.append("| （无失败断言） | | | |")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# 四维（并列，不合成总分）：性能 / 覆盖 / 交互 / 多模态（dimensions.md §0–§4）
# --------------------------------------------------------------------------- #
def _not_supported_lines(dimension):
    """每节**正文第一行**写该维「不能支撑什么」—— §6 原话照抄，不改语义。"""
    items = NOT_SUPPORTED.get(dimension) or ()
    if not items:
        return ["> 不能支撑什么：unknown（contract/dimensions.md §6 没有登记该维）。"]
    lines = ["> 不能支撑什么（contract/dimensions.md §6，原话照抄）：%s" % items[0]]
    lines.extend("> %s" % item for item in items[1:])
    return lines


def _dimension_of(dimensions, key):
    section = dimensions.get(key) if isinstance(dimensions, dict) else None
    return section if isinstance(section, dict) else {}


def _dim_text(section, key):
    """段内字段：缺 / 拿不到 → `unknown`；不适用 → `null`；真的 0 才是 0（§4）。"""
    value = section.get(key, UNKNOWN)
    return md_scalar(value)


def _cost_value(value):
    """交互成本照抄原值（`interact.json` 就是这么写的），不按均价重算、不做 6 位截断。"""
    if is_num(value):
        return repr(value)
    return md_scalar(value)


def _gap_text(gap, key):
    value = gap.get(key, UNKNOWN) if isinstance(gap, dict) else UNKNOWN
    text = md_scalar(value)
    if key == "p90" and text == UNKNOWN and isinstance(gap, dict) and gap.get("p90_reason"):
        return "%s（p90_reason: %s）" % (text, md_cell(md_scalar(gap["p90_reason"])))
    return text


def _performance_block(dimensions):
    section = _dimension_of(dimensions, "performance")
    gap = section.get("round_gap")
    gap = gap if isinstance(gap, dict) else {}
    lines = ["## 4. 运行性能", ""]
    lines.extend(_not_supported_lines("performance"))
    lines.extend(
        [
            "",
            "| 字段 | 值 | 口径（全部来自 measure.json 原值） |",
            "| --- | --- | --- |",
            "| wall_s（秒） | %s | run 级墙钟，含网络与中转站排队 |" % md_cell(_dim_text(section, "wall_s")),
            "| turns | %s | 有起点时刻的 round 条数（不是 Σ reqs） |"
            % md_cell(_dim_text(section, "turns")),
            "| out_tokens_per_wall_s | %s | run.tokens.output ÷ wall_s（工具输出速率，不是模型 token/s） |"
            % md_cell(_dim_text(section, "out_tokens_per_wall_s")),
            "| round_gap.n | %s | 样本 = 同一步内相邻轮次的 gap_s 条数 |" % md_cell(_gap_text(gap, "n")),
            "| round_gap.p50 | %s | 秒；样本 0<n<5 时只给 p50 与 max |" % md_cell(_gap_text(gap, "p50")),
            "| round_gap.p90 | %s | 秒；样本不足 5 条就不给数 |" % md_cell(_gap_text(gap, "p90")),
            "| round_gap.max | %s | 秒 |" % md_cell(_gap_text(gap, "max")),
            "| ttft_s | %s | 结构上拿不到（§1.3），不估算 |" % md_cell(_dim_text(section, "ttft_s")),
            "| tool_calls | %s | 结构上拿不到（§1.3），不估算 |" % md_cell(_dim_text(section, "tool_calls")),
            "",
        ]
    )
    return lines


def _coverage_block(dimensions):
    section = _dimension_of(dimensions, "coverage")
    counts = section.get("counts") if isinstance(section.get("counts"), dict) else {}
    clauses = section.get("unregistered_clauses")
    clauses = clauses if isinstance(clauses, dict) else {}
    lines = ["## 5. 判据覆盖", ""]
    lines.extend(_not_supported_lines("coverage"))
    lines.extend(
        [
            "",
            "| 证据级 | 计数 | 含义 |",
            "| --- | --- | --- |",
            "| mechanical | %s | 有可执行断言，失败会让门禁变红 |" % md_cell(_dim_text(counts, "mechanical")),
            "| semantic | %s | 只有 jev 概率或人工判断，不进门禁 |" % md_cell(_dim_text(counts, "semantic")),
            "| none | %s | 完全没覆盖 |" % md_cell(_dim_text(counts, "none")),
            "| not_applicable | %s | 本次不适用（不是 0 分） |"
            % md_cell(_dim_text(counts, "not_applicable")),
            "| 合计 | %s | 覆盖表只出原始计数与逐条证据级，不合成单一结论 |" % md_cell(_dim_text(counts, "total")),
            "",
            "未登记条款：clauses_total %s / registered %s / delta %s。"
            % (
                md_cell(_dim_text(clauses, "clauses_total")),
                md_cell(_dim_text(clauses, "registered")),
                md_cell(_dim_text(clauses, "delta")),
            ),
        ]
    )
    delta = clauses.get("delta")
    if is_int(delta) and delta > 0:
        # dimensions.md §2.4：delta > 0 时同一页必须写这句话（该词只许用在这里）
        lines.append("判据覆盖表自述：%s —— 未登记条款 delta > 0，覆盖表对剩余条款**不具备判别力**。"
                     % md_cell(_dim_text(section, "discriminating_power")))
    lines.append("")
    return lines


def _interactivity_block(dimensions):
    section = _dimension_of(dimensions, "interactivity")
    scenarios = [item for item in (section.get("scenarios") or []) if isinstance(item, dict)]
    lines = ["## 6. 交互性", ""]
    lines.extend(_not_supported_lines("interactivity"))
    lines.extend(
        [
            "",
            "| 场景 | turns_to_done | 结论 |",
            "| --- | --- | --- |",
        ]
    )
    if scenarios:
        for item in scenarios:
            lines.append(
                "| %s | %s | %s |"
                % (
                    md_cell(_dim_text(item, "id")),
                    md_cell(_dim_text(item, "turns_to_done")),
                    md_cell(_dim_text(item, "verdict")),
                )
            )
    else:
        lines.append("| （无数据） | unknown | unknown |")
    lines.extend(
        [
            "",
            "三个场景成本合计（interact.json 原值）：%s USD。"
            % md_cell(_cost_value(section.get("cost", UNKNOWN))),
            "",
        ]
    )
    return lines


def _modalities_block(dimensions):
    section = _dimension_of(dimensions, "modalities")
    rows = [item for item in (section.get("rows") or []) if isinstance(item, dict)]
    lines = ["## 7. 多模态", ""]
    lines.extend(_not_supported_lines("modalities"))
    lines.extend(
        [
            "",
            "| modality | status | reason | evidence |",
            "| --- | --- | --- | --- |",
        ]
    )
    if rows:
        for item in rows:
            lines.append(
                "| %s | %s | %s | %s |"
                % (
                    md_cell(_dim_text(item, "modality")),
                    md_cell(_dim_text(item, "status")),
                    md_cell(_dim_text(item, "reason")),
                    md_cell(_dim_text(item, "evidence")),
                )
            )
    else:
        lines.append("| （无数据） | unknown | unknown | unknown |")
    lines.extend(
        [
            "",
            "未测的模态一律写「未测 —— 登记为 not_applicable，原因见 reason」；"
            "不写成 0 分，也不写成「不支持」。",
            "",
        ]
    )
    return lines


def _dimensions_block(dimensions):
    """四节并列追加（§4 性能 / §5 覆盖 / §6 交互 / §7 多模态）。`dimensions` 为 None 时不出。"""
    if not isinstance(dimensions, dict):
        return []
    lines = []
    lines.extend(_performance_block(dimensions))
    lines.extend(_coverage_block(dimensions))
    lines.extend(_interactivity_block(dimensions))
    lines.extend(_modalities_block(dimensions))
    return lines


# --------------------------------------------------------------------------- #
# 出口
# --------------------------------------------------------------------------- #
def render_md(document, dimensions=None):
    """返回 md 导出的文本（末尾恰好一个 `\\n`，无 `\\r`，无 BOM）。"""
    run = run_section(document)
    lines = [
        "# run 导出 · %s" % md_cell(md_scalar(pick(run, "id"))),
        "",
        "口径：contract/trace.md §7.4。缺失值写 unknown，不适用写 null，真的 0 写 0；"
        "cost 是下限时带 >=、完整时带 =，并标 complete。",
        "",
    ]
    lines.extend(_summary_block(run))
    lines.extend(_steps_block(document))
    lines.extend(_failures_block(document))
    lines.extend(_dimensions_block(dimensions))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def encode_md(document, dimensions=None):
    return render_md(document, dimensions).encode("utf-8")


__all__ = [
    "encode_md",
    "md_asserts",
    "md_cell",
    "md_cost",
    "md_int",
    "md_model",
    "md_rate",
    "md_scalar",
    "md_tokens",
    "render_md",
]
