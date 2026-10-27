#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/export/__main__.py · `python -m view.export`（导出 json / csv / md）+ `--self-test`

用法：

    python -m view.export --run runs/fx-rework01/run.json --format csv  --out out/fx.csv
    python -m view.export --run runs/fx-rework01/run.json --format md   --out out/fx.md
    python -m view.export --run runs/fx-rework01/run.json --format json --out out/fx.json
    python -m view.export --self-test

出口纪律（trace.md §7.1 / §7.2）：
- stdout 只有产物本身，人话、警告、错误解释全部走 stderr；
- stdout 与 `--out` 落盘**字节一致**（所以两者写同一份 bytes）；
- 退出码：`0` 跑完（导出是查询）；`3` 数据不可用（`steps: 0`，§8.1）但已输出合法产物；
  `1` 工具自身故障（参数错、写盘失败、内部异常）。
"""

from __future__ import annotations

import csv as csvlib
import io
import json
import sys
import tempfile
import traceback
from pathlib import Path

from . import (
    FORMATS,
    UNKNOWN,
    JsonArgumentParser,
    ToolError,
    emit_error,
    load_document,
    norm_int,
    norm_num,
    note,
    pick,
    refuse_plan_path,
    run_section,
    steps_of,
    sub,
    write_bytes_out,
    write_stdout_bytes,
)
from .csvio import HEADER, encode_csv, render_csv
from .jsonio import encode_json, render_json
from .mdio import encode_md, render_md
from ..dimensions import build_dimensions

#: 自检夹具（本目录自己的假样本，不挂在 `runs/` 上）
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "export"

#: §7.3 表头原文，钉住顺序（改这里 = 改契约，先改 contract/trace.md）
CONTRACT_HEADER = (
    "run_id,step,title,lane,status,rounds,in,out,reasoning,cache_read,cache_write,"
    "cost_usd,cost_complete,cost_basis,asserts_total,asserts_passed,asserts_failed,"
    "asserts_timeout,first_pass_rate,final_pass_rate,exit_code,exit_code_src,"
    "rework,first_failed,verdict"
)


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_bytes(document, fmt, dimensions=None):
    """格式 → 落盘/写 stdout 的字节（两者用同一份 bytes，才不会「看起来一样」）。"""
    if fmt == "csv":
        return encode_csv(document)
    if fmt == "md":
        return encode_md(document, dimensions)
    if fmt == "json":
        return encode_json(document)
    raise ToolError("bad_format", "不认识的 --format：%s" % fmt, "只支持 " + " / ".join(FORMATS), 1)


def exit_code_for(document):
    """§7.1：导出是查询 → 0；数据不可用（`steps: 0`，§8.1 的形状）→ 3。"""
    if not steps_of(run_section(document)):
        return 3
    return 0


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    parser = JsonArgumentParser(
        prog="python -m view.export",
        description="把 run.json 导出成 json / csv / md（口径 = contract/trace.md §7.2–7.5）",
    )
    parser.add_argument("--run", metavar="RUN_JSON", help="输入 run.json（只读，view/ 只吃这个）")
    parser.add_argument(
        "--format",
        choices=list(FORMATS),
        default="json",
        help="导出格式（默认 json）",
    )
    parser.add_argument("--out", metavar="PATH", help="落盘路径（out/**）；与 stdout 字节一致")
    parser.add_argument("--self-test", action="store_true", help="跑 view/fixtures/export/ 夹具自检")
    return parser


def run(argv=None):
    # 用法错误一律走统一出口（JSON + 退出码 1），不留 argparse 默认的 exit(2)：
    # 2 在 §7.1 里是「结论 fail/regressed」，撞车就分不清「打错参数」和「真失败」。
    try:
        args = build_parser().parse_args(argv)
    except ToolError as exc:
        return emit_error(exc)

    if args.self_test:
        return run_self_test()

    if not args.run:
        return emit_error(
            ToolError(
                "missing_arg",
                "缺 --run",
                "用法：python -m view.export --run <run.json> --format {json,csv,md} [--out <path>]",
                1,
            )
        )

    try:
        document = load_document(args.run)
        # json 导出 = run.json 原文（§7.2 判据 A），四维**不**混进来；
        # 四维单独落 out/dimensions.json（`python -m view.cli dimensions`）。
        dimensions = build_dimensions(args.run) if args.format == "md" else None
        data = render_bytes(document, args.format, dimensions)
        if args.out:
            location = write_bytes_out(args.out, data)
        write_stdout_bytes(data)               # 与落盘字节一致（§7.2）
        code = exit_code_for(document)
        note(
            "format=%s bytes=%d out=%s steps=%d verdict=%s exit=%d"
            % (
                args.format,
                len(data),
                args.out or "<stdout>",
                len(steps_of(run_section(document))),
                run_section(document).get("verdict", UNKNOWN),
                code,
            )
        )
        if code == 3:
            note("数据不可用（steps: 0，见 trace.md §8.1）：产物照常输出，但不许当成「全部通过」")
        return code

    except ToolError as exc:
        return emit_error(exc)
    except Exception as exc:  # noqa: BLE001 — 工具自身故障：堆栈给人看，stdout 只放 JSON
        traceback.print_exc(file=sys.stderr)
        return emit_error(ToolError("internal_error", "内部异常：%s" % exc, "见 stderr 堆栈", 1))


# --------------------------------------------------------------------------- #
# 交叉校验：同一份 run.json 的三种导出 → 同一张「数」的表，再逐格比
# --------------------------------------------------------------------------- #
#: 一行比这几个数：n / rounds / cost_usd / asserts_total / asserts_passed
CROSS_COLUMNS = ("n", "rounds", "cost_usd", "asserts_total", "asserts_passed")

#: md 单元格里转义过的 `|` 先换成它，切完再换回来
_MD_PIPE = "\x00"


def _md_row_cells(line):
    """按 `|` 切一行 md 表（保护单元格里转义过的 `\\|`）。"""
    text = line.strip()
    if not (text.startswith("|") and text.endswith("|")):
        return []
    text = text.replace("\\|", _MD_PIPE)[1:-1]
    return [part.strip().replace(_MD_PIPE, "|") for part in text.split("|")]


def _cell_number(value):
    """csv / md 单元格 → 数。三种空值各回各的：`""` → `None`、`unknown` → `unknown`。"""
    if value == "":
        return None
    if value == UNKNOWN:
        return UNKNOWN
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _md_number(value):
    """md 单元格 → 数：md 里**不适用**写的是 `null`（对应 csv 的 `""`），拿不到写 `unknown`。"""
    return None if value == "null" else _cell_number(value)


def _md_cost_number(text):
    """`>= 0.009100 USD（complete: …）` / `= 0.001000 USD（…）` → 数；`unknown（…）` → `unknown`。"""
    head = text.split("（", 1)[0].strip()
    if head.startswith(">="):
        head = head[2:].strip()
    elif head.startswith("="):
        head = head[1:].strip()
    if head.endswith("USD"):
        head = head[:-3].strip()
    return _md_number(head)


def _md_asserts_pair(text):
    """`2/2` → `(2, 2)`；两侧各自守三态。"""
    left, _, right = text.partition("/")
    return _md_number(left.strip()), _md_number(right.strip())


def _step_key(number):
    return ("n", number) if isinstance(number, int) else ("x", str(number))


def _record(table, number, values):
    table[_step_key(number)] = dict(zip(CROSS_COLUMNS, values))


def numbers_from_document(document):
    """run.json → `{step: {n, rounds, cost_usd, asserts_total, asserts_passed}}`。"""
    table = {}
    for step in steps_of(run_section(document)):
        number = norm_int(pick(step, "n"))
        cost = sub(step, "cost")
        asserts = sub(step, "asserts")
        _record(table, number, (
            number,
            norm_int(pick(step, "rounds")),
            norm_num(pick(cost, "usd")),
            norm_int(pick(asserts, "total")),
            norm_int(pick(asserts, "passed")),
        ))
    return table


def numbers_from_csv(text):
    """csv 导出的文本 → 同一张表（从渲染结果反解，不是从 document 抄）。"""
    index = {name: position for position, name in enumerate(HEADER)}
    table = {}
    for row in list(csvlib.reader(io.StringIO(text)))[1:]:
        if len(row) != len(HEADER):
            continue
        number = _cell_number(row[index["step"]])
        _record(table, number, (
            number,
            _cell_number(row[index["rounds"]]),
            _cell_number(row[index["cost_usd"]]),
            _cell_number(row[index["asserts_total"]]),
            _cell_number(row[index["asserts_passed"]]),
        ))
    return table


def numbers_from_md(text):
    """md 导出的文本 → 同一张表（只认 `## 2. Step 表` 那一段）。"""
    table = {}
    inside = False
    for line in text.split("\n"):
        if line.startswith("## "):
            inside = line.startswith("## 2. Step 表")
            continue
        if not inside:
            continue
        cells = _md_row_cells(line)
        if len(cells) != 11 or cells[0] in ("n", "---"):
            continue
        number = _cell_number(cells[0])
        if not isinstance(number, int):
            continue                      # 0 步时那一行写「（无步骤）」，没有数可对
        passed, total = _md_asserts_pair(cells[7])
        _record(table, number, (
            number,
            _md_number(cells[4]),
            _md_cost_number(cells[6]),
            total,
            passed,
        ))
    return table


def cross_check(document):
    """json / csv / md 三份导出转成同一张「数」的表再比（covers 备注 ④）。"""
    tables = {
        "json": numbers_from_document(document),
        "csv": numbers_from_csv(render_csv(document)),
        "md": numbers_from_md(render_md(document)),
    }
    reference = tables["json"]
    mismatches = []
    for name in ("csv", "md"):
        other = tables[name]
        for key in sorted(set(reference) | set(other), key=str):
            if reference.get(key) != other.get(key):
                mismatches.append(
                    "%s 与 json 不一致（step %s）：%r ← %r"
                    % (name, key[1], reference.get(key), other.get(key))
                )
    return {
        "rows": len(reference),
        "numbers": len(reference) * len(CROSS_COLUMNS),
        "columns": list(CROSS_COLUMNS),
        "mismatches": mismatches,
    }


# --------------------------------------------------------------------------- #
# 自检：只用 view/fixtures/export/ 的编造夹具 + 手写样本
# --------------------------------------------------------------------------- #
#: 没有任何非 pass 断言的最小样本（钉住「（无失败断言）」那一行）
CLEAN_DOC = {
    "schema": 1,
    "run": {
        "id": "export-clean",
        "root": "D:\\...\\view\\fixtures\\export",
        "started_at": "2026-01-01T00:00:00+08:00",
        "finished_at": "2026-01-01T00:10:00+08:00",
        "goal": "（示意）没有失败断言的极小样本",
        "model": "unknown",
        "rounds": 1,
        "rounds_coverage": {"rows_total": 1, "rows_with_reqs": 1},
        "tokens": {"input": 1000, "output": 100, "reasoning": 0,
                   "cache_read": 0, "cache_write": 0, "complete": True},
        "cost": {"usd": 0.001, "complete": True, "basis": "host", "unknown_rows": 0},
        "verdict": "pass",
        "sources": [],
        "degraded": False,
        "degraded_reasons": [],
        "steps": [
            {
                "n": 1,
                "title": "干净步",
                "lane": "view",
                "status": "done",
                "planned_out": "view/x.py",
                "artifacts": [],
                "asserts": {
                    "total": 2, "passed": 2, "failed": 0, "timeout": 0, "unacked": 0,
                    "items": [
                        {"expr": "file:exists:view/x.py", "kind": "exists", "result": "pass", "detail": ""},
                        {"expr": "ack:有人看过", "kind": "ack", "result": "pass", "detail": ""},
                    ],
                },
                "assert_pass": {"first_pass_rate": 1.0, "final_pass_rate": 1.0},
                "exit_code": 0,
                "exit_code_src": "gate",
                "rework": 0,
                "first_failed": False,
                "rounds": 1,
                "tokens": {"input": 1000, "output": 100, "reasoning": 0,
                           "cache_read": 0, "cache_write": 0, "complete": True},
                "cost": {"usd": 0.001, "complete": True, "basis": "host", "unknown_rows": 0},
                "rounds_detail": [],
            }
        ],
    },
}


#: 关键字段全 `"unknown"` 的样本：csv 里必须写 `unknown`，**一个 0 都不许冒出来**
ALL_UNKNOWN_DOC = {
    "schema": 1,
    "run": {
        "id": "export-all-unknown",
        "root": "D:\\...\\view\\fixtures\\export",
        "started_at": "unknown",
        "finished_at": "unknown",
        "goal": "（示意）关键字段全 unknown 的退化样本",
        "model": {"provider": "unknown", "id": "unknown", "mixed": "unknown"},
        "rounds": "unknown",
        "rounds_coverage": {"rows_total": 0, "rows_with_reqs": 0},
        "tokens": {"input": "unknown", "output": "unknown", "reasoning": "unknown",
                   "cache_read": "unknown", "cache_write": "unknown", "complete": False},
        "cost": {"usd": "unknown", "complete": False, "basis": "unknown", "unknown_rows": 0},
        "verdict": "unknown",
        "sources": [],
        "degraded": True,
        "degraded_reasons": ["ledger.unreadable"],
        "steps": [
            {
                "n": 1,
                "title": "全 unknown 步",
                "lane": "view",
                "status": "unknown",
                "planned_out": "unknown",
                "artifacts": [],
                "asserts": {"total": "unknown", "passed": "unknown", "failed": "unknown",
                            "timeout": "unknown", "unacked": "unknown", "items": []},
                "assert_pass": {"first_pass_rate": "unknown", "final_pass_rate": "unknown"},
                "exit_code": "unknown",
                "exit_code_src": "unknown",
                "rework": 0,                      # 真的 0（这一步确实没返工过）
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


def _capture_stdout(argv):
    """跑一次 `run(argv)`，把 stdout / stderr 各收进字符串（自检不许污染自己的 JSON 出口）。"""
    out, err = io.StringIO(), io.StringIO()
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = run(argv)
    finally:
        sys.stdout, sys.stderr = original_out, original_err
    return code, out.getvalue(), err.getvalue()


def run_self_test():
    checks = []
    cross = {"rows": 0, "numbers": 0, "columns": list(CROSS_COLUMNS), "mismatches": []}

    def check(name, actual, expected):
        ok = actual == expected
        checks.append((ok, name, actual, expected))
        return ok

    def check_true(name, value):
        return check(name, bool(value), True)

    def no_bracket_lines(text):
        return [line for line in text.split("\n") if line.startswith("[")]

    try:
        full_doc = load_document(str(FIXTURES_DIR / "full.json"))
        degen_doc = load_document(str(FIXTURES_DIR / "degenerate.json"))

        # ---- csv：表头顺序 = 契约原文（§7.3）
        check("csv.表头与 §7.3 一字不差", ",".join(HEADER), CONTRACT_HEADER)

        full_csv = render_csv(full_doc)
        rows = list(csvlib.reader(io.StringIO(full_csv)))
        check("csv.行数 = 表头 + 每个 step 一行", len(rows), 3)
        check("csv.每行 25 列", sorted({len(row) for row in rows}), [25])

        index = {name: position for position, name in enumerate(HEADER)}

        def cell(row, key):
            return row[index[key]]

        step1, step2 = rows[1], rows[2]
        check("csv.run_id", cell(step1, "run_id"), "export-full-20260101T000000")
        check("csv.step", cell(step1, "step"), "1")
        check("csv.verdict", cell(step1, "verdict"), "fail")
        check("csv.cost_usd 定点 6 位", cell(step1, "cost_usd"), "0.028900")
        check("csv.cost_complete", cell(step1, "cost_complete"), "true")
        check("csv.cost_basis", cell(step1, "cost_basis"), "host")
        check("csv 比率定点 6 位", cell(step1, "first_pass_rate"), "0.400000")
        check("csv 真的 0 写 0（不许写成 unknown）", cell(step1, "cache_write"), "0")
        check("csv 拿不到写 unknown", cell(step1, "reasoning"), "unknown")
        check("csv 拿不到写 unknown（cost_basis）", cell(step2, "cost_basis"), "unknown")
        check(
            "csv.verdict 是 run.verdict 的复制（不是 step 级结论）",
            {cell(step1, "verdict"), cell(step2, "verdict")},
            {run_section(full_doc).get("verdict")},
        )
        check(
            "csv.cost_* 取 step.cost（run.cost 是另一级：这里 run 的 complete 是 false）",
            (cell(step1, "cost_complete"), cell(step2, "cost_complete")),
            ("true", "false"),
        )
        check("csv 不适用写空串（exit_code null）", cell(step2, "exit_code"), "")
        check("csv 不适用写空串（asserts_total null）", cell(step2, "asserts_total"), "")
        check("csv 不适用写空串（first_pass_rate null）", cell(step2, "first_pass_rate"), "")
        check_true("csv 含逗号/引号的标题按 RFC4180 加引号", '"导出, 含""引号"""' in full_csv)

        csv_bytes = encode_csv(full_doc)
        check_true("csv 编码 utf-8-sig（带 BOM）", csv_bytes.startswith(b"\xef\xbb\xbf"))
        check_true("csv 行尾只有 \\n（没有 \\r）", b"\r" not in csv_bytes)
        check_true("csv 末尾恰好一个 \\n", full_csv.endswith("\n") and not full_csv.endswith("\n\n"))
        check_true("csv 里没有 NaN / Infinity", "NaN" not in full_csv and "Infinity" not in full_csv)

        # ---- md：三张表顺序固定 + 三条排版纪律（§7.4）
        full_md = render_md(full_doc)
        positions = [
            full_md.index("## 1. Run 摘要"),
            full_md.index("## 2. Step 表"),
            full_md.index("## 3. 失败断言表"),
        ]
        check_true("md 三张表顺序固定", positions == sorted(positions))
        check_true("md.cost 带 >= 与 complete 标记", ">= 0.030100 USD（complete: false" in full_md)
        # §7.4 operator 追加：Run 摘要里必须有 run 级「返工（合计）」，且数 == Σ step.rework
        rework_rows = [
            line for line in full_md.split("\n")
            if "返工（合计）" in line and line.startswith("|")
        ]
        want_rework = sum(
            step.get("rework") or 0
            for step in steps_of(run_section(full_doc))
            if isinstance(step.get("rework"), int)
        )
        check_true(
            "md §1 有且仅有一行「返工（合计）」（不许跑到 Step 表里）",
            len(rework_rows) == 1
            and full_md.index(rework_rows[0]) < full_md.index("## 2. Step 表")
            and full_md.index(rework_rows[0]) > full_md.index("## 1. Run 摘要"),
        )
        check_true(
            "md「返工（合计）」= Σ step.rework = %d" % want_rework,
            bool(rework_rows) and str(want_rework) in rework_rows[0],
        )
        check_true(
            "md「返工（合计）」同一行写了口径（reqs / 账本 / gates.log）",
            bool(rework_rows)
            and all(word in rework_rows[0] for word in ("reqs", "账本", "gates.log")),
        )
        check_true("md 失败断言表列出 fail / timeout / unacked",
                   all(" | %s | " % result in full_md for result in ("fail", "timeout", "unacked")))
        check_true("md 里没有以 [ 开头的行（不许夹带进度行）", not no_bracket_lines(full_md))
        check_true("md 末尾恰好一个 \\n", full_md.endswith("\n") and not full_md.endswith("\n\n"))
        check_true("md 无 \\r", "\r" not in full_md)
        check_true("md 无失败时整表写一行（无失败断言）", "| （无失败断言） |" in render_md(CLEAN_DOC))
        degen_md = render_md(degen_doc)
        check_true(
            "md 残缺体（steps: 0）三张表照出",
            all(head in degen_md for head in ("## 1. Run 摘要", "## 2. Step 表", "## 3. 失败断言表")),
        )
        degen_step_row = [line for line in degen_md.split("\n") if "（无步骤）" in line]
        check_true("md 残缺体 Step 表写「（无步骤）」而不是留空", len(degen_step_row) == 1)
        check_true(
            "md 残缺体 Step 表那一行没有空单元格",
            bool(degen_step_row)
            and all(part.strip() for part in _md_row_cells(degen_step_row[0])),
        )
        check_true("md 残缺体也不用 0/0 当结论",
                   "0/0" not in degen_md and "0%" not in degen_md)
        check_true("md 残缺体用 null 表示不适用", "null" in degen_md)
        check_true(
            "md 残缺体（steps: 0）也写「返工（合计）」一行，不留空",
            "返工（合计）" in degen_md and "| 返工（合计） | 0（" in degen_md,
        )
        check_true("md 残缺体也没有以 [ 开头的行", not no_bracket_lines(degen_md))

        # ---- json：等价原文（不裁剪、不重排语义）、单对象、无 BOM
        json_text = render_json(full_doc)
        check("json 与输入等价（不裁剪任何字段）", json.loads(json_text), full_doc)
        check_true("json 顶层键序 = 输入键序", list(json.loads(json_text)) == ["schema", "run"])
        check_true("json indent=1", '\n "schema"' in json_text)
        check_true("json 非 ASCII 不转义 + 无 BOM + 无 \\r",
                   "（示意）" in json_text and not json_text.startswith("\ufeff") and "\r" not in json_text)
        check_true("json 末尾恰好一个 \\n", json_text.endswith("\n") and not json_text.endswith("\n\n"))
        check_true(
            "json 导出与源 run.json 字节一致（§7.2 逐字节，不是「等价 JSON」）",
            encode_json(full_doc) == (FIXTURES_DIR / "full.json").read_bytes(),
        )

        # ---- 残缺体：csv 只有表头，不许凭空造行（§8.1）
        check("csv 残缺体只有表头", render_csv(degen_doc), ",".join(HEADER) + "\n")
        check_true("csv 残缺体里没有 0/0", "0/0" not in render_csv(degen_doc))

        # ---- 退出码（§7.1）
        check("完整样本退出码", exit_code_for(full_doc), 0)
        check("残缺体（steps: 0）退出码", exit_code_for(degen_doc), 3)

        # ---- §7.5 确定性：同一份输入重跑字节一致
        check_true(
            "三件套两次渲染字节一致",
            render_csv(full_doc) == render_csv(full_doc)
            and render_md(full_doc) == render_md(full_doc)
            and render_json(full_doc) == render_json(full_doc),
        )

        # ---- ④ 交叉校验：同一份 run.json 的 json / csv / md 三份导出，cost / rounds / asserts 不许两样
        cross = cross_check(full_doc)
        check_true(
            "交叉校验.三种格式都析出了每一步（rows > 0）",
            cross["rows"] == len(steps_of(run_section(full_doc))) and cross["rows"] > 0,
        )
        check("交叉校验.三份导出逐格一致（cost / rounds / asserts）", cross["mismatches"], [])
        check("交叉校验.比了几个数（rows × columns）", cross["numbers"], 2 * len(CROSS_COLUMNS))
        sys.stderr.write(
            "[export][info] 交叉校验：%d 行 × %d 列 = %d 个数，json / csv / md 三份比完，"
            "不一致 %d 处\n"
            % (cross["rows"], len(CROSS_COLUMNS), cross["numbers"], len(cross["mismatches"]))
        )

        # ---- ⑤-2 关键字段全 unknown → csv 里是 `unknown`，一个 0 都不许冒出来
        unknown_rows = list(csvlib.reader(io.StringIO(render_csv(ALL_UNKNOWN_DOC))))
        check("csv 全 unknown 样本：表头 + 1 行", len(unknown_rows), 2)
        unknown_cells = dict(zip(HEADER, unknown_rows[1]))
        for column in ("rounds", "in", "out", "reasoning", "cache_read", "cache_write",
                       "cost_usd", "cost_basis", "asserts_total", "asserts_passed",
                       "first_pass_rate", "final_pass_rate", "exit_code", "exit_code_src"):
            check("csv 全 unknown：%s 写 unknown 不写 0" % column, unknown_cells[column], UNKNOWN)
        check("csv 全 unknown：cost_complete=false 不写 0", unknown_cells["cost_complete"], "false")
        check("csv 全 unknown：真 0 照抄 0（rework）", unknown_cells["rework"], "0")
        check(
            "csv 全 unknown：除真 0 外没有任何格子是 0",
            [name for name, value in unknown_cells.items() if value == "0"],
            ["rework"],
        )
        check_true(
            "md 全 unknown 样本也不把拿不到的写成 0",
            ">= 0.000000" not in render_md(ALL_UNKNOWN_DOC) and "（complete: false" in render_md(ALL_UNKNOWN_DOC),
        )

        # ---- ⑦ 导出里不许出现「总分」「加权」这类字眼（bench 从第一步起就不做加权总分）
        outputs = {
            "csv": render_csv(full_doc),
            "md": render_md(full_doc),
            "json": render_json(full_doc),
            "csv 全 unknown": render_csv(ALL_UNKNOWN_DOC),
            "md 残缺体": render_md(degen_doc),
        }
        check_true(
            "三种导出里都没有「总分」「加权」字眼",
            all(word not in text for text in outputs.values() for word in ("总分", "加权")),
        )

        # ---- CLI 端到端：落盘 == stdout 字节（§7.2），三种格式都走一遍
        with tempfile.TemporaryDirectory() as sandbox:
            for fmt, name in (("csv", "fx.csv"), ("md", "fx.md"), ("json", "fx.json")):
                target = Path(sandbox) / "out" / name
                code, stdout_text, _ = _capture_stdout(
                    ["--run", str(FIXTURES_DIR / "full.json"), "--format", fmt, "--out", str(target)]
                )
                expected = render_bytes(
                    full_doc,
                    fmt,
                    build_dimensions(str(FIXTURES_DIR / "full.json")) if fmt == "md" else None,
                )
                check("cli.%s 退出码" % fmt, code, 0)
                check_true("cli.%s 落盘字节正确" % fmt, target.exists() and target.read_bytes() == expected)
                check_true("cli.%s stdout 与落盘字节一致" % fmt, stdout_text.encode("utf-8") == expected)
            check_true(
                "cli.json 产物与源 run.json 字节一致（§7.2）",
                (Path(sandbox) / "out" / "fx.json").read_bytes()
                == (FIXTURES_DIR / "full.json").read_bytes(),
            )
            check_true(
                "cli.csv 产物带 BOM",
                (Path(sandbox) / "out" / "fx.csv").read_bytes().startswith(b"\xef\xbb\xbf"),
            )

            # ---- ⑤-4 写盘失败 → 退出码 1 + stderr 有人话，stdout 不许是半截产物
            blocker = Path(sandbox) / "blocker"
            blocker.write_text("这不是目录", encoding="utf-8")
            code, stdout_text, stderr_text = _capture_stdout(
                ["--run", str(FIXTURES_DIR / "full.json"), "--format", "csv",
                 "--out", str(blocker / "fx.csv")]
            )
            check("cli 写盘失败退出码", code, 1)
            check_true("cli 写盘失败 stdout 是完整 JSON（不是半截 csv）",
                       stdout_text.lstrip().startswith("{") and stdout_text.rstrip().endswith("}"))
            check("cli 写盘失败 error.code", json.loads(stdout_text)["error"]["code"], "write_failed")
            check_true("cli 写盘失败 stderr 有人话",
                       "[export]" in stderr_text and "写不了" in stderr_text)

        # ---- CLI 错误出口：缺参数 → 退出码 1 + stdout 仍是合法 JSON
        code, stdout_text, _ = _capture_stdout(["--format", "csv"])
        check("cli 缺 --run 退出码", code, 1)
        check("cli 缺 --run 的 error.code", json.loads(stdout_text)["error"]["code"], "missing_arg")

        # ---- CLI 用法错（不认识的 --format）→ 不许是 argparse 默认的 exit(2)+纯文本
        code, stdout_text, _ = _capture_stdout(
            ["--run", str(FIXTURES_DIR / "full.json"), "--format", "nope"]
        )
        check("cli 用法错退出码（不是 argparse 默认的 2）", code, 1)
        check("cli 用法错的 error.code", json.loads(stdout_text)["error"]["code"], "bad_usage")

        # ---- B 路硬边界：`.plan/**` 一律拒读（读一眼也算越界，slices.md §2）
        for bad in (".plan/plan.json", ".plan/ledger.jsonl", "runs/../.plan/gates.log"):
            try:
                refuse_plan_path(bad)
                check_true("拒绝 .plan 路径：%s" % bad, False)
            except ToolError as exc:
                check("拒绝 .plan 路径（%s）code" % bad, exc.code, "lane_b_plan_path")
                check("拒绝 .plan 路径（%s）exit" % bad, exc.exit_code, 1)

        # ---- 输入不在就必须报 run_json_missing / 3，不许静默出空报告
        try:
            load_document(str(FIXTURES_DIR / "nope.json"))
            check_true("缺失文件抛 ToolError", False)
        except ToolError as exc:
            check("缺失文件 code", exc.code, "run_json_missing")
            check("缺失文件 exit", exc.exit_code, 3)

    except ToolError as exc:
        checks.append((False, "自检过程中报错", "%s: %s" % (exc.code, exc.message), "无错误"))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        checks.append((False, "自检过程中内部异常", str(exc), "无异常"))

    failed = [item for item in checks if not item[0]]
    for ok, name, actual, expected in checks:
        if ok:
            sys.stderr.write("[export][ok]   %s\n" % name)
        else:
            sys.stderr.write("[export][FAIL] %s\n  期望：%r\n  实际：%r\n" % (name, expected, actual))
    sys.stderr.write("[export][self-test] %d 项，失败 %d 项\n" % (len(checks), len(failed)))

    write_stdout_bytes(
        (
            json.dumps(
                {
                    "ok": not failed,
                    "self_test": {
                        "checks": len(checks),
                        "failed": len(failed),
                        "failed_names": [item[1] for item in failed],
                        "formats": list(FORMATS),
                        "fixtures": FIXTURES_DIR.name,
                    },
                    "cross_check": {
                        "rows": cross["rows"],
                        "columns": cross["columns"],
                        "numbers": cross["numbers"],
                        "mismatches": cross["mismatches"],
                    },
                },
                ensure_ascii=False,
                indent=1,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    return 0 if not failed else 1


def main(argv=None):
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
