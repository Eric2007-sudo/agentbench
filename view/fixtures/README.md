# `view/fixtures/` · 回归对比夹具（B 路）

> **本目录所有 `run.json` 里的数字是编的**（示意数据），路径与哈希也是编的。
> 它们只是 shape 正确的假样本，用来喂 `python -m view.regress --self-test`；
> 真实数据只有采集器落到 `runs/<run_id>/run.json` 之后才有，`view/` 不许读别的来源。

## 为什么要有这一套

`runs/fx-rework01*/run.json` 那对真实夹具是 A 路（`collect/`）的产物，随时可能被改写；
自检不能挂在别人的中间状态上，所以这里备一份自己的编造样本：字段形状照 `contract/trace.md` §2 抄，
判定口径照 §6 走。**只读 `run.json`，不读 `.plan/`、不读产物目录、不 import `collect.*`。**

## 案例清单

| 目录 | 覆盖的契约条款 | 期望结论 |
|---|---|---|
| `case-a-new-failure/` | §6 新增失败 / 已修复 / `asserts.added.removed.changed` / 成本下降 + `is_lower_bound` / `mixed` 优先于 `regressed` | `verdict: "mixed"`，退出码 `2` |
| `case-b-unknown-cost/` | §6 成本行「任一 `unknown` → 两个 delta 都 `unknown`，只并列两侧原值」；§4 不许用 `0` 冒充缺失 | `delta_usd/delta_pct/direction: "unknown"`，`verdict: "unknown"`，退出码 `3` |
| `case-c-unchanged/` | §6 四段全相等 | `delta_usd: 0.0`、`direction: "flat"`、`verdict: "unchanged"`，退出码 `0` |
| `case-d-no-asserts/` | §4「分母 `null` → 比率 `null`」，不许出 `0/0`、`NaN`、`0%` | `asserts.before: null`、`delta: null`，输出里没有 `NaN` |

每例都是 `base.json`（基准）+ `cur.json`（本次）一对，`run.id` 不同、其余按案例设计。

### `export/` · 导出三件套的夹具

`export/` 是另一件事（`python -m view.export --self-test` 用），和上面的回归夹具分开放：

| 文件 | 覆盖的契约条款 | 期望结论 |
|---|---|---|
| `export/full.json` | §7.3 csv 表头顺序 / `unknown` 与 `""` 可区分 / 真 `0` 写 `0`；§7.4 三张表 + 失败断言表列出 fail/timeout/unacked；§7.2 json 等价原文；§7.5 定点 6 位 | 三件套渲染成功、两次字节一致，退出码 `0` |
| `export/degenerate.json` | §2.6 残缺体 / §8.1「`steps: 0` + `asserts_total: null` + `verdict: "unknown"`」，不许出 `0/0`、`0%` | csv 只有表头，md/ json 照常输出，退出码 `3` |

两份都是**编的示意数据**（`run.id` 以 `export-` 开头，数字与哈希都是假的），
断言里没有任何一条挂在它们的真实数值上 —— 只钉形状与口径。

## 输出里额外的一段：`regression.steps`

`regress.py` 的输出除了 trace.md §6 那五段，还有一段 `regression.steps[]`：

```
{"n": 1, "rounds": {"before": 2, "after": 2, "delta": 0},
          "rework": {"before": 1, "after": 0, "delta": -1}}
```

- 字段名**照抄** trace.md §2.3（`rounds` / `rework`），不新造语义，只是按 `step.n` 并列并给差；
- 轮次与返工的变化**不参与** `verdict`：§6 的枚举只认 `fixed` / 成本降 / 通过率升，
  所以「返工少了」仍记为 `unchanged`，事实留在本段，不许拿它改判；
- 一侧没有这一步、或该字段是 `"unknown"` → 那侧写 `"unknown"`、`delta` 也写 `"unknown"`，
  **绝不当成 0 轮 / 0 次返工**（§4）。

上面 case A/C/D 的自检都钉了这一段；夹具里的 `rounds` / `rework` 同样是编的示意值。


## 自检怎么跑

```
python -m view.regress --self-test
```

- 全部通过 → 退出码 `0`；有失败项 → 退出码 `1`，失败项名字在 stdout 的 JSON 里、明细在 stderr。
- 自检只碰本目录与 `view/regress.py`，不碰 `runs/`、不写 `out/`。

## 改写纪律

1. 夹具里**不许出现「看起来像真实账」的数字**：都是编的，改动时保持一致口径（`cost` 小数 6 位、比率 6 位）。
2. 一个案例只钉一件事；要新增判据就新增案例，别把 `case-a` 改成大杂烩。
3. 夹具是「形状 + 口径」的证据，不是判分结果的证据 —— 判分结果永远以真跑的 `run.json` 为准。
