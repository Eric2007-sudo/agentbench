#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""view/export/jsonio.py · 导出 `json`（口径 = trace.md §7.2）

> 「= `run.json` 原文（不裁剪、不重排语义）。`--json` 输出与导出文件必须字节一致。」

所以这里只做一件事：把**读进来的整份 document** 按 §7.5 的确定性口径再序列化一次 ——
不删字段、不改字段名、不按语义重排键（Python 的 dict 保序，键序 = 输入里的键序），
`ensure_ascii=false`、`indent=1`、末尾恰好一个 `\\n`、无 BOM、不用 `\\r`。

「不裁剪」是硬约束：导出器不替采集侧做减法，也不替读者挑字段 ——
少一个字段就得回头改契约（contract/trace.md），不是在导出时悄悄丢掉。
"""

from __future__ import annotations

from . import dumps


def render_json(document):
    """返回 json 导出的**文本**（调用方按 UTF-8 编码落盘/写 stdout）。"""
    return dumps(document)


def encode_json(document):
    """json 导出的字节：UTF-8、无 BOM（§7.1）。"""
    return render_json(document).encode("utf-8")


__all__ = ["encode_json", "render_json"]
