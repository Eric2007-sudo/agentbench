"""宿主 session 数据（`opencode.db`）的只读访问 —— **薄适配层**，口径全部来自 `collect/ingest/host_db.py`。

为什么要有这一层：`measure` 只许写自己的目录（简报「只写 `collect/measure/`」），
所以不在 `collect/ingest/host_db.py` 里加任何函数；这里**只调用**它已有的 `default_db_path` /
`read_rounds`，连接由本模块按 trace.md §3.2 第 2 条用 `?mode=ro` + `PRAGMA query_only` 开好后注入。

拿不到就是拿不到：不建库、不建表、不猜路径，失败一律返回空列表 + reason。
"""

import sys

sys.dont_write_bytecode = True

try:  # 正常路径：包内相对导入
    from ..ingest import host_db
except ImportError:  # pragma: no cover - 直接当脚本跑的极端情形
    from collect.ingest import host_db  # type: ignore

default_db_path = host_db.default_db_path
to_iso = host_db.to_iso


def read_session_rounds(session_id, db_path=None):
    """读一个 session 的全部 assistant 轮次（`at` 已归一成 ISO8601）。

    返回 `(rounds, reason)`；`reason` 为 `None` 表示读到了。只发 `SELECT`。
    """
    path = db_path or default_db_path()
    if not path:
        return [], "host_db.missing"
    if not isinstance(session_id, str) or not session_id:
        return [], "host_db.no_session"
    import sqlite3

    try:
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [], "host_db.unreadable:%s" % exc
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        try:
            rounds = host_db.read_rounds(conn, session_id)
        except sqlite3.Error as exc:
            return [], "host_db.unreadable:%s" % exc
    finally:
        conn.close()
    if not rounds:
        return [], "host_db.session_missing:%s" % session_id
    return rounds, None
