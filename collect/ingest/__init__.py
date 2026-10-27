"""collect/ingest —— 路 A 的只读采集层（`contract/slices.md` §2）。

子模块：
- `plan_dir.py`  采集器主程序：读 `.plan/` + 产物目录，归一化成 `run`→`step`→`round`，落 `runs/<id>/run.json`
- `host_db.py`   宿主 session 数据（`opencode.db`）的**解析层**：只拿连接、只发 SELECT

只读硬约束（`contract/trace.md` §3.2）：文件只 `open(..., "r")`；sqlite 只 `?mode=ro`
+ `PRAGMA query_only`；连接由 `plan_dir.py` 一处开出，本包任何模块都不自己开可写连接。
"""

import sys

sys.dont_write_bytecode = True
