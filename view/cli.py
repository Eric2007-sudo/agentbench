#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/cli.py · 路 B 的统一出口（`python -m view.cli`）

一句话：**出口只有一个**。四个子命令（`report` / `regress` / `export` / `board`）
都从这里把结果交给 shell，JSON 口径只在本文件里实现一次，
不再各写一套「我这块怎么吐 JSON」。

用法：

    python -m view.cli report  --run runs/fx-rework01/run.json --json
    python -m view.cli report  --run runs/fx-rework01/run.json --json --out out/fx.json
    python -m view.cli report  --run runs/fx-rework01/run.json --baseline runs/fx-rework01b/run.json
    python -m view.cli regress --base runs/fx-rework01/run.json --cur runs/fx-rework01b/run.json
    python -m view.cli export  --run runs/fx-rework01/run.json --format md --out out/fx.md
    python -m view.cli dimensions --run runs/fx-rework01/run.json --out out/dimensions.json
    python -m view.cli board   --run runs/fx-rework01/run.json --out out/board.html
    python -m view.cli --self-test

四维（性能 / 判据覆盖 / 交互性 / 多模态）**并列**塞进 `dimensions`，放在信封顶层、与 `run` 平级：
`run` 子树一个字节不动；四段之间不加权、不分级、不合成总分（contract/dimensions.md）。
四维**单独**落 `out/dimensions.json`（`dimensions` 子命令），**不许**写进 `out/fx.json`
—— `out/fx.json` 必须始终是 `run.json` 的逐字节副本（trace.md §7.2 判据 A）。
原料只有 `run.json` **同一目录**下的 `measure.json` / `coverage.json` / `interact.json`，
值全部原样照抄，缺就写 `unknown` / `not_applicable`，绝不用 0 兜底。

出口纪律（`contract/trace.md` §7.1）：

- `--json` 时 stdout **恰好一个** JSON 对象：UTF-8 无 BOM、`ensure_ascii=false`、`indent=1`、
  末尾一个 `\\n`；所有人类提示（进度、警告、解释）与错误堆栈**全部**走 stderr；
- 成功体是 `{"ok": true, ...}` 且**顶层带 `run`**（完整 run 段）；
- 出错时 stdout 仍输出 `{"ok": false, "error": {code, message, hint}}`，退出码非 0 ——
  不许 stdout 半截 JSON，也不许 stdout 空着却不给退出码；
- 退出码：`0` 跑完/查询，`2` 结论是 fail/regressed（判分结果，不是工具故障），
  `3` 数据不可用但已输出合法 JSON，`1` 工具自身故障（§7.1）；
- 用法错误（不认识的参数，如 `report --format nope`）也算工具故障：`JsonArgumentParser`
  把 argparse 默认的「纯文本 + `exit(2)`」改成「JSON + 退出码 `1`」，免得与结论 fail 撞车；
- `--out` 落盘与 stdout **字节一致**（同一份 bytes 写两处，§7.2）。

边界（`contract/slices.md` §2）：只读 `runs/**/run.json`；
`.plan/**` 一次都不许读 —— 每个入口先过 `refuse_plan_path()`；
不允许 import `collect.*`（本文件只 import 自己的子包 + 标准库）。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import traceback
from pathlib import Path

from . import board as board_module
from . import dimensions as dimensions_module
from . import regress as regress_module
from .export import (
    COST_DIGITS,
    RATE_DIGITS,
    UNKNOWN,
    JsonArgumentParser,
    ToolError,
    dumps,
    fixed,
    is_int,
    is_num,
    load_document,
    norm_int,
    norm_num,
    pick,
    refuse_plan_path,
    run_section,
    steps_of,
    sub,
    write_bytes_out,
    write_stdout_bytes,
)
from .export.csvio import encode_csv
from .export.jsonio import encode_json
from .export.mdio import encode_md

#: §7.1 退出码（数值写死，别在这里发明新码）
EXIT_OK = 0            # 跑完，且 verdict ∈ {pass, unchanged, improved}，或只是查询
EXIT_TOOL = 1          # 工具自身故障（参数错、写盘失败、内部异常）
EXIT_VERDICT_BAD = 2   # 跑完但结论是 fail / regressed（判分结果，不是故障）
EXIT_UNAVAILABLE = 3   # 数据不可用（steps: 0 / 关键字段全 unknown），但已输出合法 JSON

#: `export` 支持的格式（顺序即帮助里的顺序，§7.2–7.4）
FORMATS = ("json", "csv", "md")

#: `report` 的人类可读表头（`--json` 时一个字都不许进 stdout）
REPORT_HEADER = ("步", "标题", "路", "状态", "轮次", "断言", "首过率", "exit", "返工")


# --------------------------------------------------------------------------- #
# 出口：只在 `_emit()` 里写 stdout（§7.1）
# --------------------------------------------------------------------------- #
def note(message):
    """人话、进度、警告、堆栈只许走 stderr。"""
    sys.stderr.write("[cli] " + message + "\n")


def envelope_ok(**payload):
    """成功体：`ok` 打头，其余按调用方给的顺序（§7.1 顶层必须带 `run`）。"""
    document = {"ok": True}
    document.update(payload)
    return document


def envelope_error(exc):
    """错误体：stdout 就这一个对象，人话版解释另走 stderr。"""
    return {
        "ok": False,
        "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
    }


def _emit(document, code, human=""):
    """唯一的 JSON 出口：一份 bytes 同时写 stdout 与 `--out`（§7.2 字节一致）。"""
    data = dumps(document).encode("utf-8")
    return data, code, human


def _write_and_exit(data, code, human="", out_path=None):
    if out_path:
        write_bytes_out(out_path, data)
    write_stdout_bytes(data)
    if human:
        note(human)
    return code


def emit_json(document, code, human="", out_path=None):
    data, code, human = _emit(document, code, human)
    return _write_and_exit(data, code, human, out_path)


def _emit_error(exc):
    """错误时：stdout 只放错误对象，人话版完整解释走 stderr（§7.1）。"""
    human = exc.message + (" | " + exc.hint if exc.hint else "")
    return emit_json(envelope_error(exc), exc.exit_code, human)


# --------------------------------------------------------------------------- #
# 三态文本（§4：0 只表示真的 0，null 表示不适用，unknown 表示拿不到）
# --------------------------------------------------------------------------- #
def scalar_text(value):
    if value is None:
        return "null"
    if value is UNKNOWN:
        return UNKNOWN
    if value is True:
        return "true"
    if value is False:
        return "false"
    if is_num(value):
        return str(value) if is_int(value) else fixed(value, RATE_DIGITS)
    return str(value)


def cost_marks(cost):
    """§5.4：cost 永远带 `complete` / `unknown_rows`，别让人把下限当全账。"""
    if is_num(cost.get("usd")):
        head = ">= %s USD" % fixed(cost["usd"], COST_DIGITS)
    else:
        head = scalar_text(cost.get("usd"))
    return head, "complete: %s / basis: %s / unknown_rows: %s" % (
        scalar_text(cost.get("complete")),
        scalar_text(cost.get("basis")),
        scalar_text(cost.get("unknown_rows")),
    )


# --------------------------------------------------------------------------- #
# report：一次运行 +（可选）与基准的回归对比
# --------------------------------------------------------------------------- #
def report_payload(document, baseline=None, dimensions=None):
    """拼 `report` 的载荷：`run` 是完整 run 段（§7.1 要求顶层带 `run`）。

    语义固定：**本次 = `--run` 指向的那份**，`--baseline` 是拿来对照的基准；
    两处都只读 `run.json`，路径先过 `refuse_plan_path()`（`.plan/**` 一次都不读）。

    `dimensions` 是四维**并列**段（性能 / 覆盖 / 交互 / 多模态）：放在顶层、与 `run` 平级，
    这样 `run` 子树一个字节都不动；四段之间不加权、不合成总分（dimensions.md §2.3 / §7.2）。
    """
    run = run_section(document)
    summary = board_module.summarize(document)

    payload = {"run": run, "summary": summary}
    payload["dimensions"] = (
        dimensions if isinstance(dimensions, dict) else dimensions_module.empty_dimensions()
    )
    if baseline is not None:
        comparison = regress_module.compare_documents(baseline, run, None, None)
        payload["baseline"] = comparison["baseline"]
        payload["regression"] = comparison["regression"]
        for line in regress_module.history_notes(baseline, run):
            note(line)
    return payload


def report_exit_code(summary, regression):
    """§7.1：结论坏了 → 2；数据不可用 → 3；其余是查询 → 0。

    注意 `verdict: "unknown"` 且有数据时**给 0**：0 在这里的含义是「跑完了」，
    不是「通过了」；结论本身写在 JSON 的 `run.verdict` 里（§4 不许默认 pass，
    也不许拿退出码偷偷改成 pass）。
    """
    if regression is not None and regression.get("verdict") in ("regressed", "mixed"):
        return EXIT_VERDICT_BAD
    if summary["verdict"] == "fail":
        return EXIT_VERDICT_BAD
    if not summary["usable"]:
        return EXIT_UNAVAILABLE
    return EXIT_OK


def report_text(document, payload):
    """人类可读版（只在**没有** `--json` 时进 stdout）。"""
    summary = payload["summary"]
    head, marks = cost_marks(summary["cost"])
    lines = [
        "run %s" % scalar_text(summary["run_id"]),
        "verdict=%s  rounds=%s  cost %s（%s）"
        % (scalar_text(summary["verdict"]), scalar_text(summary["rounds"]), head, marks),
        "步骤 %s ｜ 非 pass 断言 %s ｜ degraded=%s"
        % (
            scalar_text(summary["steps"]),
            scalar_text(summary["asserts"]["non_pass"]),
            scalar_text(summary["degraded"]),
        ),
        "",
        " | ".join(REPORT_HEADER),
    ]
    for step in steps_of(run_section(document)):
        asserts = sub(step, "asserts")
        rate = pick(sub(step, "assert_pass"), "first_pass_rate")
        total = norm_int(pick(asserts, "total"))
        passed = norm_int(pick(asserts, "passed"))
        lines.append(
            " | ".join(
                [
                    scalar_text(norm_int(pick(step, "n"))),
                    scalar_text(pick(step, "title")),
                    scalar_text(pick(step, "lane")),
                    scalar_text(pick(step, "status")),
                    scalar_text(norm_int(pick(step, "rounds"))),
                    "%s/%s" % (scalar_text(passed), scalar_text(total)),
                    scalar_text(norm_num(rate)) if is_num(rate) else scalar_text(rate),
                    scalar_text(norm_int(pick(step, "exit_code"))),
                    scalar_text(norm_int(pick(step, "rework"))),
                ]
            )
        )
    regression = payload.get("regression")
    if regression is not None:
        lines.extend(
            [
                "",
                "regression.verdict=%s  new_failures=%s  fixed=%s"
                % (
                    scalar_text(regression.get("verdict")),
                    len(regression.get("new_failures") or []),
                    len(regression.get("fixed") or []),
                ),
            ]
        )
    else:
        lines.extend(["", "regression=null（没给 --baseline：不适用，不是 unchanged）"])
    return "\n".join(lines) + "\n"


def cmd_report(args):
    if not args.run:
        raise ToolError(
            "missing_arg",
            "缺 --run",
            "用法：python -m view.cli report --run <run.json> [--baseline <run.json>] [--json] [--out <path>]",
            EXIT_TOOL,
        )

    document = load_document(refuse_plan_path(args.run))
    run = run_section(document)
    steps = steps_of(run)
    if not steps:
        # §8.1：没有步骤不是「全部通过」，是数据不可用 —— error.code 固定 no_plan_dir
        raise ToolError(
            "no_plan_dir",
            "目标目录没有 .plan/，没有步骤可归一化（steps: 0）",
            "不许报 0/0 或 0%：先确认 --run 指向采集器落盘的 run.json（trace.md §8.1）",
            EXIT_UNAVAILABLE,
        )

    baseline = regress_module.load_run(args.baseline) if args.baseline else None
    dimensions = dimensions_module.build_dimensions(args.run)
    payload = report_payload(document, baseline, dimensions)
    code = report_exit_code(payload["summary"], payload.get("regression"))

    if args.json:
        human = "verdict=%s steps=%d exit=%d" % (
            scalar_text(payload["summary"]["verdict"]),
            payload["summary"]["steps"],
            code,
        )
        return emit_json(envelope_ok(**payload), code, human, args.out)

    text = report_text(document, payload).encode("utf-8")
    return _write_and_exit(text, code, "verdict=%s exit=%d" % (scalar_text(payload["summary"]["verdict"]), code), args.out)


# --------------------------------------------------------------------------- #
# regress：本次 vs 基准（口径见 trace.md §6，实现在 view/regress.py）
# --------------------------------------------------------------------------- #
def cmd_regress(args):
    if not args.cur:
        raise ToolError(
            "missing_arg",
            "缺 --cur",
            "用法：python -m view.cli regress --base <base.json> --cur <cur.json> [--out <path>]",
            EXIT_TOOL,
        )

    cur_run = regress_module.load_run(args.cur)
    base_run = regress_module.load_run(args.base) if args.base else None
    comparison = regress_module.compare_documents(base_run, cur_run, args.base, args.cur)

    payload = {"ok": True, "baseline": comparison["baseline"], "regression": comparison["regression"]}
    regression = comparison["regression"]
    if regression is None:
        # §6：没有 --baseline 就是 `regression: null`（不适用，不是 unchanged）
        code = EXIT_OK
    else:
        code = regress_module.exit_code_for(regression["verdict"])
    return emit_json(payload, code, "verdict=%s exit=%d" % (
        scalar_text(regression["verdict"]) if regression else "null", code), args.out)


# --------------------------------------------------------------------------- #
# export：三件套（json / csv / md）——产物即 stdout，不套信封（§7.2 字节一致）
# --------------------------------------------------------------------------- #
def render_export(document, fmt, dimensions=None):
    if fmt == "json":
        return encode_json(document)
    if fmt == "csv":
        return encode_csv(document)
    if fmt == "md":
        return encode_md(document, dimensions)
    raise ToolError("bad_format", "不认识的 --format：%s" % fmt, "只支持 " + " / ".join(FORMATS), EXIT_TOOL)


def cmd_export(args):
    if not args.run:
        raise ToolError(
            "missing_arg",
            "缺 --run",
            "用法：python -m view.cli export --run <run.json> --format {json,csv,md} [--out <path>]",
            EXIT_TOOL,
        )

    document = load_document(refuse_plan_path(args.run))
    # json 导出 = run.json 原文（trace.md §7.2 判据 A：与 run.json 逐字节相同）。
    # 四维**不**混进来：它们单独落 out/dimensions.json（见 cmd_dimensions）。
    dimensions = (
        dimensions_module.build_dimensions(args.run) if args.format == "md" else None
    )
    data = render_export(document, args.format, dimensions)
    code = EXIT_OK if steps_of(run_section(document)) else EXIT_UNAVAILABLE
    return _write_and_exit(
        data,
        code,
        "format=%s bytes=%d out=%s exit=%d" % (args.format, len(data), args.out or "<stdout>", code),
        args.out,
    )


# --------------------------------------------------------------------------- #
# dimensions：四维并列段**单独**落盘（不与 run.json 混写，§7.2 判据 A 不许破）
# --------------------------------------------------------------------------- #
def cmd_dimensions(args):
    """只写 `{performance, coverage, interactivity, modalities}` 四段，原值照抄。

    `out/fx.json` 必须是 `run.json` 的逐字节副本（trace.md §7.2 判据 A），
    所以四维不能塞进导出原文；这里单独落 `out/dimensions.json`。
    与 `report --json` 信封里的 `dimensions` 段是同一份对象（同一 `build_dimensions()`）。
    """
    if not args.run:
        raise ToolError(
            "missing_arg",
            "缺 --run",
            "用法：python -m view.cli dimensions --run <run.json> [--out out/dimensions.json]",
            EXIT_TOOL,
        )

    document = load_document(refuse_plan_path(args.run))
    if not steps_of(run_section(document)):
        raise ToolError(
            "no_plan_dir",
            "目标目录没有 .plan/，没有步骤可归一化（steps: 0）",
            "四维是 run 的附加观测：先确认 --run 指向采集器落盘的 run.json（trace.md §8.1）",
            EXIT_UNAVAILABLE,
        )

    payload = dimensions_module.build_dimensions(args.run)
    data = dumps(payload).encode("utf-8")
    return _write_and_exit(
        data,
        EXIT_OK,
        "dimensions bytes=%d out=%s" % (len(data), args.out or "<stdout>"),
        args.out,
    )


# --------------------------------------------------------------------------- #
# board：看板（实现在 view/board.py）
# --------------------------------------------------------------------------- #
def cmd_board(args):
    if not args.run:
        raise ToolError(
            "missing_arg",
            "缺 --run",
            "用法：python -m view.cli board --run <run.json> [--out out/board.html] [--json]",
            EXIT_TOOL,
        )

    document = load_document(refuse_plan_path(args.run))
    code = board_module.exit_code_for(document)
    if args.json:
        payload = envelope_ok(run=run_section(document), summary=board_module.summarize(document))
        return emit_json(payload, code, "board --json exit=%d" % code, args.out)

    data = board_module.render_board(
        document, dimensions_module.build_dimensions(args.run)
    ).encode("utf-8")
    return _write_and_exit(data, code, "board bytes=%d out=%s exit=%d" % (len(data), args.out or "<stdout>", code), args.out)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    parser = JsonArgumentParser(
        prog="python -m view.cli",
        description="路 B 的统一出口：report / regress / export / board（口径 = contract/trace.md）",
    )
    parser.add_argument("--self-test", action="store_true", help="跑 view/fixtures/ 夹具自检")
    subparsers = parser.add_subparsers(dest="command")

    report = subparsers.add_parser("report", help="一次运行的汇总（+ 可选回归对比），默认读 run.json")
    report.add_argument("--run", metavar="RUN_JSON", help="输入 run.json（只读）")
    report.add_argument("--baseline", metavar="RUN_JSON", help="基准 run.json（给了才有 regression）")
    report.add_argument("--json", action="store_true", help="stdout 只放一个 JSON 对象（§7.1）")
    report.add_argument("--out", metavar="PATH", help="落盘路径；与 stdout 字节一致")

    regress = subparsers.add_parser("regress", help="回归对比：本次 vs 基准（§6）")
    regress.add_argument("--base", metavar="RUN_JSON", help="基准 run.json（缺省 → regression: null）")
    regress.add_argument("--cur", metavar="RUN_JSON", help="本次 run.json")
    regress.add_argument("--json", action="store_true", help="保留参数（regress 的输出本来就是 JSON）")
    regress.add_argument("--out", metavar="PATH", help="落盘路径（runs/regress-*.json）")

    export = subparsers.add_parser("export", help="导出 json / csv / md（§7.2–7.5）")
    export.add_argument("--run", metavar="RUN_JSON", help="输入 run.json（只读）")
    export.add_argument("--format", choices=list(FORMATS), default="json", help="导出格式（默认 json）")
    export.add_argument("--out", metavar="PATH", help="落盘路径（out/**）；与 stdout 字节一致")

    board = subparsers.add_parser("board", help="看板：轮次/成本曲线 + 失败分布")
    board.add_argument("--run", metavar="RUN_JSON", help="输入 run.json（只读）")
    board.add_argument("--out", metavar="PATH", help="落盘 HTML（out/board.html）")
    board.add_argument("--json", action="store_true", help="不画图，只吐机器摘要")

    dims = subparsers.add_parser(
        "dimensions", help="四维并列段单独落盘（out/dimensions.json；不混进 out/fx.json）"
    )
    dims.add_argument("--run", metavar="RUN_JSON", help="输入 run.json（只读）")
    dims.add_argument("--out", metavar="PATH", help="落盘路径（out/dimensions.json）；与 stdout 字节一致")

    return parser


HANDLERS = {
    "report": cmd_report,
    "regress": cmd_regress,
    "export": cmd_export,
    "dimensions": cmd_dimensions,
    "board": cmd_board,
}


def run(argv=None):
    # 用法错误（不认识的参数、`--format nope`）一律走统一出口：JSON + 退出码 1。
    # argparse 的默认 `exit(2)` 会与「结论 fail」的 2 撞车（§7.1），所以 parser 用
    # `JsonArgumentParser`，这里接住它抛出的 ToolError。
    try:
        args = build_parser().parse_args(argv)
    except ToolError as exc:
        return _emit_error(exc)

    if args.self_test:
        return run_self_test()

    command = getattr(args, "command", None)
    if not command:
        return _emit_error(
            ToolError(
                "missing_command",
                "缺子命令",
                "用法：python -m view.cli {report,regress,export,board} ...（或 --self-test）",
                EXIT_TOOL,
            )
        )

    try:
        return HANDLERS[command](args)
    except ToolError as exc:
        return _emit_error(exc)
    except Exception as exc:  # noqa: BLE001 — 工具自身故障：堆栈给人看，stdout 只放 JSON
        traceback.print_exc(file=sys.stderr)
        return _emit_error(ToolError("internal_error", "内部异常：%s" % exc, "见 stderr 堆栈", EXIT_TOOL))


# --------------------------------------------------------------------------- #
# 自检：只用 view/fixtures/ 的编造夹具 + 手写样本，不读 runs/、不读 .plan/
# --------------------------------------------------------------------------- #
#: 一步、全 pass 的极小样本（钉住「结论 pass → 退出码 0」）
PASS_DOC = {
    "schema": 1,
    "run": {
        "id": "cli-pass",
        "root": "D:\\...\\view\\fixtures",
        "started_at": "2026-01-01T00:00:00+08:00",
        "finished_at": "2026-01-01T00:05:00+08:00",
        "goal": "（示意）一步全过",
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
                "title": "一步做完",
                "lane": "view",
                "status": "done",
                "planned_out": "view/cli.py",
                "artifacts": [],
                "asserts": {"total": 1, "passed": 1, "failed": 0, "timeout": 0, "unacked": 0,
                            "items": [{"expr": "file:exists:view/cli.py", "kind": "exists",
                                       "result": "pass", "detail": ""}]},
                "assert_pass": {"first_pass_rate": 1.0, "final_pass_rate": 1.0},
                "exit_code": 0,
                "exit_code_src": "gate",
                "rework": 0,
                "first_failed": False,
                "rounds": 1,
                "tokens": {"input": 1000, "output": 100, "reasoning": 0,
                           "cache_read": 0, "cache_write": 0, "complete": True},
                "cost": {"usd": 0.001, "complete": True, "basis": "host", "unknown_rows": 0},
                "rounds_detail": [
                    {"seq": 1, "at": "2026-01-01T00:00:00+08:00", "input": 1000, "output": 100,
                     "reasoning": 0, "cache_read": 0, "cache_write": 0,
                     "cost": {"usd": 0.001, "est": False}, "rounds": 1,
                     "note": "", "decision": None, "src": "ledger"},
                ],
            }
        ],
    },
}


def _capture(argv):
    """跑一次 `run(argv)`，把 stdout / stderr 各收进字符串（自检不许污染自己的出口）。"""
    out, err = io.StringIO(), io.StringIO()
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = run(argv)
    finally:
        sys.stdout, sys.stderr = original_out, original_err
    return code, out.getvalue(), err.getvalue()


def _single_object(text):
    """`--json` 的 stdout 必须**恰好一个** JSON 对象（多一个就抛，少一个也给 None）。"""
    if not text.endswith("\n") or text.endswith("\n\n"):
        return None
    try:
        return json.loads(text[:-1])
    except json.JSONDecodeError:
        return None


def run_self_test():
    checks = []

    def check(name, actual, expected):
        checks.append((actual == expected, name, actual, expected))

    def check_true(name, value):
        check(name, bool(value), True)

    fixtures = Path(__file__).resolve().parent / "fixtures"
    full = str(fixtures / "export" / "full.json")
    degen = str(fixtures / "export" / "degenerate.json")
    case_a_base = str(fixtures / "case-a-new-failure" / "base.json")
    case_a_cur = str(fixtures / "case-a-new-failure" / "cur.json")

    try:
        # ---- ① 成功体：ok + 顶层 run + 单对象 + 定点格式
        code, out, err = _capture(["report", "--run", full, "--json"])
        parsed = _single_object(out)
        check("report --json 退出码（fail → 2）", code, EXIT_VERDICT_BAD)
        check_true("report --json 是恰好一个 JSON 对象", isinstance(parsed, dict))
        check_true("成功体 ok=true", isinstance(parsed, dict) and parsed.get("ok") is True)
        check_true("成功体顶层带 run", isinstance(parsed, dict) and isinstance(parsed.get("run"), dict))
        check_true("成功体带 summary", isinstance(parsed, dict) and isinstance(parsed.get("summary"), dict))
        check_true("indent=1 且键序从 ok 开始", '\n "ok": true' in out)
        check_true("stdout 无 BOM 无 \\r", not out.startswith("\ufeff") and "\r" not in out)
        check_true("stdout 上没有人类提示（人话只在 stderr）",
                   "[cli]" not in out and "[cli]" in err)
        check_true("UTF-8 编码往返一致", out.encode("utf-8").decode("utf-8") == out)

        # ---- ② 结论 pass → 退出码 0
        with tempfile.TemporaryDirectory() as tmp:
            pass_path = Path(tmp) / "pass.json"
            pass_path.write_text(json.dumps(PASS_DOC, ensure_ascii=False, indent=1), encoding="utf-8")
            code, out, err = _capture(["report", "--run", str(pass_path), "--json"])
            parsed = _single_object(out)
            check("pass 结论 → 退出码 0", code, EXIT_OK)
            check_true("pass 结论的 verdict 照抄 run.verdict", parsed.get("run", {}).get("verdict") == "pass")

            # ---- ③ `--out` 与 stdout 字节一致（§7.2）
            out_path = Path(tmp) / "report.json"
            code2, out2, err2 = _capture(
                ["report", "--run", full, "--json", "--out", str(out_path)]
            )
            check_true("--out 与 stdout 字节一致", out_path.read_bytes() == out2.encode("utf-8"))
            check("带 --out 时退出码不变", code2, EXIT_VERDICT_BAD)

            # ---- ④ 回归对比：给了 --baseline 才有 regression 段
            code3, out3, err3 = _capture(
                ["report", "--run", case_a_cur, "--baseline", case_a_base, "--json"]
            )
            parsed3 = _single_object(out3)
            check_true("给了 --baseline → 有 regression 段",
                       isinstance(parsed3, dict) and isinstance(parsed3.get("regression"), dict))
            check_true("regression 段有 verdict", bool(parsed3.get("regression", {}).get("verdict")))
            code4, out4, err4 = _capture(["report", "--run", full, "--json"])
            check_true("没给 --baseline → regression 不出现（不是 unchanged）",
                       "regression" not in (_single_object(out4) or {}))

            # ---- ④-2 四维（v12）：值原样照抄传送带文件，view 自己不算数、不合成
            run_dir = Path(tmp) / "dims-run"
            run_dir.mkdir()
            (run_dir / "run.json").write_bytes(Path(full).read_bytes())
            measure = {
                "wall_s": 12.5,
                "turns": 2,
                "out_tokens_per_wall_s": 0.25,
                "ttft_s": UNKNOWN,
                "tool_calls": UNKNOWN,
                "round_gap": {"n": 2, "p50": 3.0, "p90": 9.0, "max": 9.0},
                "modalities": [
                    {"modality": "text", "status": "measured", "reason": None,
                     "evidence": "capabilities.input = [\"text\"]"},
                    {"modality": "image", "status": "not_applicable", "reason": "未声明",
                     "evidence": "capabilities.input = [\"text\"]"},
                ],
            }
            coverage = {
                "counts": {"mechanical": 1, "semantic": 0, "none": 2,
                           "not_applicable": 1, "total": 4},
                "unregistered_clauses": {"clauses_total": 10, "registered": 3, "delta": 7},
                "discriminating_power": "不具备判别力",
            }
            interact = {
                "scenarios": [{"id": "clarify", "turns_to_done": 2, "verdict": "done"}],
                "cost": 0.5,
            }
            for name, payload in (("measure.json", measure), ("coverage.json", coverage),
                                  ("interact.json", interact)):
                (run_dir / name).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
            dims_path = str(run_dir / "run.json")
            code5, out5, err5 = _capture(["report", "--run", dims_path, "--json"])
            dims = (_single_object(out5) or {}).get("dimensions") or {}
            check("四维并列：键名固定四个", list(dims), ["performance", "coverage",
                                                       "interactivity", "modalities"])
            check("performance.wall_s 照抄 measure.json", dims.get("performance", {}).get("wall_s"), 12.5)
            check("performance.round_gap.p90 照抄（不硬凑）",
                  dims.get("performance", {}).get("round_gap", {}).get("p90"), 9.0)
            check("performance.ttft_s 保持 unknown",
                  dims.get("performance", {}).get("ttft_s"), UNKNOWN)
            check("coverage.counts 照抄 coverage.json",
                  dims.get("coverage", {}).get("counts"), coverage["counts"])
            check("interactivity 场景照抄 interact.json",
                  dims.get("interactivity", {}).get("scenarios"), interact["scenarios"])
            check("modalities 逐模态原值（null 保持 null、not_applicable 不写 0）",
                  dims.get("modalities", {}).get("rows"), measure["modalities"])
            check_true("dimensions 里没有合成分数字段",
                       not any(word in out5 for word in ("\"score\"", "\"weight\"",
                                                         "\"grade\"", "\"threshold\"")))

            code6, out6, err6 = _capture(["export", "--run", dims_path, "--format", "md"])
            check_true("md 四节标题齐全",
                       all(title in out6 for title in ("运行性能", "判据覆盖", "交互性", "多模态")))
            check_true("md 每节第一行写「不能支撑什么」", out6.count("不能支撑什么") >= 4)
            check_true("md 没有「0%」", "0%" not in out6)

            # 四维**不**许混进 json 导出：即便 run.json 旁边有传送带文件，
            # 导出也必须仍是 run.json 的逐字节副本（trace.md §7.2 判据 A）
            side_json = Path(tmp) / "side-fx.json"
            code_side, out_side, err_side = _capture(
                ["export", "--run", dims_path, "--format", "json", "--out", str(side_json)]
            )
            check("有传送带文件时 json 导出退出码", code_side, EXIT_OK)
            check_true(
                "A：有传送带文件时 json 导出仍与 run.json 逐字节相同",
                side_json.exists()
                and side_json.read_bytes() == (run_dir / "run.json").read_bytes(),
            )
            check_true("json 导出里没有 dimensions 段", "\"dimensions\"" not in out_side)

            # 四维单独落盘：`dimensions` 子命令 == report 信封里的 dimensions 段
            code9, out9, err9 = _capture(["dimensions", "--run", dims_path])
            dims_file = (_single_object(out9) or {})
            check("dimensions 子命令退出码", code9, EXIT_OK)
            check("dimensions 子命令与 report 的 dimensions 段一致", dims_file, dims)
            check("dimensions 子命令顶层就是四段（没有信封）",
                  list(dims_file), ["performance", "coverage", "interactivity", "modalities"])

            code7, out7, err7 = _capture(["board", "--run", dims_path])
            check_true("多模态 not_applicable 在看板上照写 not_applicable",
                       "not_applicable" in out7)
            check_true("看板里没有「0%」", "0%" not in out7)

            # 缺传送带文件 → 四段 unknown，不许填 0 兜底（§4）
            code8, out8, err8 = _capture(["report", "--run", str(pass_path), "--json"])
            dims8 = (_single_object(out8) or {}).get("dimensions") or {}
            check("缺传送带文件：performance.status = unknown",
                  dims8.get("performance", {}).get("status"), UNKNOWN)
            check("缺传送带文件：wall_s 写 unknown（不是 0）",
                  dims8.get("performance", {}).get("wall_s"), UNKNOWN)
            check("缺传送带文件：modalities 一行都没有（不编）",
                  dims8.get("modalities", {}).get("rows"), [])

        # ---- ⑤ 错误体：steps: 0 → no_plan_dir + 退出码 3（§8.1）
        code, out, err = _capture(["report", "--run", degen, "--json"])
        parsed = _single_object(out)
        check("steps: 0 → 退出码 3", code, EXIT_UNAVAILABLE)
        check_true("错误体 ok=false", isinstance(parsed, dict) and parsed.get("ok") is False)
        check(
            "错误体 code = no_plan_dir",
            (parsed or {}).get("error", {}).get("code"),
            "no_plan_dir",
        )
        check_true("错误体没有 run（不是「空报告成功」）", "run" not in (parsed or {}))
        check_true("错误体 message/hint 都是字符串",
                   isinstance((parsed or {}).get("error", {}).get("message"), str)
                   and isinstance((parsed or {}).get("error", {}).get("hint"), str))
        check_true("错误时 stderr 有人话", "[cli]" in err)

        # ---- ⑥ `.plan/**` 一次都不许读：直接把路径拒掉（不打开文件）
        code, out, err = _capture(["report", "--run", ".plan/并不存在.json", "--json"])
        parsed = _single_object(out)
        check(".plan 路径 → 退出码 1", code, EXIT_TOOL)
        check(
            ".plan 路径 → 退出码前就被拒（code = lane_b_plan_path）",
            (parsed or {}).get("error", {}).get("code"),
            "lane_b_plan_path",
        )

        # ---- ⑦ 参数错也要给合法 JSON + 非 0
        code, out, err = _capture(["report", "--json"])
        parsed = _single_object(out)
        check("缺 --run → 退出码 1", code, EXIT_TOOL)
        check("缺 --run → code = missing_arg", (parsed or {}).get("error", {}).get("code"), "missing_arg")
        code, out, err = _capture([])
        parsed = _single_object(out)
        check("缺子命令 → 退出码 1", code, EXIT_TOOL)
        check("缺子命令 → code = missing_command", (parsed or {}).get("error", {}).get("code"), "missing_command")

        # ---- ⑦-2 用法错（不认识的参数）→ argparse 的 exit(2) 会与「结论 fail」撞车，
        #      必须改成「纯 JSON 错误体 + 退出码 1」（§7.1）
        code, out, err = _capture(["report", "--run", full, "--format", "nope"])
        parsed = _single_object(out)
        check("不认识的参数 → 退出码 1（不是 argparse 默认的 2）", code, EXIT_TOOL)
        check("不认识的参数 → code = bad_usage", (parsed or {}).get("error", {}).get("code"), "bad_usage")
        check_true("不认识的参数 → stdout 是完整 JSON 错误体（不留空）",
                   isinstance(parsed, dict) and parsed.get("ok") is False)
        check_true("不认识的参数 → 人话在 stderr", "[cli]" in err)

        # ---- ⑦-3 §7.2（operator 裁定）的 A / B / C 三条判据各一条用例
        #      A：导出文件 == run.json 原文（逐字节）
        #      B：导出侧 stdout == 落盘文件（逐字节）
        #      C：`--json` 信封里的 run 与 run.json 的 run 深度相等（不比字节）
        with tempfile.TemporaryDirectory() as tmp:
            byte_target = Path(tmp) / "fx.json"
            code, out, err = _capture(
                ["export", "--run", full, "--format", "json", "--out", str(byte_target)]
            )
            check("A/B 用例的导出退出码", code, EXIT_OK)
            check_true(
                "A：导出文件与 run.json 原文逐字节相同",
                byte_target.exists() and byte_target.read_bytes() == Path(full).read_bytes(),
            )
            check_true(
                "B：导出侧 stdout 与落盘文件逐字节相同",
                out.encode("utf-8") == byte_target.read_bytes(),
            )
        _, envelope_out, _ = _capture(["report", "--run", full, "--json"])
        envelope = _single_object(envelope_out) or {}
        check_true(
            "C：信封里的 run 与 run.json 的 run 深度相等（未裁字段）",
            envelope.get("run") == load_document(full).get("run"),
        )

        # ---- ⑧ 四个子命令共用同一个出口：都是 {"ok": ...}
        for name, argv in (
            ("report", ["report", "--run", full, "--json"]),
            ("regress", ["regress", "--base", case_a_base, "--cur", case_a_cur]),
            ("board", ["board", "--run", full, "--json"]),
        ):
            code, out, err = _capture(argv)
            parsed = _single_object(out)
            check_true("%s 走统一出口（单对象 + ok）" % name, isinstance(parsed, dict) and "ok" in parsed)
        code, out, err = _capture(["regress", "--base", case_a_base, "--cur", case_a_cur])
        check("regress 结论坏 → 退出码 2（判分结果，不是故障）", code, EXIT_VERDICT_BAD)

        # ---- ⑨ 出口统一 = 同一份数：report 与 board 的 run/summary 必须一模一样
        _, report_out, _ = _capture(["report", "--run", full, "--json"])
        _, board_out, _ = _capture(["board", "--run", full, "--json"])
        report_doc = _single_object(report_out) or {}
        board_doc = _single_object(board_out) or {}
        check_true("report 与 board 的 run 段一字不差", report_doc.get("run") == board_doc.get("run"))
        check_true("report 与 board 的 summary 一字不差",
                   report_doc.get("summary") == board_doc.get("summary"))

        # ---- ⑩ export 三件套不被信封包住（产物即 stdout，§7.2 字节一致）
        #      json / csv 只导 run.json；md 额外追加四维四节（§7.4 + dimensions.md）
        document = load_document(full)
        for fmt, encoder in (("json", encode_json), ("csv", encode_csv)):
            code, out, err = _capture(["export", "--run", full, "--format", fmt])
            check_true("export %s 的 stdout 就是产物本身" % fmt,
                       out.encode("utf-8") == encoder(document))
            check("export %s 退出码" % fmt, code, EXIT_OK)
        code, out, err = _capture(["export", "--run", full, "--format", "md"])
        check_true("export md 的 stdout 就是产物本身（含四维四节）",
                   out.encode("utf-8")
                   == encode_md(document, dimensions_module.build_dimensions(full)))
        check("export md 退出码", code, EXIT_OK)
        code, out, err = _capture(["export", "--run", degen, "--format", "csv"])
        check("export 残缺体 → 退出码 3", code, EXIT_UNAVAILABLE)
        code, out, err = _capture(["board", "--run", full])
        check("board 默认出 HTML（stdout 有 svg）", 0 if "<svg" in out else 1, 0)
        check("board HTML 的退出码", code, EXIT_OK)

        # ---- ⑪ 确定性：同一份输入两次 `--json` 字节相同（§7.5）
        _, first, _ = _capture(["report", "--run", full, "--json"])
        _, second, _ = _capture(["report", "--run", full, "--json"])
        check_true("两次 report --json 字节一致", first == second)

        # ---- ⑫ 三态：summary 里没有 NaN、没有把 unknown 写成 0
        summary = (report_doc or {}).get("summary", {})
        check_true("summary 里没有 NaN/Infinity",
                   "NaN" not in report_out and "Infinity" not in report_out)
        check_true(
            "拿不到的 cost 保持 unknown（不是 0）",
            any(isinstance(step.get("cost", {}).get("usd"), str)
                for step in (report_doc.get("run", {}).get("steps") or [])),
        )
        check_true("summary.asserts.total 数得出来", is_int(summary.get("asserts", {}).get("total")))

    except ToolError as exc:
        checks.append((False, "自检读夹具失败：%s" % exc.message, None, None))

    failed = [item for item in checks if not item[0]]
    for ok, name, actual, expected in checks:
        if not ok:
            sys.stderr.write("[cli][self-test] FAIL %s：actual=%r expected=%r\n" % (name, actual, expected))
    sys.stderr.write("[cli][self-test] %d 项，失败 %d 项\n" % (len(checks), len(failed)))
    write_stdout_bytes(
        dumps(
            {
                "ok": not failed,
                "self_test": {
                    "checks": len(checks),
                    "failed": len(failed),
                    "failed_names": [item[1] for item in failed],
                    "fixtures": "fixtures",
                },
            }
        ).encode("utf-8")
    )
    return EXIT_OK if not failed else EXIT_TOOL


def main(argv=None):
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
