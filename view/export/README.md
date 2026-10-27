# `view/export/` · 导出三件套（B 路）

> 口径来源只有一个：`contract/trace.md` §7.1（出口）、§7.2（json）、§7.3（csv）、§7.4（md）、§7.5（确定性）。
> 边界：只吃 `run.json`；**不读** `.plan/**`、宿主 db、产物目录，**不 import** `collect.*`。
> 写集合 = `view/**` + `out/**`。

## 文件分工

| 文件 | 干什么 |
|---|---|
| `__init__.py` | 底座：出口纪律（stdout 只放产物、人话走 stderr）、读 `run.json`、`null`/`unknown` 三态归一、浮点定点 |
| `jsonio.py` | `--format json`：`run.json` 原文等价输出（不裁剪、不重排语义，§7.2）；四维只由 CLI 在**顶层**追加一段 `dimensions` |
| `csvio.py` | `--format csv`：一行一个 step，表头顺序照抄 §7.3，`utf-8-sig` + RFC4180 |
| `mdio.py` | `--format md`：Run 摘要 / Step 表 / 失败断言表，三张表顺序固定（§7.4）；四维四节追加在后 |
| `__main__.py` | CLI（`python -m view.export`）+ `--self-test` |

## 用法

```
python -m view.export --run runs/fx-rework01/run.json --format json --out out/fx.json
python -m view.export --run runs/fx-rework01/run.json --format csv  --out out/fx.csv
python -m view.export --run runs/fx-rework01/run.json --format md   --out out/fx.md
python -m view.export --self-test
```

`--out` 落盘与 stdout **字节一致**（两者写同一份 bytes，§7.2）；不加 `--out` 就只写 stdout。

## 四维接入（性能 / 判据覆盖 / 交互性 / 多模态）

口径来源：`contract/dimensions.md`。四维**并列**放在 `dimensions` 对象里，
**不加权、不分级、不设阈值、不合成总分**（出现这类字段即越界）。

| 维 | 只读这一份（就在 `run.json` 旁边） | 该报什么 |
|---|---|---|
| `performance` | `measure.json` | `wall_s` / `turns` / `out_tokens_per_wall_s` / `round_gap.{n,p50,p90,p90_reason,max}` / `ttft_s` / `tool_calls` |
| `coverage` | `coverage.json` | `counts.{mechanical,semantic,none,not_applicable,total}` / `unregistered_clauses.{clauses_total,registered,delta}` / `discriminating_power` |
| `interactivity` | `interact.json` | 三个场景的 `id` / `turns_to_done` / `verdict` + `cost` |
| `modalities` | `measure.json` 的 `modalities` | 逐模态 `modality` / `status` / `reason` / `evidence` |

三条纪律：

1. **view 不自己算一个数**：全部原样照抄。不把 `wall_s / turns` 当 `out_tokens_per_wall_s`，
   不给 `p90` 硬凑值，不按均价乘 token 算 cost；`ttft_s` / `tool_calls` 结构上拿不到就写 `unknown`。
2. **缺就是缺**：文件不在 / 字段是 `unknown` / `null` 时整段写 `unknown` 或 `not_applicable`，
   **绝不用 0 兜底**；`not_applicable` 也不许印成 `0%` / `0/0` / 「不支持」。
3. **`run` 子树不动**：`dimensions` 只加在导出原文的**顶层**，`csv` 一个字节都不改。

出口落点：

- `python -m view.cli report --run <run.json> --json` 的顶层多一段 `dimensions`；
- md 追加四节（`## 4. 运行性能` / `## 5. 判据覆盖` / `## 6. 交互性` / `## 7. 多模态`），
  每节**正文第一行**是该维「不能支撑什么」（`contract/dimensions.md` §6 原话照抄）；
- 看板新增轮间隔分布（标 `p50` / `p90` / `max`）与覆盖表证据级分布（四类计数），内联 SVG、零外链。

产物生成（四条命令，连跑两次逐字节相同）：

```
python -m view.cli report --run runs/fx-rework01/run.json --json --out out/fx.json
python -m view.cli export --run runs/fx-rework01/run.json --format csv --out out/fx.csv
python -m view.cli export --run runs/fx-rework01/run.json --format md  --out out/fx.md
python -m view.board     --run runs/fx-rework01/run.json --out out/board.html
```

## 空值三态（§4，最容易写错的地方）

| JSON 值 | csv 单元格 | md 单元格 | 含义 |
|---|---|---|---|
| `"unknown"` | `unknown` | `unknown` | 本该有、但拿不到（来源缺/坏/无权限） |
| `null` | `""`（空串） | `null` | 已知为空 / 本就不适用（如 `exit_code: null`、`asserts_total: null`、amend 行 `cost.usd: null`） |
| `0` | `0` | `0` | 真的是 0（真的没返工、这一轮真的没花钱） |

- **任何情况下都不许把缺失写成 `0`**：那会在看板上变成「完美」；
- 比率分母是 `null`/`0` 时写 `null`（csv 里是空串），**不出 `0/0`、不出 `0%`**；
- `cost.usd` 一律带 `>=`（§5.4：它是下限）+ `complete` 标记，绝不把下限说成完整成本；
- 表头/列序是契约原文，改它等于改契约 —— 先改 `contract/trace.md`，不在导出时顺手加列。

## csv 每列取哪一级的数

`csv` 一行一个 step，所以每个 step 级列都取**这个 step 自己的值**：

| 列 | 取哪一级 | 说明 |
|---|---|---|
| `run_id` / `verdict` | run 级，每行**复制** | `verdict` 是「这次 run 的结论」，不是「这一步的结论」；步级判分看 `asserts_*` / `first_pass_rate` / `exit_code` |
| `cost_usd` / `cost_complete` / `cost_basis` | `step.cost` | **不是** `run.cost`。`fx-rework01` 正好是反例：`run.cost.complete = false`（`unknown_rows: 1`），但步 1 的 cost 是完整的；run 级聚合在 md 的 Run 摘要里 |
| 其余 | `step.*` / `step.tokens.*` / `step.asserts.*` | 三态照 §4 |

## md 的两条排版纪律

1. 单元格里不出现换行（空白折叠成单个空格），所以任何一行都不会以 `[` 开头 ——
   `nogrep:^\[@out/fx.md` 钉的就是「md 里混进了 `[export] ...` 这种进度行」；
2. 不写导出时间戳、不写绝对路径、不写进程 id（§7.5：同一份输入重跑字节相同）。

## 退出码（§7.1）

| code | 何时 |
|---|---|
| `0` | 导出跑完（导出是查询） |
| `3` | 输入合法但数据不可用（`steps: 0`，§8.1 的形状）：产物照常输出，不许当成「全部通过」 |
| `1` | 工具自身故障：缺参数、`.plan/**` 越界路径、写盘失败、内部异常 |

出错时 stdout 仍是**一个**合法 JSON 错误体 `{"ok": false, "error": {code, message, hint}}`，人话在 stderr。

## 三份导出互相校验（同一份 `run.json`）

`json` / `csv` / `md` 是同一份 `run.json` 的三个出口，**数不许两样**。`--self-test` 里做了机械交叉校验：
把三份导出各自反解成同一张「数」的表（`n` / `rounds` / `cost_usd` / `asserts_total` / `asserts_passed`），
逐格比，并把比了几个数打进 stdout 的 JSON（`cross_check: {rows, columns, numbers, mismatches}`）。
比的是「渲染出来的字节」，不是从 document 再抄一遍 —— 抄一遍什么错都验不出来。

## 自检

```
python -m view.export --self-test
```

自检只碰 `view/fixtures/export/`（`full.json` = 字段齐全含 fail/timeout/unacked，`degenerate.json` = 残缺体 /
`steps: []`）和自己手写的样本（`CLEAN_DOC` = 无失败断言、`ALL_UNKNOWN_DOC` = 关键字段全 `unknown`），
不读 `runs/`、不写 `out/`；全部通过退出码 `0`，失败清单在 stdout 的 JSON 里、明细在 stderr。

覆盖的退化场景（每条一个 PASS）：

| 场景 | 钉住什么 |
|---|---|
| `steps: 0` | md 三张表照出、Step 表整行写「（无步骤）」且无空单元格、不出 `0/0` / `0%`；退出码 `3` |
| 关键字段全 `unknown` | csv 里是 `unknown`，**一个 `0` 都不许冒出来**（真的 0 如 `rework` 照抄 `0`） |
| 字段带逗号 + 双引号 | RFC4180 加引号、内部引号翻倍 |
| 写盘失败 | 退出码 `1`、stdout 仍是**一个完整** JSON 错误体（不是半截产物）、stderr 有人话 |
| 同一输入导出两次 | 三种格式字节一致（§7.5） |
| `--format json` | 产物与源 `run.json` **逐字节**相同（不是「等价 JSON」） |
| 三种导出之间 | 交叉校验的 cost / rounds / asserts 三组数一致 |
| 全文用词 | 导出里不出现「总分」「加权」（bench 不做加权总分） |
| `.plan/**` 路径 | 入口处机械拒读（`lane_b_plan_path` / 退出码 `1`），不打开文件 |
