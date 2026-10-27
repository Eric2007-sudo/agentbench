# collect/score —— 路 A 的判分器（四类机械判分）

口径来源：`contract/trace.md` §5（唯一事实来源，本目录不新增约定）。本文件只写「哪段代码认哪条口径」。

## 四类，各出一份「数」+「口径来源」

| 段 | 模块 | 判什么 | 允许的结论 | 契约 |
|---|---|---|---|---|
| `exitcode` | `exitcode.py` | 这一步声明的命令退出码 | `pass` / `fail` / `unjudged` / `not_applicable` | §5.1 |
| `asserts` | `asserts.py` | 断言通过率（分母 = plan 的 `accept` 条数，声明口径） | 两个比率 + 计数，**不做阈值判定** | §5.2 |
| `diff` | `diff.py` | 产物 diff（按路径对齐，**只看 sha256，不看 mtime**） | `added` / `removed` / `modified` / `unchanged` / `unknown` | §5.3 |
| `cost` | `cost.py` | 成本（**下限**口径）与轮次（`Σ reqs`） | 数 + `complete` / `unknown_rows` / `is_lower_bound` | §5.4 |

**没有加权总分**：四类不相加、不排序、不给「综合分」；`score.json` 里不允许出现
`score` / `weight` / `grade` / `threshold` 一类字段（`--verify` 会逐键扫一遍）。

## 三条容易写错、这里钉死的地方

1. **`null` 与 `"unknown"` 不混用**（§4）：没声明 `cmd:` → `exit_code: null`（不适用）；
   声明了却拿不到码 → `"unknown"` 且计入 `unjudged`（不算过也不算破，也不剔出分母）。
2. **基准缺失 → `diff` 整段 `"unknown"`**：不许报「0 新增 0 删除」（那会被读成「没变化」）。
3. **没有价目就不许算钱**：本包读不到任何价目表，`pricing.table` 恒为 `"none"`、
   `estimated_rows` 只统计 `run.json` 里已有的 `cost.est: true`，自己一次乘法都不做。

## 出口

```
python -m collect.score --self-test
python -m collect.score --verify runs/fx-rework01/score.json
python -m collect.score --run runs/fx-rework01/run.json \
                        --baseline runs/fx-rework01/run.json \
                        --out runs/fx-rework01/score.json
```

- `--self-test`：造 14 个退化场景（`rows_with_reqs=0`、0 条断言、基准缺失、两侧成本 unknown、
  声明 cmd 却没码、容差、ack、超时、无价目、文本 diff、确定性、假缺失值、verdict 三分支），
  一条一条打 `PASS`/`FAIL`，最后打总条数；全对 exit 0。
- `--verify`：拿 `fx-rework01` 的钉死期望（`.plan/plan.json` 步 4 的 note /
  `collect/fixtures/README.md`）逐条核一份 `score.json`，52 条，全对 exit 0，不符项打 stderr。
- `--run`：读 `run.json`（路 A 的交接物）→ 落 `runs/<run_id>/score.json`。退出码：
  `0` 判完（`pass`/`unknown`）、`2` 判完且 `fail`、`3` 数据不可用、`1` 工具故障。
- `--run-declared`：默认**关**。开了才走 §5.1 的第 3 优先级（真跑 plan 里声明的 `cmd:`
  验收命令取退出码，在临时目录里跑）。默认关是为了让判分保持纯函数（§3.2 第 4 条）。

## 只读与产物

- 只读 `run.json`（+ 可选的基准 `run.json`）；不读 `.plan/`、不开 sqlite、不碰夹具。
- 唯一写盘动作是 `--out` 指定的 `runs/<run_id>/score.json`；不写时间戳、不写进程 id，
  键序固定、`\n` 结尾 → 同一份 `run.json` 判两次字节级相同（§7.5，`--self-test` 有一条专门验它）。
- `sys.dont_write_bytecode = True`：不在来源目录里留 `__pycache__`（§3.2 第 3 条）。
