"""`collect/measure` 包：性能五标量 + 模态登记（`contract/dimensions.md` §1 / §4 / §5）。

只读三处来源（run.json、同名目录 `.plan/ledger.jsonl`、宿主 db），不估、不猜：

- `ttft_s` / `tool_calls` 恒为 `"unknown"`（§1.3）；
- 缺字段一律 `"unknown"`，不写 `0`、不写 `null` 冒充（trace.md §4）；
- 产出里不许出现 `score`/`weight`/`grade`/`threshold` 字段（§2.3 / §7）。

入口：`python -m collect.measure`（见 `__main__.py`）。
"""

import sys

sys.dont_write_bytecode = True

__all__ = ["base", "perf", "modalities"]
