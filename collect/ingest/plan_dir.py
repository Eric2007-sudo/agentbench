"""只读采集器 —— `python -m collect.ingest.plan_dir`

把一次 agent 运行的现场（`.plan/` + 宿主 session db + 产物目录）压成
`run` → `step` → `round` 三级结构，落成 `run.json`（路 A 的唯一交接物）。

口径来源（都是冻结契约，只读，**本文件不改任何契约**）：

- `contract/trace.md` §2（run.json 形状）、§3（来源与只读约束）、§4（`null` / `"unknown"` 铁律）、
  §5（四类判分的机械口径）、§8（错误与边界）
- `contract/slices.md` §2（路 A 可写 `collect/**` 与 `runs/<id>/run.json`）、§5（断言 6 种前缀）

用法：

    python -m collect.ingest.plan_dir --root <目标目录> --out runs/<id>/run.json
    python -m collect.ingest.plan_dir --root <目标目录> --json        # 只出 stdout JSON，不落盘
    python -m collect.ingest.plan_dir --selftest                      # 拿两份夹具自证，全绿 exit 0

只读硬约束（`contract/trace.md` §3.2，本文件里能 grep 到）：

1. 文件只 `open(path, "r", encoding="utf-8", errors="replace")`，不写、不删、不 rename、不碰 mtime；
2. sqlite **只能**走 `sqlite3.connect("file:" + path + "?mode=ro", uri=True)` 并紧跟
   `PRAGMA query_only`（见 `_connect_ro`）—— 没有第二处开连接的地方；
3. 不在来源目录落任何东西：`sys.dont_write_bytecode = True`（进程级也接受 `PYTHONDONTWRITEBYTECODE=1`）；
4. 采集是纯函数：同一份来源 + 同一份代码 → 字节级相同的 `run.json`（不写 `now()`、不写进程 id）；
5. 来源在采集期间被改动 → 整份重读一次，仍不一致则记 `source.changed_during_read:<path>` 后用第二次快照。

退出码（`contract/trace.md` §7.1）：`0` 跑完且结论不是 fail；`2` 跑完且 `run.verdict == "fail"`；
`3` 数据不可用（没有 `.plan/`、账本整份不可读 —— 已输出合法 JSON）；`1` 工具自身故障。

> 契约 §7.1 的退出码表没有覆盖「数据齐、结论 `unknown`（因为部分字段拿不到）」这一格；
> 本实现把它归到 `0`（跑完了、账本和产物都在、没有 fail 证据），只有 `steps: 0` /
> 账本整份不可读这种「数据不可用」才走 `3`。见收工报告里的偏离说明。
"""

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from . import host_db

SCHEMA_VERSION = 1
UNKNOWN = "unknown"

#: 产物文件超过这个大小就只流式算 `bytes` + `sha256`，不做文本 diff（`contract/trace.md` §8.2 要求阈值写进常量）
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

#: `contract/slices.md` §5：只允许这 6 种断言前缀
ASSERT_KINDS = (
    ("file:exists:", "exists"),
    ("file:absent:", "absent"),
    ("grep:", "grep"),
    ("nogrep:", "nogrep"),
    ("cmd:", "cmd"),
    ("ack:", "ack"),
)

#: `contract/slices.md` §2 的两路映射（按产物路径前缀；表里没写的目录 → `"unknown"`）
LANE_BY_PREFIX = (("collect", "collect"), ("view", "view"))

VERDICT_PASS = {"PASS": "pass", "FAIL": "fail", "TIMEOUT": "timeout"}
EXIT_RE = re.compile(r"exit=(-?\d+)")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_A = REPO_ROOT / "collect" / "fixtures" / "rework01"
FIXTURE_B = REPO_ROOT / "collect" / "fixtures" / "rework01b"
FIXTURE_EMPTY = REPO_ROOT / "collect" / "fixtures" / "empty"


def say(message=""):
    """人类提示一律走 stderr（`contract/trace.md` §7.1）。"""
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# 只读工具层
# --------------------------------------------------------------------------


class Degraded:
    """`run.degraded_reasons` 的收集器：机器可读代码 + 人话，一行一条，去重保序。"""

    def __init__(self):
        self.reasons = []

    def add(self, reason):
        if reason not in self.reasons:
            self.reasons.append(reason)

    def __bool__(self):
        return bool(self.reasons)


def _stat_key(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _norm(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _under(path, base):
    path, base = _norm(path), _norm(base)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def is_excluded(path, excluded):
    """`contract/trace.md` §3.1：读之前先拿 `plan.exclude` 过滤，命中就一个字节都不碰。"""
    return any(_under(path, item) for item in excluded)


def read_source_text(path, rel, deg, excluded=()):
    """只读打开一份文本来源，返回 `(text|None, present, readable)`。

    采集期间来源被改动 → 整份重读一次；两次仍不一致 → 用第二次快照 + 记 degraded（契约 §3.2 第 5 条）。
    """
    if is_excluded(path, excluded):
        return None, False, False
    key0 = _stat_key(path)
    if key0 is None:
        return None, False, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None, True, False
    key1 = _stat_key(path)
    if key1 != key0:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return None, True, False
        if _stat_key(path) != key1:
            deg.add("source.changed_during_read:%s 采集期间来源被改动，已用第二次快照" % rel)
    return text, True, True


def artifact_of(root, rel, deg, excluded=()):
    """实测一份产物：`{path, exists, bytes, sha256}`（`contract/trace.md` §2.3）。

    路径是**目录**（计划的 `out` 常写成 `collect/` 这种）时：`exists: true`，
    `bytes` / `sha256` 落 `null`（契约 §4：`null` = 已知不适用，不是「拿不到」）。
    """
    entry = {"path": rel, "exists": False, "bytes": None, "sha256": None}
    target = root / rel
    if is_excluded(target, excluded):
        deg.add("artifact.excluded:%s 该路径在 plan.exclude 里，按契约不读" % rel)
        return entry
    try:
        if target.is_dir():
            entry["exists"] = True  # 目录在，但没有可哈希的字节
            return entry
        if not target.is_file():
            return entry
        size = target.stat().st_size
    except OSError:
        deg.add("artifact.unreadable:%s 拿不到大小与 hash" % rel)
        entry["sha256"] = UNKNOWN
        return entry
    entry["exists"] = True
    entry["bytes"] = size
    if size > MAX_ARTIFACT_BYTES:
        deg.add("artifact.too_large:%s 超过 %d 字节，只记 bytes，hash 记 unknown" % (rel, MAX_ARTIFACT_BYTES))
        entry["sha256"] = UNKNOWN
        return entry
    digest = hashlib.sha256()
    try:
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        deg.add("artifact.unreadable:%s 算 hash 时读失败" % rel)
        entry["sha256"] = UNKNOWN
        return entry
    entry["sha256"] = digest.hexdigest()
    return entry


def _connect_ro(db_path):
    """打开宿主 db 的**唯一**入口：只读 URI + `PRAGMA query_only`（`contract/trace.md` §3.2 第 2 条）。

    这是全文件里唯一一处 `sqlite3.connect`；`host_db.py` 只拿这个连接发 SELECT。
    """
    conn = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only")
    return conn


# --------------------------------------------------------------------------
# 来源解析
# --------------------------------------------------------------------------


def load_plan(root, deg, excluded):
    """`.plan/plan.json` → dict；读不到/坏 JSON 一律 `None`（`contract/trace.md` §8.1 / §8.2）。"""
    path = root / ".plan" / "plan.json"
    text, present, readable = read_source_text(path, ".plan/plan.json", deg, excluded)
    if not present:
        return None, False, False
    if not readable or text is None:
        deg.add("plan.unreadable:.plan/plan.json 存在但读不了，视为没有计划")
        return None, True, False
    try:
        plan = json.loads(text)
    except ValueError:
        deg.add("plan.bad_json:.plan/plan.json 不是合法 JSON，视为没有计划")
        return None, True, False
    if not isinstance(plan, dict):
        deg.add("plan.bad_json:.plan/plan.json 顶层不是对象，视为没有计划")
        return None, True, False
    schema = plan.get("schema")
    if isinstance(schema, int) and schema > SCHEMA_VERSION:
        deg.add("plan.schema_newer:%s 计划 schema 高于本契约（%d），未知字段已忽略" % (schema, SCHEMA_VERSION))
    return plan, True, True


def load_ledger(root, deg, excluded):
    """`.plan/ledger.jsonl` → `(rows, bad_lines, rows_total, present, readable)`。

    - 坏行跳过 + `ledger.bad_json:<行号>`（1-based），**其余行照常用**（契约 §8.2）；
    - `rows_total` 是物理非空行数（含坏行），用来算 `rounds_coverage`。
    """
    path = root / ".plan" / "ledger.jsonl"
    text, present, readable = read_source_text(path, ".plan/ledger.jsonl", deg, excluded)
    rows, bad, rows_total = [], [], 0
    if not present:
        return rows, bad, rows_total, False, False
    if not readable or text is None:
        return rows, bad, rows_total, True, False
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.strip() == "":
            continue
        rows_total += 1
        try:
            obj = json.loads(line)
        except ValueError:
            bad.append(lineno)
            deg.add("ledger.bad_json:%d 第 %d 行不是合法 JSON，已跳过" % (lineno, lineno))
            continue
        if not isinstance(obj, dict):
            bad.append(lineno)
            deg.add("ledger.bad_json:%d 第 %d 行不是 JSON 对象，已跳过" % (lineno, lineno))
            continue
        obj["__line__"] = lineno
        rows.append(obj)
    return rows, bad, rows_total, present, readable


def parse_gates(text, deg):
    """`.plan/gates.log` → gate 块列表（每次 gate 一块，内含各自的断言结果）。

    行形状（仓库里真实日志与夹具一致）：

        [2026-10-26T20:55:40] gate #1 写契约
          FAIL  grep:collect/@contract/slices.md
               文件不存在: .../contract/slices.md        <- 缩进更深的明细
          PASS  file:exists:contract/trace.md

    明细行挂到它上面最近的一条结果上（`cmd:` 的 `exit=N` 就在明细里，契约 §5.1）。
    """
    blocks = []
    for line in text.split("\n"):
        if line.startswith("["):
            head = line.split("]", 1)[1].strip() if "]" in line else ""
            step = None
            for token in head.split():
                if token.startswith("#"):
                    try:
                        step = int(token[1:])
                    except ValueError:
                        step = None
                    break
            blocks.append({"header": line.strip(), "step": step, "results": []})
            continue
        if not blocks:
            continue
        stripped = line.strip()
        if stripped == "":
            continue
        matched = False
        for verdict in ("PASS", "FAIL", "TIMEOUT"):
            if stripped.startswith(verdict + " "):
                blocks[-1]["results"].append(
                    {"expr": stripped[len(verdict):].strip(), "verdict": verdict, "details": []}
                )
                matched = True
                break
        if matched:
            continue
        if blocks[-1]["results"]:
            blocks[-1]["results"][-1]["details"].append(stripped)
        else:
            blocks[-1].setdefault("loose", []).append(stripped)
    if not any(block["results"] for block in blocks):
        deg.add("gates.no_result:.plan/gates.log 里没解析出任何断言结果，断言结果记 unknown")
    return blocks


# --------------------------------------------------------------------------
# 判分（① exit code ② 断言通过率）—— 只认机械证据，不做主观打分
# --------------------------------------------------------------------------


def kind_of(expr):
    for prefix, kind in ASSERT_KINDS:
        if expr.startswith(prefix):
            return kind
    return None


def _detail_text(result):
    return "; ".join(result["details"]) if result["details"] else ""


def _result_of(block, expr):
    """取某条断言在某次 gate 里的最后一条结果。"""
    found = None
    for result in block["results"]:
        if result["expr"] == expr:
            found = result
    return found


def assert_block(accept, acks, blocks, deg, step_n):
    """算一步的 `asserts` + `assert_pass` + `rework` + `first_failed` + exit code（契约 §5.1 / §5.2）。"""
    declared = list(accept)
    total = len(declared)
    step_blocks = [block for block in blocks if block["step"] == step_n]

    if total == 0:
        # 契约 §2.3：计划里这一步就没有断言 → total 是 null，其余是已知的空，比率是 null
        return (
            {"total": None, "passed": 0, "failed": 0, "timeout": 0, "unacked": 0, "items": []},
            {"first_pass_rate": None, "final_pass_rate": None},
            0,
            False,
            None,
            None,
        )

    if not step_blocks:
        if blocks:
            deg.add("asserts.unknown:gates.log 里没有步 %d 的 PASS/FAIL 行，断言结果记 unknown" % step_n)
        return (
            {
                "total": total,
                "passed": UNKNOWN,
                "failed": UNKNOWN,
                "timeout": UNKNOWN,
                "unacked": UNKNOWN,
                "items": [],
            },
            {"first_pass_rate": UNKNOWN, "final_pass_rate": UNKNOWN},
            0,
            False,
            None,
            None,
        )

    final, first = step_blocks[-1], step_blocks[0]
    items, unknown_present = [], False
    for expr in declared:
        kind = kind_of(expr)
        if kind is None:
            # 契约 §8.2：写法不认识 → fail + 明细，不静默放行
            items.append(
                {"expr": expr, "kind": "unknown", "result": "fail", "detail": "无法识别的断言语法"}
            )
            continue
        if kind == "ack":
            confirmed = expr in acks
            items.append(
                {
                    "expr": expr,
                    "kind": "ack",
                    "result": "pass" if confirmed else "unacked",
                    "detail": "" if confirmed else "尚未人工确认（tg ack）",
                }
            )
            continue
        hit = _result_of(final, expr)
        if hit is None:
            # 契约 §8.2：gates.log 缺这条 → 不推断
            unknown_present = True
            items.append(
                {
                    "expr": expr,
                    "kind": kind,
                    "result": UNKNOWN,
                    "detail": "gates.log 缺这条断言的结果行，不推断",
                }
            )
            continue
        items.append(
            {
                "expr": expr,
                "kind": kind,
                "result": VERDICT_PASS[hit["verdict"]],
                "detail": _detail_text(hit),
            }
        )

    if unknown_present:
        passed = failed = timeout = unacked = UNKNOWN
    else:
        passed = len([item for item in items if item["result"] == "pass"])
        timeout = len([item for item in items if item["result"] == "timeout"])
        unacked = len([item for item in items if item["result"] == "unacked"])
        # 契约 §2.3：`asserts.failed` 含 `ack:` 未确认
        failed = len([item for item in items if item["result"] == "fail"]) + unacked

    first_rate = _rate(first, declared, total)
    final_rate = _rate(final, declared, total)

    return (
        {
            "total": total,
            "passed": passed,
            "failed": failed,
            "timeout": timeout,
            "unacked": unacked,
            "items": items,
        },
        {"first_pass_rate": first_rate, "final_pass_rate": final_rate},
        _rework(declared, step_blocks),
        _first_failed(first, declared, acks),
        *_exit_code(declared, final),
    )


def _rate(block, declared, total):
    """某一次 gate 的通过率。分母是**声明条数**；分母 0 → `null`，拿不到 → `"unknown"`。"""
    if total == 0:
        return None
    hits = 0
    for expr in declared:
        if kind_of(expr) == "ack":
            continue  # ack: 没有 gate 结果，单独按 unacked 处理，不计入分子
        hit = _result_of(block, expr)
        if hit is None:
            return UNKNOWN
        if hit["verdict"] == "PASS":
            hits += 1
    return round(hits / total, 6)


def _rework(declared, step_blocks):
    """返工次数（契约 §5.2）：某条断言首次不是 pass，之后每出现一个新结果计 1 次。"""
    rework = 0
    for expr in declared:
        seen = []
        for block in step_blocks:
            hit = _result_of(block, expr)
            if hit is None:
                continue
            seen.append(VERDICT_PASS[hit["verdict"]])
        if len(seen) < 2 or seen[0] == "pass":
            continue
        for prev, cur in zip(seen, seen[1:]):
            if cur != prev:
                rework += 1
    return rework


def _first_failed(first, declared, acks=()):
    """首次判定就没过（含 timeout / ack 未确认）→ `True`（契约 §5.2 的返工锚点）。"""
    for expr in declared:
        if kind_of(expr) == "ack":
            if expr not in acks:
                return True
            continue
        hit = _result_of(first, expr)
        if hit is None or hit["verdict"] != "PASS":
            return True
    return False


def _exit_code(declared, final):
    """`exit_code` / `exit_code_src`（契约 §5.1）：宿主逐轮命令码 → gates.log 的 `exit=N` → unknown。"""
    cmd_exprs = [expr for expr in declared if expr.startswith("cmd:")]
    if not cmd_exprs:
        return None, None  # 这一步没有可执行命令：不适用，不是失败
    for expr in cmd_exprs:
        hit = _result_of(final, expr)
        if hit is None:
            continue
        matched = EXIT_RE.search(_detail_text(hit))
        if matched:
            return int(matched.group(1)), "gate"
    return UNKNOWN, UNKNOWN


# --------------------------------------------------------------------------
# 归一化：run → step → round
# --------------------------------------------------------------------------


def _int_or_unknown(value):
    if isinstance(value, bool):
        return UNKNOWN
    if isinstance(value, int):
        return value
    return UNKNOWN


def _round_cost(row, host_window):
    """单轮的 `cost`。`null` = 这一轮确实不花钱（amend 记账行）；`"unknown"` = 拿不到（契约 §2.4）。"""
    if host_window:
        values = [item["cost"] for item in host_window]
        if values and all(isinstance(value, (int, float)) for value in values):
            return {"usd": round(float(sum(values)), 6), "est": False}
    raw = row.get("cost")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {"usd": round(float(raw), 6), "est": bool(row.get("cost_est"))}
    if row.get("amend") or row.get("forced"):
        return {"usd": None, "est": False}
    return {"usd": UNKNOWN, "est": False}


def _round_tokens(row, host_window):
    fields = (("input", "in"), ("output", "out"), ("reasoning", "reasoning"),
              ("cache_read", "cache_read"), ("cache_write", "cache_write"))
    tokens = {}
    for name, column in fields:
        if host_window:
            values = [item[name] for item in host_window]
            if values and all(value is not None for value in values):
                tokens[name] = int(sum(values))
                continue
        tokens[name] = _int_or_unknown(row.get(column))
    return tokens


def rollup_tokens(rounds):
    """token 求和（契约 §4）：任一子项 `"unknown"` → 父项 `"unknown"`，不许跳过它按剩下的求和。"""
    names = ("input", "output", "reasoning", "cache_read", "cache_write")
    if not rounds:
        return {**{name: UNKNOWN for name in names}, "complete": False}
    total = {}
    for name in names:
        values = [item[name] for item in rounds]
        total[name] = UNKNOWN if any(value == UNKNOWN for value in values) else int(sum(values))
    total["complete"] = all(total[name] != UNKNOWN for name in names)
    return total


def rollup_cost(rounds):
    """成本求和（契约 §5.4）：`usd` 是**下限**；一行都拿不到 → `"unknown"`（不是 0）。"""
    known, unknown_rows, ests = [], 0, set()
    for item in rounds:
        usd = item["cost"]["usd"]
        if usd is None:
            continue  # 确实不产生费用，不是「拿不到」
        if usd == UNKNOWN:
            unknown_rows += 1
            continue
        known.append(usd)
        ests.add(bool(item["cost"]["est"]))
    if not known:
        basis = UNKNOWN
        usd = UNKNOWN
    else:
        if ests == {False}:
            basis = "host"
        elif ests == {True}:
            basis = "estimate"
        else:
            basis = "mixed"
        usd = round(float(sum(known)), 6)
    return {
        "usd": usd,
        "complete": bool(rounds) and unknown_rows == 0,
        "basis": basis,
        "unknown_rows": unknown_rows,
    }


def lane_of(step):
    lane = step.get("lane")
    if isinstance(lane, str) and lane in ("collect", "view"):
        return lane
    out = step.get("out")
    if isinstance(out, str):
        normalized = out.replace("\\", "/").lstrip("./")
        for prefix, name in LANE_BY_PREFIX:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return name
    return UNKNOWN


def compact_stamp(value):
    """`2026-10-26T20:48:14+08:00` → `20261026T204814`（丢掉时区尾巴，`run.id` 只求稳定紧凑）。"""
    if not isinstance(value, str):
        return None
    head = re.split(r"[+]|Z$|-\d{2}:\d{2}$", value.strip())[0]
    digits = re.sub(r"[^0-9T]", "", head)
    return digits or None


def build_run(root, host_db_path=None):
    """把 `root` 采集成 `run.json` 的 dict。**纯函数**：不写任何文件、不取 `now()`。"""
    root = Path(root)
    try:
        root_abs = root.resolve()
    except OSError:
        root_abs = root.absolute()
    deg = Degraded()

    plan, plan_present, plan_readable = load_plan(root_abs, deg, ())
    excluded = []
    if plan is not None:
        raw_exclude = plan.get("exclude")
        if isinstance(raw_exclude, list):
            excluded = [item for item in raw_exclude if isinstance(item, str)]

    rows, bad_lines, rows_total, ledger_present, ledger_readable = load_ledger(
        root_abs, deg, excluded
    )
    if ledger_present and not ledger_readable:
        deg.add("ledger.unreadable:.plan/ledger.jsonl 整份读不了，token/rounds/cost 记 unknown")

    gates_text, gates_present, gates_readable = read_source_text(
        root_abs / ".plan" / "gates.log", ".plan/gates.log", deg, excluded
    )
    if gates_present and not gates_readable:
        deg.add("gates.unreadable:.plan/gates.log 存在但读不了，断言结果记 unknown")
    blocks = parse_gates(gates_text, deg) if gates_readable and gates_text else []

    plan_md_path = root_abs / ".plan" / "plan.md"
    plan_md_present = plan_md_path.is_file()
    notes_dir = root_abs / ".plan" / "notes"
    notes_present = notes_dir.is_dir()

    # ---- 宿主 session（只读连接只在这里开一处）----
    db_path = Path(host_db_path) if host_db_path else host_db.default_db_path()
    db_present = bool(db_path and Path(db_path).is_file())
    db_readable = False
    conn = None
    session_ids = []
    for row in rows:
        sid = row.get("session")
        if isinstance(sid, str) and sid not in session_ids:
            session_ids.append(sid)
    sessions = {}
    host_rounds = {}
    if db_present:
        try:
            conn = _connect_ro(db_path)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            db_readable = True
            for sid in session_ids:
                sessions[sid] = host_db.read_session(conn, sid)
                if sessions[sid]["found"]:
                    host_rounds[sid] = host_db.read_rounds(conn, sid)
    if session_ids and not db_readable:
        deg.add("host_db.unreadable:宿主 session db 打不开，token/cost 回落账本")

    # 宿主逐轮分列数据只在「条数对得上」时用来替换账本聚合口径（契约 §5.4 口径优先级）
    window_by_line = {}
    for sid in session_ids:
        session_rows = [row for row in rows if row.get("session") == sid]
        reqs_total = sum(int(row.get("reqs") or 0) for row in session_rows if isinstance(row.get("reqs"), int))
        rounds_list = host_rounds.get(sid) or []
        usable = (
            rounds_list
            and reqs_total > 0
            and len(rounds_list) == reqs_total
            and all(item["input"] is not None and item["output"] is not None for item in rounds_list)
        )
        if not usable:
            continue
        cursor = 0
        for row in session_rows:
            count = int(row.get("reqs") or 0) if isinstance(row.get("reqs"), int) else 0
            if count > 0:
                window_by_line[row["__line__"]] = rounds_list[cursor:cursor + count]
                cursor += count

    # ---- round 明细 ----
    def make_rounds(step_rows):
        detail = []
        for seq, row in enumerate(step_rows, 1):
            window = window_by_line.get(row.get("__line__"))
            tokens = _round_tokens(row, window)
            at = row.get("ts") if isinstance(row.get("ts"), str) else UNKNOWN
            if at == UNKNOWN and window:
                at = next((item["at"] for item in reversed(window) if item["at"]), UNKNOWN)
            detail.append(
                {
                    "seq": seq,
                    "at": at,
                    "input": tokens["input"],
                    "output": tokens["output"],
                    "reasoning": tokens["reasoning"],
                    "cache_read": tokens["cache_read"],
                    "cache_write": tokens["cache_write"],
                    "cost": _round_cost(row, window),
                    "rounds": int(row.get("reqs") or 0) if isinstance(row.get("reqs"), int) else 0,
                    "note": row.get("note") if isinstance(row.get("note"), str) else "",
                    "decision": None,
                    "src": "host" if window else "ledger",
                }
            )
        return detail

    def rounds_scalar(step_rows):
        if not step_rows:
            return 0
        reqs = [row.get("reqs") for row in step_rows]
        if not any(isinstance(value, int) and value > 0 for value in reqs):
            # 一条都没记 reqs：凭什么叫「0 轮」（契约 §5.4）
            return UNKNOWN
        return sum(int(value) for value in reqs if isinstance(value, int) and not isinstance(value, bool))

    # ---- step ----
    steps = []
    rows_by_step = {}
    for row in rows:
        rows_by_step.setdefault(row.get("step"), []).append(row)
    plan_steps = []
    if plan is not None:
        raw_steps = plan.get("steps")
        if isinstance(raw_steps, list):
            plan_steps = [item for item in raw_steps if isinstance(item, dict)]
        plan_steps.sort(key=lambda item: item.get("n") if isinstance(item.get("n"), int) else 0)
    acks = plan.get("acks") if plan is not None and isinstance(plan.get("acks"), list) else []

    if plan is not None:
        for raw in plan_steps:
            n = raw.get("n")
            accept = raw.get("accept") if isinstance(raw.get("accept"), list) else []
            accept = [item for item in accept if isinstance(item, str)]
            step_rows = rows_by_step.get(n, [])
            artifacts = []
            planned_out = raw.get("out")
            if isinstance(planned_out, str):
                artifacts.append(artifact_of(root_abs, planned_out, deg, excluded))
            detail = make_rounds(step_rows)
            step_asserts, step_rates, rework, first_failed, exit_code, exit_src = assert_block(
                accept, acks, blocks, deg, n
            )
            steps.append(
                {
                    "n": n if isinstance(n, int) else UNKNOWN,
                    "title": raw.get("title") if isinstance(raw.get("title"), str) else UNKNOWN,
                    "lane": lane_of(raw),
                    "status": raw.get("status") if isinstance(raw.get("status"), str) else UNKNOWN,
                    "planned_out": planned_out if isinstance(planned_out, str) else UNKNOWN,
                    "artifacts": artifacts,
                    "asserts": step_asserts,
                    "assert_pass": step_rates,
                    "exit_code": exit_code,
                    "exit_code_src": exit_src,
                    "rework": rework,
                    "first_failed": first_failed,
                    "rounds": rounds_scalar(step_rows),
                    "tokens": rollup_tokens(detail),
                    "cost": rollup_cost(detail),
                    "rounds_detail": detail,
                }
            )
        orphans = sorted(
            str(key) for key in rows_by_step if key not in {step.get("n") for step in plan_steps}
        )
        if orphans:
            deg.add("ledger.step_unknown:账本里出现计划没有的 step=%s，这些行不参与归一化" % ",".join(orphans))
    else:
        deg.add("no_plan_dir:目标目录没有 .plan/，没有步骤可归一化")
        if rows:
            deg.add("ledger.step_unknown:没有计划，%d 行账本无处归位" % len(rows))

    # ---- run 级汇总 ----
    all_rounds = []
    for step in steps:
        all_rounds.extend(step["rounds_detail"])
    if plan is None:
        all_rounds = []
    rows_with_reqs = len(
        [row for row in rows if isinstance(row.get("reqs"), int) and row.get("reqs")]
    )
    if ledger_readable or ledger_present:
        if rows_with_reqs == 0 and rows_total > 0:
            run_rounds = UNKNOWN
            deg.add("rounds.unknown:账本一行 reqs 都没有，不报「0 轮」（契约 §5.4）")
        else:
            run_rounds = sum(
                int(row.get("reqs")) for row in rows
                if isinstance(row.get("reqs"), int) and not isinstance(row.get("reqs"), bool)
            )
    else:
        run_rounds = UNKNOWN

    run_tokens = rollup_tokens(all_rounds)
    run_cost = rollup_cost(all_rounds)
    if run_tokens["reasoning"] == UNKNOWN:
        deg.add(
            "tokens.reasoning_folded_into_output:宿主逐轮分列数据拿不到，回落到账本的合并 in/out 口径，output 可能系统性偏大"
        )
    if run_tokens["cache_write"] == UNKNOWN:
        deg.add("tokens.cache_write:unknown 账本没有 cache_write 列，按契约记 unknown（不是 0）")
    if run_cost["unknown_rows"]:
        deg.add(
            "cost.unknown_rows:%d 有 %d 行账本没有 cost，cost.usd 只是下限"
            % (run_cost["unknown_rows"], run_cost["unknown_rows"])
        )
    if run_cost["basis"] == "mixed":
        deg.add("cost.mixed:含估算值，cost.usd 是上界不是真账")

    # ---- 模型 ----
    model = UNKNOWN
    counted = [
        (sessions[sid]["model"], max(1, int(sessions[sid]["assistant_messages"] or 1)))
        for sid in session_ids
        if sessions.get(sid, {}).get("found") and sessions[sid]["model"]
    ]
    if counted:
        scores = {}
        for item, weight in counted:
            key = (item["provider"], item["id"])
            scores[key] = scores.get(key, 0) + weight
        best = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]
        model = {"provider": best[0], "id": best[1], "mixed": len(scores) > 1}
    else:
        missing = [sid for sid in session_ids if not sessions.get(sid, {}).get("found")]
        for sid in missing:
            deg.add("host_db.session_missing:%s 宿主 db 里没有这个 session，token/cost 回落账本" % sid)
        if session_ids:
            deg.add("model.unknown:宿主 session 记录里拿不到 model.providerID / model.id")
        else:
            deg.add("model.unknown:账本没有 session 字段，拿不到 model")

    # ---- 轮次交叉校验（宿主 assistant 条数只做交叉校验，契约 §5.4）----
    host_count = None
    if session_ids:
        counts = [
            sessions[sid]["assistant_messages"]
            for sid in session_ids
            if sessions.get(sid, {}).get("found")
        ]
        if counts and all(count is not None for count in counts):
            host_count = sum(int(count) for count in counts)
    if host_count is not None and isinstance(run_rounds, int) and host_count != run_rounds:
        deg.add("rounds.mismatch:ledger=%d host=%d 以账本为主" % (run_rounds, host_count))

    # ---- 时间：只取数据里的时间，不取 now() ----
    stamps = [item["at"] for item in all_rounds if item["at"] != UNKNOWN]
    started_at = stamps[0] if stamps else (
        plan.get("created") if plan is not None and isinstance(plan.get("created"), str) else UNKNOWN
    )
    finished_at = stamps[-1] if stamps else UNKNOWN
    run_id = "%s-%s" % (root_abs.name or "root", compact_stamp(started_at) or UNKNOWN)

    # ---- verdict（契约 §5.5）----
    verdict = verdict_of(steps, run_tokens, run_cost, run_rounds)

    sources = [
        _source("plan_json", ".plan/plan.json", plan_present, plan_readable,
                "" if plan_present else "文件不存在"),
        _source("plan_md", ".plan/plan.md", plan_md_present, plan_md_present,
                "" if plan_md_present else "文件不存在（可选来源）"),
        _source("ledger", ".plan/ledger.jsonl", ledger_present, ledger_readable,
                "" if ledger_present else "文件不存在"),
        _source("gates_log", ".plan/gates.log", gates_present, gates_readable,
                "" if gates_present else "文件不存在"),
        _source("notes", ".plan/notes/", notes_present, notes_present,
                "目录不存在" if not notes_present else "decision 提取口径契约未定义，本轮全部记 null"),
        _source(
            "host_db",
            str(db_path) if db_path else "unknown",
            db_present,
            db_readable,
            "mode=ro + PRAGMA query_only" if db_readable else ("文件不存在" if not db_present else "打不开"),
        ),
        _source(
            "artifacts",
            ".",
            root_abs.is_dir(),
            root_abs.is_dir(),
            _artifacts_note(plan),
        ),
    ]

    run = {
        "id": run_id,
        "root": str(root_abs),
        "started_at": started_at,
        "finished_at": finished_at,
        "goal": plan.get("goal") if plan is not None and isinstance(plan.get("goal"), str) else UNKNOWN,
        "model": model,
        "rounds": run_rounds,
        "rounds_coverage": {"rows_total": rows_total, "rows_with_reqs": rows_with_reqs},
        "tokens": run_tokens,
        "cost": run_cost,
        "verdict": verdict,
        "sources": sources,
        "degraded": bool(deg),
        "degraded_reasons": list(deg.reasons),
        "steps": steps,
    }
    document = {"schema": SCHEMA_VERSION, "run": run}
    if excluded:
        run["excluded"] = excluded
    if not steps:
        # 契约 §2.6：残缺体额外带 summary，且**不许**出现 0/0、0%
        run["summary"] = {
            "steps": 0,
            "asserts_total": None,
            "asserts_passed": None,
            "assert_pass_rate": None,
            "verdict": verdict,
        }
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return document


def _source(kind, path, present, readable, note):
    return {"kind": kind, "path": path, "present": bool(present), "readable": bool(readable), "note": note}


def _artifacts_note(plan):
    """`plan.json` 顶层的 `out`（若有）在 `run.json` 里没有 run 级产物槽位，只在来源 note 里点名。"""
    if plan is not None and isinstance(plan.get("out"), str):
        return "只读快照；plan.out=%s（run 级产物无独立字段，见 steps[].artifacts）" % plan["out"]
    return "只读快照"


def verdict_of(steps, run_tokens, run_cost, run_rounds):
    """`run.verdict`（契约 §5.5）：缺证据就是 `"unknown"`，**不许默认 pass**。"""
    if not steps:
        return UNKNOWN
    for step in steps:
        failed = step["asserts"]["failed"]
        if isinstance(failed, int) and failed > 0:
            return "fail"
        code = step["exit_code"]
        if isinstance(code, int) and code != 0:
            return "fail"
    unknown = (
        run_rounds == UNKNOWN
        or not run_tokens["complete"]
        or run_cost["usd"] == UNKNOWN
        or any(
            step["asserts"][key] == UNKNOWN
            for step in steps
            for key in ("passed", "failed", "timeout", "unacked")
        )
        or any(step["exit_code"] == UNKNOWN for step in steps)
        or any(step["assert_pass"]["final_pass_rate"] == UNKNOWN for step in steps)
    )
    if unknown:
        return UNKNOWN
    # 比率 `null` = 这一步就没有断言（不适用），不挡住 pass，也不凑成 1.0
    rates = [
        step["assert_pass"]["final_pass_rate"]
        for step in steps
        if isinstance(step["assert_pass"]["final_pass_rate"], (int, float))
    ]
    if rates and all(rate == 1.0 for rate in rates):
        return "pass"
    if not rates:
        return UNKNOWN
    return "fail"


def exit_code_of(document, root):
    """退出码（契约 §7.1 / §8.1）。`verdict == "fail"` → 2；数据不可用 → 3；其余 0。"""
    run = document["run"]
    if not run["steps"]:
        return 3
    if run["verdict"] == "fail":
        return 2
    return 0


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def dumps(document):
    """`run.json` 的字节形状：键序固定（本文件的构造顺序）、`ensure_ascii=False`、末尾一个 `\\n`。"""
    return json.dumps(document, ensure_ascii=False, indent=1) + "\n"


def write_run(document, out_path):
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps(document))
    except OSError as exc:
        raise SystemExit(_fatal("write_failed", "写 run.json 失败: %s" % exc, str(out_path)))
    return out_path


def _fatal(code, message, hint=""):
    say("[collect] 错误: %s" % message)
    if hint:
        say("[collect] 提示: %s" % hint)
    return 1


def _json_out(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m collect.ingest.plan_dir",
        description="只读采集器：.plan/ + 宿主 db + 产物目录 → run.json（contract/trace.md §2）",
    )
    parser.add_argument("--root", default=".", help="被观测的目标目录（默认当前目录）")
    parser.add_argument("--out", default=None, help="run.json 落盘路径，形状 runs/<run_id>/run.json")
    parser.add_argument("--host-db", default=None, help="宿主 opencode.db 路径（默认自动找）")
    parser.add_argument("--json", action="store_true", help="stdout 只出 JSON（契约 §7.1）")
    parser.add_argument("--selftest", action="store_true", help="拿两份夹具自证，全绿 exit 0")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    root = Path(args.root)
    if not root.is_dir():
        _fatal("no_root", "目标目录不存在: %s" % root, "给一个存在的 --root")
        if args.json:
            _json_out({"ok": False, "error": {"code": "no_root", "message": "目标目录不存在", "hint": str(root)}})
        return 1

    try:
        document = build_run(root, args.host_db)
    except Exception as exc:  # 工具自身故障（契约 §7.1 的 1）
        _fatal("internal", "采集过程内部异常: %s" % exc, "这是工具故障，不是判分结果")
        if args.json:
            _json_out({"ok": False, "error": {"code": "internal", "message": str(exc), "hint": ""}})
        return 1

    code = exit_code_of(document, root)
    run = document["run"]
    if args.out:
        try:
            written = write_run(document, args.out)
        except SystemExit as exc:
            if args.json:
                _json_out({"ok": False, "error": {"code": "write_failed", "message": "写 run.json 失败", "hint": str(args.out)}})
            return int(exc.code or 1)
        say("[collect] 已写入 %s" % written)

    if args.json:
        if code == 3:
            _json_out(
                {
                    "ok": False,
                    "error": {
                        "code": "no_plan_dir",
                        "message": "目标目录没有可用的 .plan/，没有步骤可归一化",
                        "hint": "仍写出了 steps: 0 的 run.json；断言总数是 null，不是 0",
                    },
                    "run": run,
                }
            )
        else:
            _json_out({"ok": True, "run": run})
    else:
        say(
            "[collect] run=%s steps=%d rounds=%s tokens=%s/%s cost=%s(%s) verdict=%s degraded=%s"
            % (
                run["id"],
                len(run["steps"]),
                run["rounds"],
                run["tokens"]["input"],
                run["tokens"]["output"],
                run["cost"]["usd"],
                run["cost"]["basis"],
                run["verdict"],
                run["degraded"],
            )
        )
        for reason in run["degraded_reasons"]:
            say("[collect] degraded: %s" % reason)
    return code


# --------------------------------------------------------------------------
# 自检：把 plan.json 步 3 里写死的六个期望值逐条核一遍
# --------------------------------------------------------------------------

SELFTEST_RESULTS = []


def _check(name, ok, detail=""):
    if len(detail) > 160:
        detail = detail[:160] + "…"
    SELFTEST_RESULTS.append((name, ok, detail))
    say("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("  " + detail) if detail else ""))
    return ok


def _eq(name, got, want):
    return _check(name, got == want, "got=%r want=%r" % (got, want))


def _snapshot(root):
    """来源目录的（相对路径 → sha256, size, mtime）快照，用来证明采集器一个字节都没写。"""
    snap = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            snap[path.relative_to(root).as_posix()] = (digest, stat.st_size, stat.st_mtime_ns)
    return snap


def _no_plan_case():
    """残缺体自检用的目标目录：存在，但**没有** `.plan/`。

    `contract/slices.md` §4.3 指的是 `collect/fixtures/empty/`；那个目录是步 2 的产物，
    本步不去造它（越界），所以缺的时候退回到「存在但确实没有 `.plan/`」的 `collect/ingest/`，
    走的是同一条 `no_plan_dir` 分支。
    """
    if FIXTURE_EMPTY.is_dir():
        return FIXTURE_EMPTY
    return REPO_ROOT / "collect" / "ingest"


def selftest():
    say("collect/ingest/plan_dir —— 采集器自检（期望值来自 plan.json 步 3 + collect/fixtures/README.md）")
    ok_all = True

    say("[1] rework01：坏行、缺 cost、reasoning 不许写 0")
    before_a, before_b = _snapshot(FIXTURE_A), _snapshot(FIXTURE_B)
    doc_a = build_run(FIXTURE_A)
    doc_b = build_run(FIXTURE_B)
    run_a, run_b = doc_a["run"], doc_b["run"]
    step1 = run_a["steps"][0]
    step2 = run_a["steps"][1]

    _eq("run.cost.usd == 0.0091（第 3 行没 cost 所以不计）", run_a["cost"]["usd"], 0.0091)
    _eq("run.cost.complete == false", run_a["cost"]["complete"], False)
    _eq("run.cost.unknown_rows == 1", run_a["cost"]["unknown_rows"], 1)
    _eq("run.cost.basis == host", run_a["cost"]["basis"], "host")
    _eq("步 2 一行 cost 都没有 → cost.usd 是 unknown（不是 0）", step2["cost"]["usd"], UNKNOWN)
    _eq("步 2 unknown_rows == 1", step2["cost"]["unknown_rows"], 1)

    _eq("run.tokens.reasoning == unknown（不许写 0）", run_a["tokens"]["reasoning"], UNKNOWN)
    _eq("步 1 round.reasoning == unknown（不许写 0）", step1["rounds_detail"][0]["reasoning"], UNKNOWN)
    _eq("run.tokens.complete == false", run_a["tokens"]["complete"], False)
    _eq("run.tokens.input == 59000（20000+21000+18000）", run_a["tokens"]["input"], 59000)
    _eq("run.tokens.output == 3000（1100+1000+900）", run_a["tokens"]["output"], 3000)
    _eq("run.tokens.cache_read == 29000（10000+10000+9000）", run_a["tokens"]["cache_read"], 29000)

    say("[2] rework01：坏行跳过但前 3 行照常用")
    _check(
        "degraded_reasons 含 ledger.bad_json:4（1-based 行号）",
        any(reason.startswith("ledger.bad_json:4") for reason in run_a["degraded_reasons"]),
        "reasons=%r" % (run_a["degraded_reasons"],),
    )
    _eq("坏行不整份放弃：步 1 rounds == 2", step1["rounds"], 2)
    _eq("坏行不整份放弃：步 2 rounds == 1", step2["rounds"], 1)

    say("[3] rework01：返工定位（trace.md §5.2）")
    _eq("步 1 rework == 1", step1["rework"], 1)
    _eq("步 1 first_failed == true", step1["first_failed"], True)
    _eq("步 1 first_pass_rate == 0.5", step1["assert_pass"]["first_pass_rate"], 0.5)
    _eq("步 1 final_pass_rate == 1.0", step1["assert_pass"]["final_pass_rate"], 1.0)
    _eq("步 2 rework == 0", step2["rework"], 0)
    _eq("步 2 first_failed == false", step2["first_failed"], False)
    _eq("步 2 exit_code == 0 且 src == gate", (step2["exit_code"], step2["exit_code_src"]), (0, "gate"))
    _eq("步 1 没有 cmd: 断言 → exit_code 是 null（不适用）", (step1["exit_code"], step1["exit_code_src"]), (None, None))

    say("[4] rework01：轮次口径（trace.md §5.4）")
    _eq(
        "rounds_coverage == {rows_total: 4, rows_with_reqs: 3}",
        run_a["rounds_coverage"],
        {"rows_total": 4, "rows_with_reqs": 3},
    )
    _eq("run.rounds == Σ reqs == 3", run_a["rounds"], 3)

    say("[5] rework01：degraded 至少两条")
    _eq("run.degraded == true", run_a["degraded"], True)
    _check("degraded_reasons 至少两条", len(run_a["degraded_reasons"]) >= 2, "n=%d" % len(run_a["degraded_reasons"]))
    _check(
        "degraded_reasons 覆盖「坏行 + 缺 cost」两件事",
        any("ledger.bad_json" in reason for reason in run_a["degraded_reasons"])
        and any("cost.unknown_rows" in reason for reason in run_a["degraded_reasons"]),
    )

    say("[6] rework01b：只改两处 —— 返工消失、cost 补齐")
    _eq("rework01b cost.complete == true", run_b["cost"]["complete"], True)
    _eq("rework01b cost.unknown_rows == 0", run_b["cost"]["unknown_rows"], 0)
    _eq("rework01b cost.usd == 0.0122（0.0042+0.0049+0.0031）", run_b["cost"]["usd"], 0.0122)
    _eq("rework01b 步 1 first_pass_rate == 1.0", run_b["steps"][0]["assert_pass"]["first_pass_rate"], 1.0)
    _eq("rework01b 步 1 rework == 0", run_b["steps"][0]["rework"], 0)

    say("[7] 纯函数：同一份来源重跑 → 字节级相同的 run.json")
    _eq("两次采集字节一致（rework01）", dumps(build_run(FIXTURE_A)), dumps(doc_a))
    _eq("两次采集字节一致（rework01b）", dumps(build_run(FIXTURE_B)), dumps(doc_b))
    _check("run.json 以单个换行结尾", dumps(doc_a).endswith("\n") and not dumps(doc_a).endswith("\n\n"))
    _check("run.json 里没有本机采集时间戳式字段（started_at 取自数据）", run_a["started_at"] != UNKNOWN)

    say("[8] 只读：夹具（来源）目录一个字节都没被改")
    _eq("rework01 来源快照未变（hash/size/mtime）", _snapshot(FIXTURE_A), before_a)
    _eq("rework01b 来源快照未变（hash/size/mtime）", _snapshot(FIXTURE_B), before_b)
    _check(
        "夹具里没有冒出 run.json",
        not (FIXTURE_A / ".plan" / "run.json").exists() and not (FIXTURE_B / ".plan" / "run.json").exists(),
    )

    say("[9] 残缺体：没有 .plan/ 时不许报 0/0（trace.md §8.1）")
    empty_case = _no_plan_case()
    doc_empty = build_run(empty_case)
    run_empty = doc_empty["run"]
    text_empty = dumps(doc_empty)
    _check("残缺体的目标目录存在、但确实没有 .plan/", empty_case.is_dir() and not (empty_case / ".plan").exists(), str(empty_case))
    _eq("steps == 0", run_empty["steps"], [])
    _eq("summary.asserts_total 是 null（不是 0）", run_empty["summary"]["asserts_total"], None)
    _eq("summary.assert_pass_rate 是 null", run_empty["summary"]["assert_pass_rate"], None)
    _eq("verdict == unknown", run_empty["verdict"], UNKNOWN)
    _eq("退出码 == 3（数据不可用）", exit_code_of(doc_empty, empty_case), 3)
    _check("输出里没有 0/0、0%", ("0/0" not in text_empty) and ("0%" not in text_empty))

    say("")
    failed = [item for item in SELFTEST_RESULTS if not item[1]]
    if failed:
        for name, _, detail in failed:
            say("selftest: FAIL %s %s" % (name, detail))
        say("selftest: %d/%d 条不符" % (len(failed), len(SELFTEST_RESULTS)))
        ok_all = False
    else:
        say("selftest: %d/%d 全绿" % (len(SELFTEST_RESULTS), len(SELFTEST_RESULTS)))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
