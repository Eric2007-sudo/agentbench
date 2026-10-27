"""collect/coverage —— 判据覆盖表（`contract/dimensions.md` §2：机械 / 语义 / 未覆盖）。

这一路只回答「每条规格点现在靠什么证据」，**不出总分、不出百分比、不做加权**：

- `accept:<全局序号>` —— `plan.json` 的每条 `accept`，序号跨步连续（1..N），不写死条数；
- `clause:<短名>` —— trace.md 写明但没有断言覆盖、人工登记在 `spec/points.json` 的条款；
- `mechanical` 只给「score.json 的 `asserts.by_step[*].items` 里有原文完全相等的断言，
  且 `result ∈ {pass, fail}`」的规格点 —— 没验完（timeout/unacked/unknown）不算。

入口见 `__main__.py`（`python -m collect.coverage --self-test`）。
"""

from __future__ import annotations

from .table import (
    BANNED_KEY_PARTS,
    LEVELS,
    POINT_FIELDS,
    assert_index,
    build_table,
    check_document,
    check_point_evidence,
    classify_accept,
    consistency_guard,
    count_accepts,
    count_clauses,
    dumps,
    iter_accepts,
    iter_keys,
    load_json,
    load_points,
    plan_steps,
)

__all__ = [
    "BANNED_KEY_PARTS",
    "LEVELS",
    "POINT_FIELDS",
    "assert_index",
    "build_table",
    "check_document",
    "check_point_evidence",
    "classify_accept",
    "consistency_guard",
    "count_accepts",
    "count_clauses",
    "dumps",
    "iter_accepts",
    "iter_keys",
    "load_json",
    "load_points",
    "plan_steps",
]
