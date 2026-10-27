"""`collect/measure` 的共用原语 —— 只装「时间 / 三态 / 分位数 / JSON 出口」四件事。

口径来源：`contract/dimensions.md` §1（性能）、§4（多模态）、§5（字段映射）、§7（禁令）
与 `contract/trace.md` §4（`null` / `"unknown"` / `0` 三态）、§7.1（`--json` 出口）。

三条纪律：

1. **`"unknown"` 是字符串字面量**，不是 `null`、不是 `0`、不是 `-1`、不是 `NaN`（trace.md §4）。
2. 拿不到就是 `UNKNOWN`，绝不估算、绝不用别的量顶替（dimensions.md §1.3）。
3. 输出是纯函数：同一份输入 → 字节相同的 JSON；不写时间戳、不写绝对临时路径。
"""

import sys

sys.dont_write_bytecode = True

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

UNKNOWN = "unknown"

# 浮点定点位数（trace.md §7.5：cost / 比率 6 位小数；本模块的秒数同样 6 位）
PLACES = 6

# p90 的最少样本数（dimensions.md §1.4：n < 5 → p90 必须是 unknown）
P90_MIN_SAMPLES = 5


# ---------------------------------------------------------------- 三态 / 数值


def is_unknown(value):
    """严格判定「拿不到」。`null` 不是 `"unknown"`，`0` 更不是。"""
    return value == UNKNOWN and isinstance(value, str)


def is_number(value):
    """真数（bool 不算数，NaN / Infinity 不算数）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def round6(value):
    """定点到 6 位小数（十进制四舍五入，避免二进制浮点的 0.1+0.2 尾巴）。"""
    if not is_number(value):
        return UNKNOWN
    try:
        quant = Decimal(1).scaleb(-PLACES)
        return float(Decimal(repr(float(value))).quantize(quant, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return UNKNOWN


def int_or_unknown(value):
    """int 类字段：真 int 才留，否则 `"unknown"`（不写 0、不写 null）。"""
    if isinstance(value, bool):
        return UNKNOWN
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and value not in (float("inf"), float("-inf")):
        return int(value)
    return UNKNOWN


# ---------------------------------------------------------------- 时间


def parse_iso(text):
    """ISO8601 → 带时区的 `datetime`；不带时区 / 解析失败 / 非字符串 → `None`。

    「无时区」必须当作拿不到（dimensions.md §1.2：`rounds[].gap_s` 任一端无时区 → unknown），
    否则两台机器按各自本地时区解读同一个朴素时间戳，差出来的秒数就是编的。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def iso_of(moment):
    return moment.isoformat()


def to_epoch_ms(value):
    """ISO8601 字符串 或 整数毫秒 → epoch 毫秒（float）；拿不到 → `None`。

    整数毫秒来自宿主库的 `time_created` / `data.time.created`（`collect/ingest/host_db.py`
    给出的 `at` 已是 ISO，但自测与兜底都要能直接吃毫秒）。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float) and value == value:
        return value
    moment = parse_iso(value)
    if moment is None:
        return None
    try:
        return moment.timestamp() * 1000.0
    except (OverflowError, OSError, ValueError):
        return None


def ms_to_iso(ms):
    """epoch 毫秒 → 本地时区的 ISO8601（与 host_db.to_iso 同口径）。"""
    if not is_number(ms):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def delta_s(later, earlier):
    """两个时间端点之差（秒，6 位小数）；任一端拿不到 → `"unknown"`。

    端点可以是 ISO8601 字符串，也可以是 epoch 毫秒。
    """
    a = to_epoch_ms(later)
    b = to_epoch_ms(earlier)
    if a is None or b is None:
        return UNKNOWN
    return round6((a - b) / 1000.0)


# ---------------------------------------------------------------- 分位数


def nearest_rank(sorted_values, percent):
    """最近秩法（nearest-rank）：第 `ceil(percent/100 × n)` 个（1 起）。空集 → `None`。"""
    n = len(sorted_values)
    if n <= 0:
        return None
    if not isinstance(percent, int) or isinstance(percent, bool) or not 1 <= percent <= 100:
        raise ValueError("percent must be an int in 1..100")
    rank = (percent * n + 99) // 100
    if rank < 1:
        rank = 1
    return sorted_values[rank - 1]


def percentiles(gaps):
    """`round_gap` 段：样本 = 同一步内全部可得的 `gap_s`（dimensions.md §1.4）。

    - 空样本 → `p50` / `p90` / `max` 全 `null`（不适用，不是 unknown）；
    - 样本含 `"unknown"` → 三者全 `"unknown"`（严格传播，不跳过它按剩下的算）；
    - `n < 5` → `p90` 为 `"unknown"` 且 `p90_reason: "too_few_samples"`，`p50` / `max` 照报；
    - 永远同时给 `n`。
    """
    out = {"n": len(gaps), "p50": None, "p90": None, "max": None}
    if not gaps:
        return out
    if any(not is_number(gap) for gap in gaps):
        out["p50"] = UNKNOWN
        out["p90"] = UNKNOWN
        out["p90_reason"] = "unknown_sample"
        out["max"] = UNKNOWN
        return out
    ordered = sorted(float(gap) for gap in gaps)
    out["p50"] = round6(nearest_rank(ordered, 50))
    out["max"] = round6(ordered[-1])
    if len(ordered) < P90_MIN_SAMPLES:
        out["p90"] = UNKNOWN
        out["p90_reason"] = "too_few_samples"
    else:
        out["p90"] = round6(nearest_rank(ordered, 90))
    return out


# ---------------------------------------------------------------- JSON 出口

# dimensions.md §2.3 / §7 禁令：这几个字段名一律不许出现在 measure.json 里
# 简报警令 ⑭ 追加：也不许出现 rate / pct / percent / ratio / coverage 这类合成分数键
FORBIDDEN_KEYS = (
    "score",
    "weight",
    "grade",
    "threshold",
    "rate",
    "pct",
    "percent",
    "ratio",
    "coverage",
)

# 门禁是**文件级子串 grep**（`nogrep:"grade"` 这类），所以这几个词连子串都不许出现。
# `ratio` 不在其中：OTel 名 `gen_ai.invoke_agent.duration` 里含 `ratio`（du-ratio-n），
# 那是 §5 要求保留的映射说明，不是分数字段。
FORBIDDEN_SUBSTRINGS = ("score", "weight", "grade", "threshold", "rate")


def dumps(obj):
    """唯一出口格式：UTF-8 无 BOM、`ensure_ascii=False`、`indent=1`、末尾一个 `\\n`（trace.md §7.1）。"""
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def forbidden_hits(obj, path=""):
    """扫全树，返回所有命中的禁用字段路径（只认字段名本身等于禁用词，大小写不敏感）。

判据只有一条：字段名不许是这四个词 —— `degraded_reasons` 这类包含子串的名字不算
（断言是 `grep:"score":`，带引号的是键名；误伤会让诚实声明无处可放）。
"""
    hits = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = "%s.%s" % (path, key) if path else str(key)
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                hits.append(here)
            hits.extend(forbidden_hits(value, here))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            hits.extend(forbidden_hits(value, "%s[%d]" % (path, index)))
    return hits


def write_text(path, text):
    """写盘：`utf-8` 无 BOM、`\\n` 换行、不加 BOM 不加时间戳。"""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
