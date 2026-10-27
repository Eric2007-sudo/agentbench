"""collect/score/__main__.py —— `python -m collect.score` 的出口（`contract/trace.md` §7.1）。

```
python -m collect.score --self-test                       # 造退化场景自检
python -m collect.score --verify runs/fx-rework01/score.json
python -m collect.score --run runs/fx-rework01/run.json \
                        --baseline runs/fx-rework01/run.json \
                        --out runs/fx-rework01/score.json
```

出口纪律（§7.1）：

- stdout 只在 `--json` 时出现**恰好一个** JSON 对象（`ensure_ascii=false`、`indent=1`、单个 `\\n` 结尾）；
- 所有人类提示与错误明细走 **stderr**；
- 出错时 stdout 仍是合法 JSON 错误对象，退出码非 0。

退出码：

| code | 含义 |
|---|---|
| `0` | 判完，`verdict ∈ {pass, unknown}`（unknown 是「证据不全」，不是工具故障） |
| `2` | 判完且 `verdict == "fail"`（判分结果，不是故障） |
| `3` | 数据不可用（`steps: 0` 或关键字段全 `"unknown"`），但已输出合法 JSON |
| `1` | 工具自身故障（参数错、读不到文件、写盘失败、内部异常） |
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import selftest, verify
from .base import UNKNOWN, is_unknown
from .judge import dumps, judge

PROG = "python -m collect.score"


def _fail(code: int, error_code: str, message: str, hint: str, as_json: bool) -> int:
    """§7.1：stdout 一个纯 JSON 错误对象，人话走 stderr，退出码非 0。"""
    print("错误 [%s] %s；%s" % (error_code, message, hint), file=sys.stderr)
    if as_json:
        payload = {"ok": False, "error": {"code": error_code, "message": message, "hint": hint}}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    return code


def _load_json(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return json.load(handle)


def _unusable(document: dict) -> bool:
    """数据不可用：一步都没有，或四类里关键字段全 `"unknown"`（入参是判分快照，不是 run.json）。"""
    steps = (document.get("asserts") or {}).get("by_step") or []
    if not steps:
        return True
    cost = document.get("cost") or {}
    asserts = document.get("asserts") or {}
    exitcode = document.get("exitcode") or {}
    everything_unknown = (
        is_unknown(cost.get("usd", UNKNOWN))
        and is_unknown(cost.get("rounds", UNKNOWN))
        and is_unknown((asserts.get("counts") or {}).get("passed", UNKNOWN))
        and is_unknown((exitcode.get("counts") or {}).get("pass", UNKNOWN))
    )
    return everything_unknown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="路 A 判分器：exit code / 断言通过率 / 产物 diff / 成本与轮次（contract/trace.md §5）",
        add_help=True,
    )
    parser.add_argument("--self-test", action="store_true",
                        help="造退化场景自检（一条一条 PASS/FAIL，最后打总条数）")
    parser.add_argument("--verify", metavar="SCORE_JSON", help="拿钉死的期望值逐条核一份 score.json")
    parser.add_argument("--run", metavar="RUN_JSON", help="要判分的 run.json（路 A 的交接物）")
    parser.add_argument("--baseline", metavar="BASELINE_RUN_JSON",
                        help="产物 diff 的基准 run.json；不给 → diff 整段 unknown（不报「0 新增 0 删除」）")
    parser.add_argument("--out", metavar="SCORE_JSON", help="判分快照落盘路径，如 runs/fx-rework01/score.json")
    parser.add_argument("--json", action="store_true", help="stdout 输出恰好一个 JSON 对象（提示全走 stderr）")
    parser.add_argument("--run-declared", action="store_true",
                        help="允许第 3 优先级：真跑 plan 里声明的 cmd: 验收命令取退出码（默认关，保持纯函数）")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    if args.self_test:
        return selftest.main()
    if args.verify:
        return verify.run(args.verify)
    if not args.run:
        return _fail(1, "no_action",
                     "没有指定动作",
                     "用 --self-test / --verify <score.json> / --run <run.json> [--out <score.json>]",
                     args.json)

    try:
        run_doc = _load_json(args.run)
    except OSError as exc:
        return _fail(1, "run_json_unreadable", "读不到 %s（%s）" % (args.run, exc.__class__.__name__),
                     "先确认采集器的交接物在 runs/<id>/run.json", args.json)
    except ValueError as exc:
        return _fail(1, "run_json_bad_json", "%s 不是合法 JSON（%s）" % (args.run, exc),
                     "run.json 是交接物，坏了要回到采集那一步", args.json)

    baseline_doc = None
    if args.baseline:
        try:
            baseline_doc = _load_json(args.baseline)
        except (OSError, ValueError) as exc:
            print("警告：基准 %s 读不到（%s）→ diff 段记 unknown" % (args.baseline, exc.__class__.__name__),
                  file=sys.stderr)
            baseline_doc = None

    try:
        document = judge(run_doc, baseline_doc, run_json_path=args.run,
                         baseline_path=args.baseline if baseline_doc is not None else None,
                         run_declared=args.run_declared)
    except ValueError as exc:
        return _fail(1, "run_json_bad_shape", str(exc), "run.json 缺顶层 run 对象", args.json)

    text = dumps(document)
    if args.out:
        try:
            parent = os.path.dirname(os.path.abspath(args.out))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            return _fail(1, "score_write_failed", "写不了 %s（%s）" % (args.out, exc.__class__.__name__),
                         "检查目标目录权限与剩余空间", args.json)
        print("已落盘 %s（%d 字节）" % (args.out, len(text.encode("utf-8"))), file=sys.stderr)

    if args.json:
        sys.stdout.write(json.dumps({"ok": True, "score": document}, ensure_ascii=False,
                                    indent=1, sort_keys=False) + "\n")

    cost = document.get("cost") or {}
    asserts = document.get("asserts") or {}
    print("verdict=%s；cost.usd=%s（complete=%s，is_lower_bound=%s）；rounds=%s；"
          "断言 %s/%s；首过率=%s；收尾通过率=%s"
          % (document["verdict"], cost.get("usd"), cost.get("complete"), cost.get("is_lower_bound"),
             cost.get("rounds"), (asserts.get("counts") or {}).get("passed"),
             (asserts.get("counts") or {}).get("total"),
             asserts.get("first_pass_rate"), asserts.get("final_pass_rate")),
          file=sys.stderr)

    if _unusable(document):
        print("数据不可用：steps 为空或关键字段全 unknown → 退出码 3（已输出合法 JSON）", file=sys.stderr)
        return 3
    if document["verdict"] == "fail":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
