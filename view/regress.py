#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/regress.py · 回归对比：本次 vs 基准（判定口径 = contract/trace.md §6）

只吃 `run.json`：不读 `.plan/**`、不读产物目录、不 import `collect.*`。
出口纪律（trace.md §7.1）：stdout **恰好一个** JSON 对象（UTF-8、无 BOM、
`ensure_ascii=false`、`indent=1`、末尾一个 `\\n`），所有人话与警告一律走 stderr。

用法：
    python -m view.regress --base runs/fx-rework01/run.json \
                           --cur  runs/fx-rework01b/run.json \
                           --out  runs/regress-fx.json
    python -m view.regress --self-test

对齐键（§6）：断言级 = `step.n` + 断言原文 `expr`；成本级 = `run` 整体；
通过率级 = 全 run 的 `Σasserts.passed / Σasserts.total`（run.json 没有 run 级的
`final_pass_rate`，只能由 step 级汇总，口径写死在这里，不许临场改）；
`regression.steps` 只是把 §2.3 的 `rounds` / `rework` 按 `step.n` 并列并给 delta，
字段名照抄契约，不新造语义（轮次与返工的变化必须与成本、通过率分开报）。

退出码（§7.1）：0 = 结论 improved/unchanged 或只是查询；2 = regressed/mixed；
3 = 数据不可用（关键字段 unknown、基准缺失导致无从对比）；1 = 工具自身故障。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

SCHEMA_VERSION = 1
UNKNOWN = "unknown"

#: 断言结果的合法取值（trace.md §5.2 的 `result ∈ pass/fail/timeout/unacked`）
KNOWN_RESULTS = ("pass", "fail", "timeout", "unacked")
#: 基准里这些结果 + 本次 pass → `fixed`（§6）
BASE_NOT_PASS = ("fail", "timeout", "unacked")
#: 基准 pass + 本次这些结果 → `new_failures`（§6）
CUR_BROKEN = ("fail", "timeout")

RATE_DIGITS = 6   # §7.5 浮点定点：比率 6 位
COST_DIGITS = 6   # §7.5 浮点定点：cost 6 位

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

EXIT_BY_VERDICT = {
    "improved": 0,
    "unchanged": 0,
    "regressed": 2,
    "mixed": 2,
    "unknown": 3,
}


# --------------------------------------------------------------------------- #
# 出口工具
# --------------------------------------------------------------------------- #
def _note(message):
    """人话只许走 stderr（trace.md §7.1）。"""
    sys.stderr.write("[regress] " + message + "\n")


def dumps(document):
    """唯一的 JSON 序列化口径：字节级可复现（§7.5）。"""
    return json.dumps(document, ensure_ascii=False, indent=1, allow_nan=False) + "\n"


def write_stdout(text):
    """stdout 必须与 `--out` 落盘字节一致（§7.2）。

    Windows 上 `sys.stdout` 是文本模式，会把 `\\n` 翻成 `\\r\\n`，
    所以走 buffer 直接写 UTF-8 字节 —— 否则「stdout 与导出文件字节一致」不成立。
    """
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:                      # 已被重定向成纯文本流（测试用）
        sys.stdout.write(text)
    else:
        stream.write(text.encode("utf-8"))
    sys.stdout.flush()


class ToolError(Exception):
    """带出口码与机器可读 code 的故障（trace.md §7.1 错误体）。"""

    def __init__(self, code, message, hint="", exit_code=1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


def _emit_error(exc):
    document = {
        "ok": False,
        "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
    }
    write_stdout(dumps(document))
    _note(exc.message + (" | " + exc.hint if exc.hint else ""))
    return exc.exit_code


# --------------------------------------------------------------------------- #
# 读 run.json（只读，只碰 runs/**/run.json）
# --------------------------------------------------------------------------- #
def refuse_plan_path(path):
    """B 路的硬边界：`.plan/**` 一次都不许读（slices.md §2「绝对不碰」）。

    `view/` 只吃 `run.json`。这里不是「顺手校验一下」，而是入口处的机械闸：
    只要目标路径里出现 `.plan` 这一段，直接按工具用法错误拒掉（退出码 1），
    绝不打开文件 —— 越界读到的中间状态会污染回归判断，读一眼也算越界。
    """
    location = Path(path)
    for part in location.parts:
        if part == ".plan":
            raise ToolError(
                "lane_b_plan_path",
                "拒绝读取 %s：view/ 只吃 run.json，不许读 .plan/**" % path,
                "run.json 缺字段就该改契约（contract/trace.md），不是回头读 .plan/",
                1,
            )
    return location


def load_run(path):
    """读一份 run.json，返回 `run` 段（dict）。读不到就抛 ToolError。"""
    location = refuse_plan_path(path)
    try:
        raw = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ToolError(
            "run_json_missing",
            "读不到 %s" % path,
            "view/ 只吃 runs/**/run.json；先确认采集器已经把 run.json 落盘",
            3,
        )
    except OSError as exc:
        raise ToolError("run_json_unreadable", "读不了 %s：%s" % (path, exc), "", 3)

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(
            "run_json_bad_json",
            "%s 不是合法 JSON：%s" % (path, exc),
            "坏输入不推断、不修补，交回采集侧",
            1,
        )

    if not isinstance(document, dict):
        raise ToolError("run_json_shape", "%s 顶层不是对象" % path, "", 3)

    run = document.get("run")
    if isinstance(run, dict):
        return run
    if "steps" in document:            # 容忍裸 run 体（没有 run 包装）
        return document
    raise ToolError("run_json_shape", "%s 里没有 run 段" % path, "形状见 trace.md §2.1", 3)


# --------------------------------------------------------------------------- #
# 小谓词
# --------------------------------------------------------------------------- #
def _is_num(value):
    """number 判定：bool 不算 number（Python 里 True == 1，会毒化 delta）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _steps(run):
    return [step for step in (run.get("steps") or []) if isinstance(step, dict)]


def _step_no(step):
    number = step.get("n")
    return number if isinstance(number, int) and not isinstance(number, bool) else UNKNOWN


def _sort_key(step_no, expr):
    """§7.5 数组按稳定键排序：先步号（数字）再断言原文。"""
    if isinstance(step_no, int):
        return (0, step_no, str(expr))
    return (1, 0, str(expr))


def _assert_index(run):
    """(step.n, expr) → 断言条目；同键重复时保留先出现的并记一笔（不合并、不平均）。"""
    index = {}
    duplicates = []
    for step in _steps(run):
        number = _step_no(step)
        asserts = step.get("asserts") or {}
        for item in asserts.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = (number, item.get("expr"))
            if key in index:
                duplicates.append(key)
                continue
            index[key] = item
    return index, duplicates


def _run_id(run):
    identifier = run.get("id")
    return identifier if isinstance(identifier, str) and identifier else UNKNOWN


# --------------------------------------------------------------------------- #
# 成本段（§6 cost 行）
# --------------------------------------------------------------------------- #
def _cost_section(base_run, cur_run):
    base_cost = base_run.get("cost") or {}
    cur_cost = cur_run.get("cost") or {}

    before_usd = base_cost.get("usd", UNKNOWN)
    after_usd = cur_cost.get("usd", UNKNOWN)
    # complete 缺字段按 false 处理：宁可多报 is_lower_bound，不许把下限说成完整成本。
    before_complete = base_cost.get("complete") is True
    after_complete = cur_cost.get("complete") is True

    section = {
        "before": {"usd": before_usd, "complete": before_complete},
        "after": {"usd": after_usd, "complete": after_complete},
    }

    if _is_num(before_usd) and _is_num(after_usd):
        delta = round(after_usd - before_usd, COST_DIGITS)
        section["delta_usd"] = delta
        if before_usd == 0:
            # §4：分母是 0 → null（不适用），绝不用 0 冒充。
            section["delta_pct"] = None
        else:
            section["delta_pct"] = round((delta / before_usd) * 100.0, COST_DIGITS)
        section["direction"] = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    else:
        # 任一 unknown → 两个 delta 都 unknown，只并列两侧原值（§6）。
        section["delta_usd"] = UNKNOWN
        section["delta_pct"] = UNKNOWN
        section["direction"] = UNKNOWN

    # 任一侧 complete:false → 成本是「至少花了这么多」。
    section["is_lower_bound"] = (not before_complete) or (not after_complete)
    return section


# --------------------------------------------------------------------------- #
# 断言段（§6 asserts 行）
# --------------------------------------------------------------------------- #
def _aggregate_pass_rate(run):
    """全 run 通过率 = Σpassed / Σtotal（只用 step 级已算好的数，不重算断言）。"""
    total = 0
    passed = 0
    unknown = False
    for step in _steps(run):
        asserts = step.get("asserts") or {}
        step_total = asserts.get("total")
        step_passed = asserts.get("passed")
        if step_total is None and step_passed is None:
            continue                       # 计划里这一步就没有断言 → 不进分母
        if not _is_num(step_total) or not _is_num(step_passed):
            unknown = True                 # 有分母/分子拿不到 → 严格传播（§4）
            continue
        total += int(step_total)
        passed += int(step_passed)
    if unknown:
        return UNKNOWN
    if total == 0:
        return None                        # §4：分母 0 → null，不写 0%
    return round(passed / total, RATE_DIGITS)


def _asserts_section(base_run, cur_run):
    before = _aggregate_pass_rate(base_run)
    after = _aggregate_pass_rate(cur_run)
    if _is_num(before) and _is_num(after):
        delta = round(after - before, RATE_DIGITS)
    elif before is UNKNOWN or after is UNKNOWN:
        delta = UNKNOWN
    else:
        delta = None                       # 一侧不适用 → 变化不适用
    return {"before": before, "after": after, "delta": delta}


# --------------------------------------------------------------------------- #
# 步骤段（§2.3 的 `rounds` / `rework` 逐 step 对照）
# --------------------------------------------------------------------------- #
def _step_by_no(run):
    """step.n → step；同号重复时保留先出现的（与断言索引同口径，不合并）。"""
    index = {}
    for step in _steps(run):
        number = _step_no(step)
        if number in index:
            _note("run 里 step=%s 重复，只认先出现的那个" % number)
            continue
        index[number] = step
    return index


def _step_metric(step, field):
    """取 §2.3 的 `rounds` / `rework`：没有 step、没有字段、值不认识 → `"unknown"`。"""
    if step is None:
        return UNKNOWN
    value = step.get(field, UNKNOWN)
    if field == "rework":
        return value if _is_num(value) else UNKNOWN      # 契约里 rework 是 int
    if _is_num(value) or value == UNKNOWN:
        return value
    return UNKNOWN                                       # None/别的东西都不许冒充 0


def _delta(before, after):
    """int 类字段严格传播（§4）：任一侧 unknown → 父项 unknown，不按剩下的算。"""
    if _is_num(before) and _is_num(after):
        return after - before
    if before is UNKNOWN or after is UNKNOWN:
        return UNKNOWN
    return None                                          # 两侧都「不适用」


def _steps_section(base_run, cur_run):
    """轮次与返工的逐 step 对照（字段名照抄 trace.md §2.3，不新造语义）。"""
    base_steps = _step_by_no(base_run)
    cur_steps = _step_by_no(cur_run)
    rows = []
    for number in sorted(set(base_steps) | set(cur_steps), key=lambda n: _sort_key(n, "")):
        base_step = base_steps.get(number)
        cur_step = cur_steps.get(number)
        if base_step is None or cur_step is None:
            # 一侧没有这一步：那侧的值是「拿不到」，不是 0 轮、也不是 0 次返工。
            _note("step=%s 只在%s里出现，另一侧记 unknown" % (number, "基准" if cur_step is None else "本次"))
        row = {"n": number}
        for field in ("rounds", "rework"):
            before = _step_metric(base_step, field)
            after = _step_metric(cur_step, field)
            row[field] = {"before": before, "after": after, "delta": _delta(before, after)}
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# 主对比
# --------------------------------------------------------------------------- #
def compare(base_run, cur_run):
    """返回 `regression` 段（形状见 trace.md §6）。"""
    base_index, base_dupes = _assert_index(base_run)
    cur_index, cur_dupes = _assert_index(cur_run)
    for key in base_dupes:
        _note("基准里断言重复，只认先出现的那条：step=%s expr=%s" % (key[0], key[1]))
    for key in cur_dupes:
        _note("本次里断言重复，只认先出现的那条：step=%s expr=%s" % (key[0], key[1]))

    new_failures = []
    fixed = []
    changed = []
    added = []
    removed = []
    uncompared = []

    for key in sorted(set(base_index) | set(cur_index), key=lambda k: _sort_key(k[0], k[1])):
        step_no, expr = key
        base_item = base_index.get(key)
        cur_item = cur_index.get(key)

        if base_item is None:              # 基准里没有这条 → added，不算新增失败
            added.append({"step": step_no, "expr": expr})
            continue
        if cur_item is None:               # 本次这条消失 → removed，不算已修复
            removed.append({"step": step_no, "expr": expr})
            continue

        base_result = base_item.get("result", UNKNOWN)
        cur_result = cur_item.get("result", UNKNOWN)

        if base_result not in KNOWN_RESULTS:
            uncompared.append({"step": step_no, "expr": expr, "reason": "baseline.unknown"})
            continue
        if cur_result not in KNOWN_RESULTS:
            uncompared.append({"step": step_no, "expr": expr, "reason": "current.unknown"})
            continue

        if base_result != cur_result:
            changed.append(
                {"step": step_no, "expr": expr, "before": base_result, "now": cur_result}
            )

        if base_result == "pass" and cur_result in CUR_BROKEN:
            new_failures.append({
                "step": step_no,
                "expr": expr,
                "before": base_result,
                "now": cur_result,
                "detail": cur_item.get("detail") or "",
            })
        elif base_result in BASE_NOT_PASS and cur_result == "pass":
            fixed.append({
                "step": step_no,
                "expr": expr,
                "before": base_result,
                "now": cur_result,
            })

    cost = _cost_section(base_run, cur_run)
    asserts = _asserts_section(base_run, cur_run)
    asserts["added"] = added
    asserts["removed"] = removed
    asserts["changed"] = changed
    steps = _steps_section(base_run, cur_run)

    verdict, verdict_reasons = _verdict(
        base_run=base_run,
        cur_run=cur_run,
        new_failures=new_failures,
        fixed=fixed,
        cost=cost,
        asserts_delta=asserts["delta"],
        steps=steps,
    )
    for reason in verdict_reasons:
        _note("verdict=%s 的依据：%s" % (verdict, reason))

    return {
        "new_failures": new_failures,
        "fixed": fixed,
        "cost": cost,
        "asserts": asserts,
        "steps": steps,
        "uncompared": uncompared,
        "verdict": verdict,
    }


def _verdict(base_run, cur_run, new_failures, fixed, cost, asserts_delta, steps):
    """§6 优先级：unknown → mixed → regressed → improved → unchanged，一次只出一个。"""
    reasons = []

    # 1) 关键字段拿不到 → unknown（缺证据不许给结论，§4/§5.5）
    missing = []
    if not _steps(base_run):
        missing.append("baseline.steps 为空，断言无从对齐")
    if not _steps(cur_run):
        missing.append("current.steps 为空，断言无从对齐")
    if cost["before"]["usd"] == UNKNOWN:
        missing.append("baseline.cost.usd = unknown")
    if cost["after"]["usd"] == UNKNOWN:
        missing.append("current.cost.usd = unknown")
    if _aggregate_pass_rate(base_run) == UNKNOWN:
        missing.append("baseline 通过率 = unknown")
    if _aggregate_pass_rate(cur_run) == UNKNOWN:
        missing.append("current 通过率 = unknown")
    if missing:
        return "unknown", missing

    improved_side = []
    if fixed:
        improved_side.append("%d 条断言已修复" % len(fixed))
    if cost["direction"] == "down":
        improved_side.append("成本下降（delta_usd=%s）" % cost["delta_usd"])
    if _is_num(asserts_delta) and asserts_delta > 0:
        improved_side.append("通过率上升（delta=%s）" % asserts_delta)

    if new_failures and improved_side:
        reasons.append("%d 条新增失败（优先列出）" % len(new_failures))
        reasons.extend(improved_side)
        return "mixed", reasons
    if new_failures:
        reasons.append("%d 条新增失败" % len(new_failures))
        return "regressed", reasons
    if improved_side:
        reasons.extend(improved_side)
        return "improved", reasons

    reasons.append("四段全空/全相等（新增失败 0、已修复 0、成本未降、通过率未升）")
    if cost["direction"] == "up":
        reasons.append(
            "注意：成本上升（delta_usd=%s）但契约 §6 的 verdict 枚举里没有"
            "「仅成本变差」这一档，按第 3 条记 unchanged；up 的事实保留在 cost.direction" % cost["delta_usd"]
        )
    better_rework = [
        row["n"] for row in steps
        if _is_num(row["rework"]["delta"]) and row["rework"]["delta"] < 0
    ]
    if better_rework:
        reasons.append(
            "注意：step=%s 返工次数下降，但契约 §6 的 verdict 枚举只认 fixed / 成本降 / 通过率升，"
            "不拿返工变化改判 → 仍记 unchanged；事实保留在 regression.steps[].rework"
            % ",".join(str(n) for n in better_rework)
        )
    return "unchanged", reasons


def compare_documents(base_run, cur_run, base_path=None, cur_path=None):
    """拼出落盘的完整对象（形状见 trace.md §6「输出形状」）。"""
    document = {"baseline": None, "regression": None}
    if base_run is None:
        _note("没有 --base：regression: null（不适用，不是 unchanged）")
        return document
    document["baseline"] = {"run_id": _run_id(base_run), "path": base_path}
    document["regression"] = compare(base_run, cur_run)
    _note("base=%s" % base_path)
    _note("cur=%s" % cur_path)
    return document


def history_notes(base_run, cur_run):
    """诚实声明 run.json 装不下的东西：逐条「首次结果」不在 swap 里，不许反推 fixed。"""
    notes = []
    for label, run in (("基准", base_run), ("本次", cur_run)):
        for step in _steps(run):
            asserts = step.get("asserts") or {}
            first = (step.get("assert_pass") or {}).get("first_pass_rate")
            non_pass = [
                item.get("expr")
                for item in (asserts.get("items") or [])
                if isinstance(item, dict) and item.get("result") in BASE_NOT_PASS
            ]
            if _is_num(first) and first < 1 and not non_pass:
                notes.append(
                    "%s step=%s first_pass_rate=%s 但收尾结果全是 pass："
                    "逐条首次结果不在 run.json 里，对应断言只能算 added/removed/changed，"
                    "不许反推成 fixed（不脑补）"
                    % (label, _step_no(step), first)
                )
    return notes


def exit_code_for(verdict):
    return EXIT_BY_VERDICT.get(verdict, 3)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m view.regress",
        description="回归对比：本次 vs 基准（口径 = contract/trace.md §6）",
    )
    parser.add_argument("--base", metavar="RUN_JSON", help="基准 run.json（缺省 → regression: null）")
    parser.add_argument("--cur", metavar="RUN_JSON", help="本次 run.json（必填）")
    parser.add_argument("--out", metavar="PATH", help="落盘路径（写 runs/regress-*.json；与 stdout 字节一致）")
    parser.add_argument("--self-test", action="store_true", help="跑 view/fixtures/ 夹具自检")
    return parser


def _write_out(path, text):
    location = Path(path)
    try:
        if location.parent and not location.parent.exists():
            location.parent.mkdir(parents=True, exist_ok=True)
        # 不加 BOM、不转换行：与 stdout 字节一致（§7.2）
        location.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ToolError("write_failed", "写不了 %s：%s" % (path, exc), "检查目录权限", 1)


def run(argv=None):
    args = build_parser().parse_args(argv)

    if args.self_test:
        return run_self_test()

    if not args.cur:
        return _emit_error(
            ToolError(
                "missing_arg",
                "缺 --cur",
                "用法：python -m view.regress --base <base.json> --cur <cur.json> [--out <path>]",
                1,
            )
        )

    try:
        cur_run = load_run(args.cur)
        base_run = load_run(args.base) if args.base else None
        document = compare_documents(base_run, cur_run, args.base, args.cur)

        regression = document["regression"]
        if regression is not None:
            for note in history_notes(base_run, cur_run):
                _note(note)

        text = dumps(document)
        if args.out:
            _write_out(args.out, text)

        write_stdout(text)

        if regression is None:
            _note("regression=null（不适用），exit=0")
            return 0

        verdict = regression["verdict"]
        _note(
            "new_failures=%d fixed=%d cost.direction=%s is_lower_bound=%s verdict=%s exit=%d"
            % (
                len(regression["new_failures"]),
                len(regression["fixed"]),
                regression["cost"]["direction"],
                regression["cost"]["is_lower_bound"],
                verdict,
                exit_code_for(verdict),
            )
        )
        return exit_code_for(verdict)

    except ToolError as exc:
        return _emit_error(exc)
    except Exception as exc:  # noqa: BLE001 — 工具自身故障：堆栈给人看，stdout 只放 JSON
        traceback.print_exc(file=sys.stderr)
        return _emit_error(ToolError("internal_error", "内部异常：%s" % exc, "见 stderr 堆栈", 1))


# --------------------------------------------------------------------------- #
# 自检：只用 view/fixtures/ 的编造夹具，不读 runs/、不读 .plan/
# --------------------------------------------------------------------------- #
def _selftest_case(case):
    base_path = FIXTURES_DIR / case / "base.json"
    cur_path = FIXTURES_DIR / case / "cur.json"
    base_run = load_run(str(base_path))
    cur_run = load_run(str(cur_path))
    return compare_documents(base_run, cur_run, str(base_path), str(cur_path))


def run_self_test():
    checks = []

    def check(name, actual, expected):
        ok = actual == expected
        checks.append((ok, name, actual, expected))
        return ok

    def check_true(name, value):
        return check(name, bool(value), True)

    try:
        # ---- case A：新增失败 + 已修复 + added/removed + 成本下降（下限）→ mixed
        doc_a = _selftest_case("case-a-new-failure")
        reg_a = doc_a["regression"]
        check("A.baseline.run_id", doc_a["baseline"]["run_id"], "case-a-base")
        check(
            "A.new_failures",
            [(item["step"], item["expr"], item["now"]) for item in reg_a["new_failures"]],
            [(1, "cmd:python -m collect.score --self-test", "fail")],
        )
        check(
            "A.new_failures.detail",
            [item["detail"] for item in reg_a["new_failures"]],
            ["exit=1"],
        )
        check(
            "A.fixed",
            [(item["step"], item["expr"], item["before"]) for item in reg_a["fixed"]],
            [
                (1, "ack:看板在 1280 宽下不横向滚动", "unacked"),
                (1, "grep:view/@contract/slices.md", "fail"),
            ],
        )
        check(
            "A.asserts.added",
            [(item["step"], item["expr"]) for item in reg_a["asserts"]["added"]],
            [(1, "file:exists:view/regress.py")],
        )
        check(
            "A.asserts.removed",
            [(item["step"], item["expr"]) for item in reg_a["asserts"]["removed"]],
            [(1, "file:exists:collect/old_dir.py")],
        )
        check("A.asserts.before", reg_a["asserts"]["before"], 0.6)
        check("A.asserts.after", reg_a["asserts"]["after"], 0.8)
        check("A.asserts.delta", reg_a["asserts"]["delta"], 0.2)
        check("A.cost.delta_usd", reg_a["cost"]["delta_usd"], -0.012)
        check("A.cost.delta_pct", reg_a["cost"]["delta_pct"], -40.0)
        check("A.cost.direction", reg_a["cost"]["direction"], "down")
        check("A.cost.is_lower_bound", reg_a["cost"]["is_lower_bound"], True)
        check("A.verdict", reg_a["verdict"], "mixed")          # mixed 优先于 regressed
        check("A.exit", exit_code_for(reg_a["verdict"]), 2)

        # ---- case B：一侧 cost.usd = unknown → 两个 delta 都 unknown，verdict unknown
        reg_b = _selftest_case("case-b-unknown-cost")["regression"]
        check("B.cost.before.usd", reg_b["cost"]["before"]["usd"], UNKNOWN)
        check("B.cost.delta_usd", reg_b["cost"]["delta_usd"], UNKNOWN)
        check("B.cost.delta_pct", reg_b["cost"]["delta_pct"], UNKNOWN)
        check("B.cost.direction", reg_b["cost"]["direction"], UNKNOWN)
        check("B.cost.is_lower_bound", reg_b["cost"]["is_lower_bound"], True)
        check_true("B.cost.只并列两侧原值", reg_b["cost"]["after"]["usd"] == 0.01)
        check("B.new_failures", reg_b["new_failures"], [])
        check("B.verdict", reg_b["verdict"], "unknown")        # 不许默认 pass/unchanged
        check("B.exit", exit_code_for(reg_b["verdict"]), 3)

        # ---- case C：一模一样 → unchanged，delta 是 0 而不是缺失
        reg_c = _selftest_case("case-c-unchanged")["regression"]
        check("C.new_failures", reg_c["new_failures"], [])
        check("C.fixed", reg_c["fixed"], [])
        check("C.cost.delta_usd", reg_c["cost"]["delta_usd"], 0.0)
        check("C.cost.delta_pct", reg_c["cost"]["delta_pct"], 0.0)
        check("C.cost.direction", reg_c["cost"]["direction"], "flat")
        check("C.cost.is_lower_bound", reg_c["cost"]["is_lower_bound"], False)
        check("C.asserts.delta", reg_c["asserts"]["delta"], 0.0)
        check("C.verdict", reg_c["verdict"], "unchanged")
        check("C.exit", exit_code_for(reg_c["verdict"]), 0)

        # ---- case D：一侧计划里没有断言 → null（不适用），不许出 0/0、0%
        reg_d = _selftest_case("case-d-no-asserts")["regression"]
        check("D.asserts.before", reg_d["asserts"]["before"], None)
        check("D.asserts.after", reg_d["asserts"]["after"], 1.0)
        check("D.asserts.delta", reg_d["asserts"]["delta"], None)
        check_true("D.asserts.added 非空", len(reg_d["asserts"]["added"]) == 2)
        check("D.verdict", reg_d["verdict"], "unchanged")
        text_d = dumps(_selftest_case("case-d-no-asserts"))
        check_true("D.输出里没有 NaN", "NaN" not in text_d)
        check_true("D.输出里没有 0/0", "0/0" not in text_d)
        check_true("D.输出里没有 Infinity", "Infinity" not in text_d)

        # ---- 步骤段：§2.3 的 rounds / rework 逐 step 并列 + delta
        check(
            "A.steps",
            [
                (row["n"], row["rounds"]["before"], row["rounds"]["after"],
                 row["rounds"]["delta"], row["rework"]["before"],
                 row["rework"]["after"], row["rework"]["delta"])
                for row in reg_a["steps"]
            ],
            [(1, 2, 2, 0, 1, 1, 0)],
        )
        check(
            "C.steps",
            [(row["n"], row["rounds"]["delta"], row["rework"]["delta"]) for row in reg_c["steps"]],
            [(1, 0, 0)],
        )
        check(
            "D.steps",
            [(row["n"], row["rounds"]["before"], row["rework"]["before"]) for row in reg_d["steps"]],
            [(1, 1, 0)],
        )
        # 一侧没有这一步 → 那侧的 rounds/rework 记 unknown，delta 也 unknown（不许当 0）
        lone = compare_documents(
            {"id": "lone-base", "steps": [{"n": 1, "rounds": 3, "rework": 2, "asserts": {"total": 1, "passed": 1, "items": []}}]},
            {"id": "lone-cur", "steps": [{"n": 2, "rounds": 1, "rework": 0, "asserts": {"total": 1, "passed": 1, "items": []}}]},
            "base.json",
            "cur.json",
        )["regression"]
        check(
            "steps.一侧缺步 → unknown",
            [(row["n"], row["rounds"]["after"], row["rounds"]["delta"],
              row["rework"]["after"], row["rework"]["delta"]) for row in lone["steps"]],
            [(1, UNKNOWN, UNKNOWN, UNKNOWN, UNKNOWN),
             (2, 1, UNKNOWN, 0, UNKNOWN)],
        )
        # rounds 本身 unknown → delta 严格传播成 unknown，不按剩下的减
        unknown_rounds = compare_documents(
            {"id": "u-base", "cost": {"usd": 0.01, "complete": True},
             "steps": [{"n": 1, "rounds": UNKNOWN, "rework": 1, "asserts": {"total": 1, "passed": 1, "items": []}}]},
            {"id": "u-cur", "cost": {"usd": 0.01, "complete": True},
             "steps": [{"n": 1, "rounds": 2, "rework": 0, "asserts": {"total": 1, "passed": 1, "items": []}}]},
            "base.json",
            "cur.json",
        )["regression"]
        check(
            "steps.rounds=unknown → delta unknown",
            (unknown_rounds["steps"][0]["rounds"]["delta"],
             unknown_rounds["steps"][0]["rework"]["delta"]),
            (UNKNOWN, -1),
        )

        # ---- B 路硬边界：`.plan/**` 一律拒读（读一眼也算越界）
        for bad in (".plan/plan.json", ".plan/ledger.jsonl", "runs/../.plan/gates.log"):
            try:
                refuse_plan_path(bad)
                check_true("拒绝 .plan 路径：%s" % bad, False)
            except ToolError as exc:
                check("拒绝 .plan 路径（%s）code" % bad, exc.code, "lane_b_plan_path")
                check("拒绝 .plan 路径（%s）exit" % bad, exc.exit_code, 1)
        check_true(
            "非 .plan 路径照常放行",
            str(refuse_plan_path("runs/fx-rework01/run.json")).endswith("run.json"),
        )

        # ---- 出口纪律：同一份输入两次序列化必须字节一致；末尾恰好一个换行
        first = dumps(_selftest_case("case-a-new-failure"))
        second = dumps(_selftest_case("case-a-new-failure"))
        check_true("序列化字节级可复现", first == second)
        check_true("末尾恰好一个 \\n", first.endswith("\n") and not first.endswith("\n\n"))
        check_true("stdout 是单个 JSON 对象", isinstance(json.loads(first), dict))
        check_true("输出无 BOM", not first.startswith("\ufeff"))
        check_true("序列化不含 \\r（stdout 与 --out 要字节一致）", "\r" not in first)

        # ---- 契约里的空值纪律：不许用 0 冒充缺失
        for label, regression in (("A", reg_a), ("B", reg_b), ("C", reg_c), ("D", reg_d)):
            check_true(
                "%s.cost.is_lower_bound 是 bool" % label,
                isinstance(regression["cost"]["is_lower_bound"], bool),
            )
            check_true(
                "%s.cost.delta_usd 不写成 0 冒充 unknown" % label,
                not (
                    regression["cost"]["before"]["usd"] == UNKNOWN
                    and regression["cost"]["delta_usd"] == 0.0
                ),
            )

        # ---- 没有 --base → regression: null（不适用，不是 unchanged）
        doc_no_base = {"baseline": None, "regression": None}
        cur_only = load_run(str(FIXTURES_DIR / "case-c-unchanged" / "cur.json"))
        doc_no_base = compare_documents(None, cur_only, None, "cur.json")
        check("无 base → regression", doc_no_base["regression"], None)
        check("无 base → baseline", doc_no_base["baseline"], None)

        # ---- 基准缺失（文件不在）必须报 run_json_missing / 退出码 3，不许静默空报告
        try:
            load_run(str(FIXTURES_DIR / "case-a-new-failure" / "nope.json"))
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
            sys.stderr.write("[regress][ok]   %s\n" % name)
        else:
            sys.stderr.write("[regress][FAIL] %s\n  期望：%r\n  实际：%r\n" % (name, expected, actual))
    sys.stderr.write(
        "[regress][self-test] %d 项，失败 %d 项\n" % (len(checks), len(failed))
    )

    write_stdout(dumps({
        "ok": not failed,
        "self_test": {
            "checks": len(checks),
            "failed": len(failed),
            "failed_names": [item[1] for item in failed],
            "fixtures": str(FIXTURES_DIR.name),
        },
    }))
    return 0 if not failed else 1


def main(argv=None):
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
