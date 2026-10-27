"""collect/score/base.py —— 判分器共用原语：`"unknown"` 字面量、数值判定、固定容差。

口径来源（只读契约，不许改）：
- `contract/trace.md` §4：`null` = 已知为空 / 本就不适用；`"unknown"` = 拿不到；
  **严禁用 `0` 冒充缺失值**；`"unknown"` 是字符串字面量，不是 `null` / `NaN` / `-1`。
- `contract/trace.md` §5.3：浮点容差写死 `|a-b| <= max(1e-9, 1e-6 * max(|a|,|b|))`，不许临场调。

本模块不含任何来源读取与判定逻辑，只放四类判分共用的原语，避免各段各写一份口径。
"""

from __future__ import annotations

import math

#: 契约里的「拿不到」是字符串字面量，不是 None、不是 0、不是 NaN。
UNKNOWN = "unknown"

#: 判分产物自己的契约版本（与 `run.json` 的 `schema` 同号）。
SCHEMA = 1

#: 浮点容差（`contract/trace.md` §5.3，写死）。
ABS_TOL = 1e-9
REL_TOL = 1e-6
TOL_FORMULA = "|a-b| <= max(1e-9, 1e-6 * max(|a|,|b|))"

#: 数值固定精度（`contract/trace.md` §7.5：cost 与比率保留 6 位小数）。
PLACES = 6


def is_unknown(value) -> bool:
    """是不是契约里的 `"unknown"` 字面量（大小写敏感，别的都不算）。"""
    return isinstance(value, str) and value == UNKNOWN


def is_number(value) -> bool:
    """真数值：排除 bool（它在 Python 里是 int 子类）与 NaN / Inf。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def numbers_close(a, b) -> bool:
    """浮点容差比较；任一侧不是真数值 → `False`（缺失值不许参与数值比较）。"""
    if not (is_number(a) and is_number(b)):
        return False
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def round_places(value, places: int = PLACES):
    """定点到 `places` 位；非数值原样返回（`"unknown"` / `None` 不许被变成 0）。"""
    if not is_number(value):
        return value
    return round(float(value), places)


def rate(numerator, denominator):
    """算比率，严格按 `contract/trace.md` §4 的传播规则：

    - 分母 `null`（本就不适用，如这一步没有断言）→ `None`
    - 分母 `"unknown"`（拿不到）→ `"unknown"`
    - 分母 `0` → `None`（**不许出现 0/0、NaN、0% 当结论**）
    - 分子 `"unknown"` 而分母已知 → `"unknown"`
    - 其余 → 保留 6 位小数
    """
    if denominator is None:
        return None
    if is_unknown(denominator):
        return UNKNOWN
    if not is_number(denominator):
        return UNKNOWN
    if denominator == 0:
        return None
    if numerator is None:
        return None
    if is_unknown(numerator):
        return UNKNOWN
    if not is_number(numerator):
        return UNKNOWN
    return round_places(numerator / denominator)


def sum_known(values):
    """int 类字段的严格求和：**任一子项 `"unknown"` → 整体 `"unknown"`**，不跳过按剩下的求和。

    `None`（不适用）被跳过 —— 例如 amend 记账行确实不产生轮次。
    """
    total = 0
    for value in values:
        if is_unknown(value):
            return UNKNOWN
        if value is None:
            continue
        if not is_number(value):
            return UNKNOWN
        total += value
    return total


def tolerance_block() -> dict:
    """容差块：把口径连同数字一起写进产物，读者不用去猜实现里调了多少。"""
    return {
        "abs": ABS_TOL,
        "rel": REL_TOL,
        "formula": TOL_FORMULA,
        "applies_to": "结构里的数值（cost / 轮次 / bytes）；文本与产物本身仍以 sha256 为准，不做近似文本 diff",
    }


#: 这些字段名上的 `-1` 只可能是哨兵值（缺失被写成负数），必抓。
_MISSING_PRONE = (
    "usd", "total", "passed", "failed", "timeout", "unacked", "rounds", "bytes",
    "input", "output", "reasoning", "cache_read", "cache_write",
    "first_pass_rate", "final_pass_rate", "delta_pct", "unknown_rows", "added",
    "removed", "modified", "unchanged", "count",
)


def _leaf_name(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def find_fake_missing(document, path: str = ""):
    """扫描产物：抓「把不知道说成 0/-1/NaN」这类退化写法，返回问题列表（空 = 干净）。

    判据（`contract/trace.md` §4）：`"unknown"` 必须是字符串字面量；NaN / Inf 一律非法；
    计数 / 成本 / 轮次一类字段上的 `-1` 是哨兵值（真会等于 -1 的只有差值字段，不在此列）。
    """
    problems = []
    if isinstance(document, dict):
        for key, value in document.items():
            problems.extend(find_fake_missing(value, "%s.%s" % (path, key) if path else key))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            problems.extend(find_fake_missing(value, "%s[%d]" % (path, index)))
    elif isinstance(document, bool):
        pass
    elif isinstance(document, float) and not math.isfinite(document):
        problems.append("%s: NaN/Inf 不是合法值" % (path or "<root>"))
    elif is_number(document) and document == -1 and _leaf_name(path) in _MISSING_PRONE:
        problems.append("%s: -1 是哨兵值，缺失请写 \"unknown\"" % (path or "<root>"))
    elif (isinstance(document, str) and document.strip().lower() in ("nan", "none", "inf")
          and _leaf_name(path) in _MISSING_PRONE):
        problems.append("%s: %r 不是契约里的缺失写法（应为 \"unknown\" 或 null）" % (path or "<root>", document))
    return problems
