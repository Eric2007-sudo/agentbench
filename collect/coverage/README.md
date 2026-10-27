# collect/coverage —— 判据覆盖表（不出总分）

按 `contract/dimensions.md` **§2（正确性：覆盖表）** 实现。只出**计数 + 逐条证据级**，
没有加权、没有阈值、没有百分比；`mechanical` 是唯一「有机械证据」的级别，`semantic`
只说明有人看过（或给了 jev 概率），**不能当通过**。

```
python -m collect.coverage --self-test
python -m collect.coverage --run  runs/fx-rework01/run.json \
                            --score runs/fx-rework01/score.json \
                            --plan  collect/fixtures/rework01/.plan/plan.json \
                            --out   runs/fx-rework01/coverage.json
python -m collect.coverage --plan collect/fixtures/rework01/.plan/plan.json --json  # stdout 恰好一个 JSON，人话走 stderr
```

## 规格点从哪来

| 来源 | id | 说明 |
|---|---|---|
| `plan.json` 的 `steps[].accept` | `accept:<全局序号>` | 序号从 `steps[0].accept[0]` 起算、**跨步连续**（1..N），不是步内编号、不是步号。条数每次从数据数，**不写死**。规格点必须来自**这次 run 实际执行的那个项目**的 `.plan/plan.json`（即 `<run.root>/.plan/plan.json`），不是采集方自己项目的计划 |
| `trace.md` 写明但没被 accept 覆盖的条款 | `clause:<短名>` | 人工登记在 `spec/points.json`，字段 `{id, clause, level, evidence, reason}`；`evidence` 必须写成 `<真实文件>:<真实行号>` |

`items[]` 每条是 `{id, source, prefix, level, expr, evidence, reason}`：`prefix` 是 id 的命名空间
（`accept:` / `clause:`），与 `id` 前缀必须一致；`not_applicable` 必带 `reason`，`mechanical` 必带 `evidence`。

**一致性守卫**：算交集后若 `|score 断言 ∩ plan.accept| * 2 < |plan.accept|`，说明 plan 与 score
不是同一个项目 —— CLI 直接非 0 退出，把两边条数与示例 `expr` 打到 stderr，**不写任何
`coverage.json`**（`--json` 时 stdout 仍是 `{"ok": false, "error": {...}}`）。没有 `--score`
或 score 里一条断言都没有时不比。

## 证据级怎么判（可复现）

- **`mechanical`** ⟺ 该条 `accept` 在 `score.json` 的 `asserts.by_step[*].items` 里有一条
  **`expr` 原文完全相等**、且 `result ∈ {pass, fail}` 的断言。`timeout` / `unacked` /
  `unknown` 都是「没验完」，不算机械证据。
  - `score.json` 是 **run 级** 判分，**没有** `steps` 顶层键；断言挂在
    `asserts.by_step[*].items`。
  - 本夹具的 `score.json` 有 2 个步骤、4 条断言。规格点来自 `runs/fx-rework01` 的
    `run.root`（`collect/fixtures/rework01`）的 `.plan/plan.json`，它的 4 条 accept 与这 4 条
    断言原文完全相等，所以 `mechanical = 4`（全部 pass）。拿 bench 自己的 `.plan/plan.json`
    去比会被一致性守卫拦下（交集不过半，非 0 退出），不会输出一张几乎全 `none` 的表。
- **`semantic`** ⟺ `spec/points.json` 登记 `level: "semantic"`，并在 `reason` 里写人工依据。
- **`none`** ⟺ 既没有断言结果、也没有人工登记（`clause` 条目也可以登记为 `none`，表示「已知没覆盖」）。
- **`not_applicable`** ⟺ 本次 run 不适用，**必须**带 `reason`；不是 0 分。

## 未登记条款缺口

`clauses_total` 是 `trace.md` 里**可枚举的条款数**（自动口径，写死在 `table.count_clauses()`）：

> 含规范性措辞（必须 / 不许 / 不得 / 严禁 / 禁止 / 永远 / 一律 / 只能 / 只许 / 不可 / 绝…）的正文行；
> 跳过空行、标题行（`#`）、代码块内、表格分隔行（`|---|`）。

同一句话在多少处出现就按行计多少 —— 这是「可枚举」的口径，不是语义去重。`delta = clauses_total - registered`；
`delta > 0` 时输出里必须出现 `discriminating_power: "不具备判别力"`（只许出现在这里）。

## 落盘纪律

- UTF-8 无 BOM、`indent=1`、换行 `\n`、末尾恰好一个换行；同输入两次输出**逐字节相同**。
- 禁用字段按**键名**判（`degraded_reasons` 里含 `grade` 四个字母，不许误伤），不做整份 JSON 子串搜索。
- 只读 `plan.json` / `score.json` / `run.json` / `contract/trace.md`；本目录只往外写 `--out` 指到的那个文件。

## 退出码

`0` 出表完成；`3` 计划里一条 accept 都没有（已输出合法 JSON）；`1` 工具故障（参数错、读写失败、
登记表 evidence 不实、表自身不自洽、plan 与 score 不是同一个项目）。
