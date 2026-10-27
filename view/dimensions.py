#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/dimensions.py · 四维并列接入（运行性能 / 判据覆盖 / 交互性 / 多模态）

口径来源：`contract/dimensions.md`（裁决与落点）+ `contract/trace.md` §4（三态）。

三条硬规矩：

1. **四维并列，不合成总分。** 本模块只把四个维各自的原始数摆在一起，不加权、不分级、
   不设阈值（出现这类字段即越界，见 dimensions.md §2.3 / §7.2）。
2. **view 不自己算一个数。** 这里所有值都是**原样抄**传送带文件里的值：
   `runs/<run_id>/measure.json` / `coverage.json` / `interact.json`。
   不把 `wall_s / turns` 当 `out_tokens_per_wall_s`，不给 `p90` 硬凑一个值，
   不按均价乘 token 算 cost —— 那些都是编数字。
3. **缺就是缺。** 文件不在、字段是 `"unknown"` / `null`，整段写 `"unknown"`
   或 `"not_applicable"`，**绝不用 0 兜底**（trace.md §4）。

边界：只读 `--run` 指向的那份 `run.json` **同一目录**下的三个传送带文件；
`.plan/**`、`collect/**`、`interact/**` 的内部实现一次都不碰（读入口先过 `refuse_plan_path()`）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .export import UNKNOWN, is_int, is_num, refuse_plan_path

#: 四维的固定键名与顺序（并列，谁也不是谁的子项）
DIMENSION_KEYS = ("performance", "coverage", "interactivity", "modalities")

#: 允许读的三个传送带文件（就在 run.json 旁边；别的文件一概不读）
MEASURE_FILE = "measure.json"
COVERAGE_FILE = "coverage.json"
INTERACT_FILE = "interact.json"

#: dimensions.md §6「不能支撑什么」—— **原话照抄**，md 里每维的正文第一行用它。
#: §7 是禁令，不是这一节，别抄错段。
NOT_SUPPORTED = {
    "performance": (
        "**中转延迟混进 wall。** `wall_s` 含网络往返与中转站排队，**不能当模型速度**；"
        "同一份提示换一条线路，`wall_s` 会变而模型没变。",
        "**n=1 没有置信区间。** 夹具只有一份、单机串行跑一次，所有秒数都是**点估计**，"
        "没有区间、没有重复采样，不能说「显著更快/更慢」。",
        "**性能数不能跨机器比。** 换 CPU、换磁盘、换网络、换时段都会动 `wall_s` 与 `round_gap`；"
        "只能同一台机器、同一份夹具上纵向比。另外 `ttft_s` / `tool_calls` 两项**全程缺席**，"
        "任何「首 token 快不快」「工具调用多不多」的说法在本项目里都没有证据。",
    ),
    "coverage": (
        "**覆盖表 `semantic` 级不能当通过。** 它只有 jev 概率或人工印象，不进任何门禁；"
        "把 `semantic` 算成 pass 就是把「有人看过」当成「做到了」。",
    ),
    "interactivity": (
        "**脚本用户不测自然对话。** 我们与 tau-bench 划清界限：tau-bench 用 LLM 扮演用户；"
        "我们用手写脚本。因此测的是「面对固定追问序列时的响应质量」，**不测自然对话能力**，"
        "也不覆盖「用户话说不清」的情况。",
    ),
    "modalities": (
        "**多模态全 `not_applicable` 等于没测。** image/audio/video/pdf 四行都不是"
        "「测了得了 0 分」，而是「这一版根本没送过任何非文本输入」——"
        "不能据此说 bench「支持多模态」或「多模态表现差」。",
    ),
}


# --------------------------------------------------------------------------- #
# 读传送带文件：缺文件 / 坏 JSON 就是「拿不到」，不推断、不修补（trace.md §4）
# --------------------------------------------------------------------------- #
def sidecar_path(run_path, name):
    """传送带文件只在 `run.json` 的**同一目录**下找；不在就返回 None。"""
    location = refuse_plan_path(Path(run_path))
    candidate = location.resolve().parent / name
    return candidate if candidate.is_file() else None


def load_sidecar(run_path, name):
    """读一份传送带 JSON；文件不在 / 读不了 / 不是对象 → None（调用方写 unknown）。"""
    path = sidecar_path(run_path, name)
    if path is None:
        return None
    refuse_plan_path(path)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _value(source, key):
    """原样取值：没有这个 key 或值是 null → `"unknown"`（拿不到），别的都照抄。"""
    if not isinstance(source, dict) or key not in source:
        return UNKNOWN
    value = source[key]
    return UNKNOWN if value is None else value


def _number_or_unknown(source, key):
    value = _value(source, key)
    return value if is_num(value) else UNKNOWN


def _copy(source, keys):
    """按固定键序**原样**抄一段：缺的键写 `unknown`，`null` 保持 `null`（§4 三态各回各位）。"""
    if not isinstance(source, dict):
        return {key: UNKNOWN for key in keys}
    return {key: source[key] if key in source else UNKNOWN for key in keys}


# --------------------------------------------------------------------------- #
# 四维：每段并列，段内只放该维自己的原料
# --------------------------------------------------------------------------- #
ROUND_GAP_KEYS = ("n", "p50", "p90", "p90_reason", "max")
COVERAGE_COUNT_KEYS = ("mechanical", "semantic", "none", "not_applicable", "total")
UNREGISTERED_KEYS = ("clauses_total", "registered", "delta")


def performance_section(measure):
    """运行性能：`measure.json` 原值（`ttft_s` / `tool_calls` 结构上拿不到就是 unknown）。"""
    gap = measure.get("round_gap") if isinstance(measure, dict) else None
    section = {
        "status": UNKNOWN,
        "source": MEASURE_FILE,
        "wall_s": _number_or_unknown(measure, "wall_s"),
        "turns": _value(measure, "turns"),
        "out_tokens_per_wall_s": _number_or_unknown(measure, "out_tokens_per_wall_s"),
        "round_gap": _copy(gap, ROUND_GAP_KEYS),
        "ttft_s": _value(measure, "ttft_s"),
        "tool_calls": _value(measure, "tool_calls"),
    }
    if any(
        is_num(section[key]) for key in ("wall_s", "turns", "out_tokens_per_wall_s")
    ):
        section["status"] = "measured"
    return section


def coverage_section(coverage):
    """判据覆盖：只出覆盖表（计数 + 逐条证据级），**不出总分**（dimensions.md §2）。"""
    counts = coverage.get("counts") if isinstance(coverage, dict) else None
    section = {
        "status": UNKNOWN,
        "source": COVERAGE_FILE,
        "counts": _copy(counts, COVERAGE_COUNT_KEYS),
        "unregistered_clauses": _copy(
            coverage.get("unregistered_clauses") if isinstance(coverage, dict) else None,
            UNREGISTERED_KEYS,
        ),
        "discriminating_power": _value(coverage, "discriminating_power"),
    }
    if any(is_int(section["counts"][key]) for key in COVERAGE_COUNT_KEYS):
        section["status"] = "measured"
    return section


INTERACT_KEYS = ("id", "turns_to_done", "verdict")


def interactivity_section(interact):
    """交互性：三个场景的 `turns_to_done` / `verdict` + 成本，全是 `interact.json` 原值。"""
    scenarios = interact.get("scenarios") if isinstance(interact, dict) else None
    rows = []
    for item in scenarios or []:
        if isinstance(item, dict):
            rows.append(_copy(item, INTERACT_KEYS))
    section = {
        "status": "measured" if rows else UNKNOWN,
        "source": INTERACT_FILE,
        "scenarios": rows,
        "cost": _number_or_unknown(interact, "cost"),
    }
    return section


MODALITY_KEYS = ("modality", "status", "reason", "evidence")


def modalities_section(measure):
    """多模态：逐模态登记表（`measured` / `not_applicable`），原值照抄。"""
    raw = measure.get("modalities") if isinstance(measure, dict) else None
    rows = []
    for item in raw or []:
        if isinstance(item, dict):
            rows.append(_copy(item, MODALITY_KEYS))
    statuses = {row["status"] for row in rows}
    if "measured" in statuses:
        status = "measured"
    elif rows and statuses == {"not_applicable"}:
        status = "not_applicable"
    else:
        status = UNKNOWN
    return {
        "status": status,
        "source": MEASURE_FILE,
        "rows": rows,
    }


def build_dimensions(run_path):
    """从 run.json 旁边的三个传送带文件拼出四段；缺哪份就哪段 `unknown`。

    返回值固定四个键（顺序 = `DIMENSION_KEYS`），**互不合成**。
    """
    measure = load_sidecar(run_path, MEASURE_FILE)
    coverage = load_sidecar(run_path, COVERAGE_FILE)
    interact = load_sidecar(run_path, INTERACT_FILE)
    return {
        "performance": performance_section(measure),
        "coverage": coverage_section(coverage),
        "interactivity": interactivity_section(interact),
        "modalities": modalities_section(measure),
    }


def empty_dimensions():
    """一份传送带文件都没有时的四段：全 `unknown`，不是 0。"""
    return {
        "performance": performance_section(None),
        "coverage": coverage_section(None),
        "interactivity": interactivity_section(None),
        "modalities": modalities_section(None),
    }


__all__ = [
    "COVERAGE_COUNT_KEYS",
    "DIMENSION_KEYS",
    "MODALITY_KEYS",
    "NOT_SUPPORTED",
    "ROUND_GAP_KEYS",
    "UNREGISTERED_KEYS",
    "build_dimensions",
    "coverage_section",
    "empty_dimensions",
    "interactivity_section",
    "load_sidecar",
    "modalities_section",
    "performance_section",
    "sidecar_path",
]
