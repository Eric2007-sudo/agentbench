"""view/ · 路 B：只吃 run.json 的回归对比 / 导出 / 看板。

硬约束（contract/slices.md §2）：
- 只读 `runs/**/run.json` 与 `contract/**`；**一次都不读** `.plan/**`、宿主 db、目标产物目录、`collect/**`；
- 允许 import：自己的子包 + 标准库（不许 import `collect.*`）。
"""

__all__ = []
