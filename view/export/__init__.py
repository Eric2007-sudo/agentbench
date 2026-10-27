#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/export/ · 路 B 的导出三件套（json / csv / md）

口径来源：`contract/trace.md`
- §7.2 json = `run.json` 原文（不裁剪、不重排语义），导出文件与 `run.json` 逐字节相同（判据 A），
  stdout 与落盘文件逐字节相同（判据 B）；
  > 本步（#12）追加：四维（性能 / 判据覆盖 / 交互性 / 多模态）**不写进** `out/fx.json`，
  > 而是单独落 `out/dimensions.json`（`python -m view.cli dimensions`），`--json` 信封里的
  > `dimensions` 段与它是同一份对象；md 导出追加四节。
- §7.3 csv = 一行一个 step，表头顺序固定，缺失值 `unknown`、不适用 `""`，编码 `utf-8-sig`；
- §7.4 md = 三张表（Run 摘要 / Step 表 / 失败断言表），顺序固定；四维四节追加在后（每节第一行写「不能支撑什么」）；
- §7.5 确定性 = 同一份输入重跑导出字节相同（键序、浮点定点、`\n`、末尾一个换行）。

边界（contract/slices.md §2）：只吃 `run.json`；**一次都不读** `.plan/**`、宿主 db、
目标产物目录、`collect/**`；不 import `collect.*`；写集合 = `view/**` + `out/**`。

本模块只放三件套共用的底座（出口、读入、归一化、定点格式），
格式各自落在 `jsonio.py` / `csvio.py` / `mdio.py`，命令行在 `__main__.py`。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1
#: §4：`"unknown"` 是字符串字面量，不是 null、不是 NaN、不是 -1
UNKNOWN = "unknown"
#: §7.5 浮点定点：cost 6 位、比率 6 位；token 一律整数
COST_DIGITS = 6
RATE_DIGITS = 6
#: CLI 支持的格式（顺序即帮助里的顺序）
FORMATS = ("json", "csv", "md")


# --------------------------------------------------------------------------- #
# 出口：stdout 只放产物，人话一律 stderr（trace.md §7.1）
# --------------------------------------------------------------------------- #
def note(message):
    """人话、进度、警告只许走 stderr。"""
    sys.stderr.write("[export] " + message + "\n")


def dumps(document):
    """唯一的 JSON 序列化口径：`ensure_ascii=false`、`indent=1`、末尾恰好一个 `\\n`。"""
    return json.dumps(document, ensure_ascii=False, indent=1, allow_nan=False) + "\n"


def write_stdout_bytes(data):
    """stdout 必须是字节级的 UTF-8。

    Windows 上 `sys.stdout` 是文本模式，会把 `\\n` 翻成 `\\r\\n`，
    所以走 buffer 直接写字节 —— 否则「stdout 与导出文件字节一致」不成立。
    """
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:                      # 已被替换成纯文本流（自检用）
        sys.stdout.write(data.decode("utf-8", "replace"))
    else:
        stream.write(data)
    sys.stdout.flush()


def write_bytes_out(path, data):
    """落盘：二进制写、不转行尾、不加额外换行（`out/**` 是本路的写集合）。"""
    location = Path(path)
    try:
        if location.parent and not location.parent.exists():
            location.parent.mkdir(parents=True, exist_ok=True)
        location.write_bytes(data)
    except OSError as exc:
        raise ToolError("write_failed", "写不了 %s：%s" % (path, exc), "检查目录权限", 1)
    return location


class ToolError(Exception):
    """带出口码与机器可读 code 的故障（trace.md §7.1 错误体）。"""

    def __init__(self, code, message, hint="", exit_code=1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    """用法错误也走 §7.1 的统一出口（cli / export 两个入口共用）。

    argparse 默认 `error()` 是「往 stderr 写纯文本 + `exit(2)`」。这里两个都不行：
    退出码 `2` 在 §7.1 里是「跑完但结论是 fail / regressed」（判分结果，不是工具故障），
    与「参数打错了」撞车就再也分不清；stdout 还会空着。所以改成抛 `ToolError`，
    由统一出口吐 `{"ok": false, "error": {...}}`、退出码 `1`，人话仍在 stderr。

    子 parser 的 `parser_class` 默认就是调用方的类，因此 `report --format nope`
    这种「子命令上的错」也自动归到同一个出口。
    """

    def error(self, message):
        raise ToolError(
            "bad_usage",
            "参数错误：%s" % message,
            "用法：%s --help" % self.prog,
            1,
        )


def emit_error(exc):
    """机器读 stdout 里的 JSON 错误体，人读 stderr 里的话。"""
    document = {
        "ok": False,
        "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
    }
    write_stdout_bytes(dumps(document).encode("utf-8"))
    note(exc.message + (" | " + exc.hint if exc.hint else ""))
    return exc.exit_code


# --------------------------------------------------------------------------- #
# 读入：只碰 `runs/**/run.json`（B 路的硬边界）
# --------------------------------------------------------------------------- #
def refuse_plan_path(path):
    """`.plan/**` 一次都不许读（slices.md §2「绝对不碰」）。

    不是「顺手校验一下」，而是入口处的机械闸：路径里出现 `.plan` 这一段就直接按
    工具用法错误拒掉（退出码 1），**绝不打开文件** —— 越界读到的中间状态会污染判断。
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


def load_document(path):
    """读一份 run.json 原文，返回完整 document（含 `schema` 与 `run`）。"""
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
        raise ToolError("run_json_shape", "%s 顶层不是对象" % path, "形状见 trace.md §2.1", 3)
    return document


def run_section(document):
    """取 `run` 段；容忍没有 `run` 包装的裸 run 体（`regress.py` 同口径）。"""
    if isinstance(document, dict):
        run = document.get("run")
        if isinstance(run, dict):
            return run
        if "steps" in document:
            return document
    return {}


def steps_of(run):
    """`steps[]`：只认 dict，按 `n` 升序（§7.5 数组按稳定键排序）。"""
    raw = run.get("steps") if isinstance(run, dict) else None
    items = [step for step in (raw or []) if isinstance(step, dict)]
    return sorted(items, key=_step_sort_key)


def _step_sort_key(step):
    number = step.get("n")
    if is_int(number):
        return (0, number)
    return (1, 0)


# --------------------------------------------------------------------------- #
# 取值与归一化：`0` 只表示真的 0，`null` 表示不适用，`"unknown"` 表示拿不到（§4）
# --------------------------------------------------------------------------- #
def pick(mapping, key):
    """取字段。**没有这个 key 就是拿不到** → `"unknown"`；key 在、值是 null → `None`。"""
    if not isinstance(mapping, dict):
        return UNKNOWN
    return mapping[key] if key in mapping else UNKNOWN


def sub(mapping, key):
    """取子对象（`tokens` / `cost` / `asserts` / `assert_pass`）；不是 dict 就当空的。"""
    value = mapping.get(key) if isinstance(mapping, dict) else None
    return value if isinstance(value, dict) else {}


def is_int(value):
    """bool 不算 int（Python 里 `True == 1`，会毒化数）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def is_num(value):
    """number 判定：bool 不算 number。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def norm_int(value):
    """int 类字段（step/rounds/tokens/asserts/exit_code/rework）：不许用 0 冒充缺失。"""
    if value is None or value is UNKNOWN:
        return value
    if is_int(value):
        return value
    return UNKNOWN


def norm_num(value):
    """number 类字段（cost.usd / 比率）。"""
    if value is None or value is UNKNOWN:
        return value
    if is_num(value):
        return value
    return UNKNOWN


def norm_bool(value):
    """bool 类字段（complete / first_failed）。"""
    if value is None or value is UNKNOWN or isinstance(value, bool):
        return value
    return UNKNOWN


def fixed(value, digits):
    """§7.5 浮点定点：`0.0412` → `0.041200`。"""
    return "%.*f" % (digits, value)


#: §7.4 operator 追加：run 级「返工（合计）」= Σ `step.rework`，**口径必须与数写在同一行**。
#: 理由：`rounds` 有 run 级合计、`rework` 却只有 step 级，读报告的人得自己把 Step 表加一遍；
#: 而「没合计」很容易被读成「没返工」—— 那是「把不知道说成 0」旁边的另一个坑。
REWORK_TOTAL_NOTE = (
    "Σ step.rework（来自账本 reqs − 1，与 gates.log 的 exit code 不是同一个来源，"
    "两处不一致是设定、不是缺陷）"
)


def rework_total(run):
    """`run` 级返工合计 = Σ `step.rework`（§7.4 operator 追加）。

    守 §4：`rework` 是 int 类字段，**严格传播** —— 任何一步拿不到（`"unknown"` / 非 int）
    合计就是 `"unknown"`，不许跳过它按剩下的求和；`steps: 0` 时是真 0
    （没有步骤可返工，能数清，所以不写 `"unknown"`）。
    """
    total = 0
    for step in steps_of(run):
        value = norm_int(pick(step, "rework"))
        if not is_int(value):
            return UNKNOWN
        total += value
    return total


__all__ = [
    "SCHEMA_VERSION",
    "UNKNOWN",
    "COST_DIGITS",
    "RATE_DIGITS",
    "FORMATS",
    "REWORK_TOTAL_NOTE",
    "JsonArgumentParser",
    "ToolError",
    "dumps",
    "emit_error",
    "fixed",
    "is_int",
    "is_num",
    "load_document",
    "norm_bool",
    "norm_int",
    "norm_num",
    "note",
    "pick",
    "refuse_plan_path",
    "rework_total",
    "run_section",
    "steps_of",
    "sub",
    "write_bytes_out",
    "write_stdout_bytes",
]
