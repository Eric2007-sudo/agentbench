"""collect/coverage/__main__.py —— `python -m collect.coverage` 的出口（`contract/trace.md` §7.1）。

```
python -m collect.coverage --self-test
python -m collect.coverage --run  runs/fx-rework01/run.json \
                            --score runs/fx-rework01/score.json \
                            --plan  collect/fixtures/rework01/.plan/plan.json \
                            --out   runs/fx-rework01/coverage.json
```

出口纪律（§7.1）：stdout 只在 `--json` 时出现**恰好一个** JSON 对象
（UTF-8 无 BOM、`ensure_ascii=False`、`indent=1`、末尾一个 `\\n`）；所有人类提示与错误文本走 stderr。

退出码：`0` 出表完成；`3` 数据不可用（计划里一条 accept 都没有）但已写出合法 JSON；
`1` 工具自身故障（参数错、读写失败、登记表或表本身不自洽、plan 与 score 不是同一个项目）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import selftest, table
from .table import (build_table, check_document, check_point_evidence, consistency_guard,
                    count_clauses, dumps, load_json, load_points)

PROG = "python -m collect.coverage"

#: 登记表跟着代码走（不依赖 cwd）；trace.md 默认按 cwd 解析，找不到再回落到仓库根。
SPEC_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec", "points.json")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fail(code: int, error_code: str, message: str, hint: str, as_json: bool) -> int:
    """§7.1：stdout 一个纯 JSON 错误对象，人话走 stderr，退出码非 0。"""
    print("错误 [%s] %s；%s" % (error_code, message, hint), file=sys.stderr)
    if as_json:
        payload = {"ok": False, "error": {"code": error_code, "message": message, "hint": hint}}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    return code


def _resolve_trace(path: str) -> str:
    """`contract/trace.md` 先按 cwd 找，找不到按仓库根找（两边都在也只读，不改）。"""
    if os.path.isfile(path) or os.path.isabs(path):
        return path
    fallback = os.path.join(ROOT, path)
    return fallback if os.path.isfile(fallback) else path


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="判据覆盖表：plan 的 accept + 人工登记的 clause，逐条给证据级（不出总分）",
        add_help=True,
    )
    parser.add_argument("--self-test", action="store_true",
                        help="逐条 PASS/FAIL 自检（全过 exit 0）")
    parser.add_argument("--run", metavar="RUN_JSON",
                        help="这次 run 的归一化快照（只读；本表不依赖它，给不出来只提示）")
    parser.add_argument("--score", metavar="SCORE_JSON",
                        help="run 级判分快照；mechanical 只认它的 asserts.by_step[*].items")
    parser.add_argument("--plan", metavar="PLAN_JSON", help=".plan/plan.json（accept 的唯一来源）")
    parser.add_argument("--trace", metavar="TRACE_MD", default="contract/trace.md",
                        help="条款梳理的来源（默认 contract/trace.md）")
    parser.add_argument("--spec", metavar="POINTS_JSON", default=SPEC_DEFAULT,
                        help="人工登记的 clause 条款表（默认 collect/coverage/spec/points.json）")
    parser.add_argument("--out", metavar="COVERAGE_JSON", help="覆盖表落盘路径")
    parser.add_argument("--json", action="store_true",
                        help="stdout 输出恰好一个 JSON 对象（提示全走 stderr）")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    if args.self_test:
        return selftest.main()
    if not args.plan:
        return _fail(1, "no_action", "没有指定计划",
                     "用 --self-test，或 --plan <plan.json> [--score <score.json>] [--out <coverage.json>]",
                     args.json)

    try:
        plan_doc = load_json(args.plan)
    except OSError as exc:
        return _fail(1, "plan_unreadable", "读不到 %s（%s）" % (args.plan, exc.__class__.__name__),
                     "先确认 .plan/plan.json 在", args.json)
    except ValueError as exc:
        return _fail(1, "plan_bad_json", "%s 不是合法 JSON（%s）" % (args.plan, exc),
                     "计划坏了要回到编排那一步，本表不猜", args.json)

    score_doc = {}
    if args.score:
        try:
            score_doc = load_json(args.score)
        except (OSError, ValueError) as exc:
            print("警告：判分快照 %s 读不到（%s）→ 没有任何规格点能是 mechanical"
                  % (args.score, exc.__class__.__name__), file=sys.stderr)
            score_doc = {}

    # 一致性守卫：plan 与 score 必须是同一个项目（简报 §八 ⑮ ⑰）。
    # 不过半就直接停，**不写任何 coverage.json** —— 否则会得到一张技术上真实、
    # 含义上误导的表（拿 A 项目的计划比 B 项目的判分）。
    guard = consistency_guard(plan_doc, score_doc)
    if not guard["ok"]:
        return _fail(
            1, "plan_score_mismatch",
            "计划与判分不是同一个项目：plan %d 条、score %d 条、交集 %d 条。"
            "示例 plan 有 %s，score 有 %s"
            % (guard["plan_total"], guard["score_total"], guard["intersection"],
               guard["plan_sample"], guard["score_sample"]),
            "规格点必须来自 <run.root>/.plan/plan.json；不要拿别的项目的判分来比",
            args.json)

    if args.run:
        try:
            run_doc = load_json(args.run)
            run_id = ((run_doc or {}).get("run") or {}).get("id", "unknown")
            print("run=%s（只读核对；覆盖表不依赖 run.json 的字段）" % run_id, file=sys.stderr)
        except (OSError, ValueError) as exc:
            print("提示：%s 读不到（%s）→ 覆盖表照常出，只记下这次没核到 run"
                  % (args.run, exc.__class__.__name__), file=sys.stderr)

    trace_path = _resolve_trace(args.trace)
    try:
        trace_text = _read_text(trace_path)
    except OSError as exc:
        return _fail(1, "trace_unreadable", "读不到 %s（%s）" % (trace_path, exc.__class__.__name__),
                     "条款总数要从 trace.md 数，缺它就出不了 unregistered_clauses", args.json)

    try:
        points = load_points(args.spec)
    except (OSError, ValueError) as exc:
        return _fail(1, "spec_invalid", "登记表 %s 不可用（%s）" % (args.spec, exc),
                     "检查 collect/coverage/spec/points.json 的字段与级别", args.json)

    problems = check_point_evidence(points, ROOT)
    if problems:
        for text in problems:
            print("登记表 evidence 不实：%s" % text, file=sys.stderr)
        return _fail(1, "spec_evidence_invalid", "登记表的 evidence 指向不存在的文件或行号",
                     "evidence 必须写成 <真实文件>:<真实行号>，不许编", args.json)

    clauses_total = count_clauses(trace_text)
    document = build_table(plan_doc, score_doc, points, clauses_total)
    inconsistent = check_document(document)
    if inconsistent:
        for text in inconsistent:
            print("覆盖表不自洽：%s" % text, file=sys.stderr)
        return _fail(1, "table_inconsistent", "覆盖表没通过自洽校验",
                     "这是本模块的缺陷，先修 counts/items/delta 的对齐", args.json)

    text = dumps(document)
    if args.out:
        try:
            parent = os.path.dirname(os.path.abspath(args.out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            return _fail(1, "table_write_failed", "写不了 %s（%s）" % (args.out, exc.__class__.__name__),
                         "检查目标目录权限与剩余空间", args.json)
        print("已落盘 %s（%d 字节）" % (args.out, len(text.encode("utf-8"))), file=sys.stderr)

    if args.json:
        sys.stdout.write(json.dumps({"ok": True, "table": document}, ensure_ascii=False,
                                    indent=1, sort_keys=False) + "\n")

    counts = document["counts"]
    gap = document["unregistered_clauses"]
    print("规格点 %d 条：mechanical=%d semantic=%d none=%d not_applicable=%d；"
          "条款 %d 条已登记 %d 条 delta=%d"
          % (counts["total"], counts["mechanical"], counts["semantic"],
             counts["none"], counts["not_applicable"],
             gap["clauses_total"], gap["registered"], gap["delta"]),
          file=sys.stderr)
    print("从 plan.json 实测 %d 条 accept（条数由数据数出，不写死）"
          % table.count_accepts(plan_doc), file=sys.stderr)
    if gap["delta"] > 0:
        print("未登记条款 delta > 0，覆盖表对剩余条款不具备判别力（见 discriminating_power）",
              file=sys.stderr)

    if counts["total"] == 0:
        print("数据不可用：计划里一条 accept 都没有 → 退出码 3（已输出合法 JSON）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
