"""宿主 session 数据（`opencode.db`）的只读解析层 —— 只给 `collect/ingest/plan_dir.py` 用。

口径来源：`contract/trace.md` §3.1（表 `session_v2`（会话累计）/ `session_message`、`message`
（逐轮 `data` JSON）→ `data.tokens.{input,output,reasoning}` / `data.tokens.cache.{read,write}` /
`data.cost` / `data.model.{providerID,id}` / `time_created` / `session_id`）。

两条纪律：

1. **本模块不自己开连接。** 连接由 `plan_dir.py` 用 `?mode=ro` + `PRAGMA query_only` 开好后注入，
   这样「只读」这条硬约束在源码里只有一处、不会漏（`contract/trace.md` §3.2 第 2 条）。
2. **拿不到就是 `None`，绝不编。** 表缺、列缺、字段缺、解析失败一律返回 `None`，
   由上层决定落 `"unknown"`（`contract/trace.md` §4）。

本模块只发 `SELECT`；不建索引、不 `VACUUM`、不 `ATTACH`、不写任何文件。
"""

import sys

sys.dont_write_bytecode = True

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SESSION_TABLES = ("session_v2", "session")
MESSAGE_TABLES = ("session_message", "message")

# `session_v2` 里的会话累计列（契约 §3.1 点名的那几个）
TOKEN_COLUMNS = (
    ("input", "tokens_input"),
    ("output", "tokens_output"),
    ("reasoning", "tokens_reasoning"),
    ("cache_read", "tokens_cache_read"),
    ("cache_write", "tokens_cache_write"),
)


def default_db_path():
    """宿主 `opencode.db` 的默认位置（`<home>/.local/share/opencode/opencode.db`）。

    路径不存在就返回 `None` —— 不猜、不建（契约 §3.1 / §8.2「宿主 db 不存在」）。
    """
    candidates = []
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / ".local" / "share" / "opencode" / "opencode.db")
    for env in ("LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "opencode" / "opencode.db")
            candidates.append(Path(base) / ".local" / "share" / "opencode" / "opencode.db")
    for path in candidates:
        if path.is_file():
            return path
    return None


def to_iso(ms):
    """毫秒时间戳 → 带时区的 ISO8601（`contract/trace.md` §2.2 要求带时区）。"""
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def table_names(conn):
    try:
        return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    except sqlite3.Error:
        return set()


def _pick(table_set, names):
    for name in names:
        if name in table_set:
            return name
    return None


def _as_dict(row):
    if row is None:
        return None
    try:
        return {key: row[key] for key in row.keys()}
    except (AttributeError, TypeError):
        return None


def _json_obj(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _model_of(raw):
    """`{"id":...,"providerID":...}` → `{"provider":..., "id":...}`；拿不到就是 `None`。"""
    data = _json_obj(raw)
    if data is None:
        return None
    ident = data.get("id") or data.get("modelID")
    provider = data.get("providerID") or data.get("provider")
    if not isinstance(ident, str) or not isinstance(provider, str):
        return None
    return {"provider": provider, "id": ident}


def read_session(conn, session_id):
    """读一个 session 的累计信息。

    返回 `{"found": bool, "model": {...}|None, "cost": float|None,
           "tokens": {input,output,reasoning,cache_read,cache_write}(int|None),
           "assistant_messages": int|None, "first_at": str|None, "last_at": str|None}`

    `found=False` 表示宿主 db 里根本没有这个 session（夹具的假 session 就是这种），
    上层按 `contract/trace.md` §3.1 回落到账本。
    """
    empty = {
        "found": False,
        "model": None,
        "cost": None,
        "tokens": {name: None for name, _ in TOKEN_COLUMNS},
        "assistant_messages": None,
        "first_at": None,
        "last_at": None,
    }
    tables = table_names(conn)
    table = _pick(tables, SESSION_TABLES)
    if table is None:
        return empty
    try:
        row = conn.execute("select * from %s where id = ?" % table, (session_id,)).fetchone()
    except sqlite3.Error:
        return empty
    data = _as_dict(row)
    if data is None:
        return empty

    out = dict(empty)
    out["found"] = True
    out["model"] = _model_of(data.get("model"))
    cost = data.get("cost")
    out["cost"] = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
    out["tokens"] = {name: _int_or_none(data.get(col)) for name, col in TOKEN_COLUMNS}
    msg_table = _pick(tables, MESSAGE_TABLES)
    if msg_table is not None:
        try:
            counted = conn.execute(
                "select count(*) from %s where session_id = ? and type = 'assistant'" % msg_table,
                (session_id,),
            ).fetchone()
            out["assistant_messages"] = int(counted[0]) if counted else None
        except sqlite3.Error:
            out["assistant_messages"] = None
    out["first_at"] = to_iso(data.get("time_created"))
    out["last_at"] = to_iso(data.get("time_updated"))
    return out


def read_rounds(conn, session_id):
    """读一个 session 的逐轮（assistant 消息）明细；拿不到的字段一律 `None`。

    返回按 `time_created` 升序、再按 `id` 升序的列表，每条：
    `{"at", "input", "output", "reasoning", "cache_read", "cache_write", "cost", "model"}`

    宿主库不带逐轮 token（`data` 里没有 `tokens`）时，各 token 字段就是 `None` ——
    上层据此判断「宿主分列口径拿不到」，按 `contract/trace.md` §5.4 回落账本并记 degraded。
    """
    tables = table_names(conn)
    table = _pick(tables, MESSAGE_TABLES)
    if table is None:
        return []
    try:
        rows = list(
            conn.execute(
                "select * from %s where session_id = ? and type = 'assistant'" % table,
                (session_id,),
            )
        )
    except sqlite3.Error:
        return []

    rounds = []
    for row in rows:
        data = _as_dict(row)
        if data is None:
            continue
        payload = _json_obj(data.get("data")) or {}
        tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        cost = payload.get("cost")
        created = payload.get("time", {}).get("created") if isinstance(payload.get("time"), dict) else None
        rounds.append(
            {
                "at": to_iso(created if created is not None else data.get("time_created")),
                "input": _int_or_none(tokens.get("input")),
                "output": _int_or_none(tokens.get("output")),
                "reasoning": _int_or_none(tokens.get("reasoning")),
                "cache_read": _int_or_none(cache.get("read")),
                "cache_write": _int_or_none(cache.get("write")),
                "cost": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                "model": _model_of(payload.get("model")),
                "_sort": (data.get("time_created") or 0, str(data.get("id") or "")),
            }
        )
    rounds.sort(key=lambda item: item["_sort"])
    for item in rounds:
        item.pop("_sort", None)
    return rounds
