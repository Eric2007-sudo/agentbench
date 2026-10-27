# slices.md · bench 的切片清单

> 配套 `contract/trace.md`（字段与判定口径以它为准）。本文只回答：**分几步做、每步交什么、谁碰哪个目录、这一步怎么算做完。**
> 步数控制 **7 步**；每步只做一件事，做完就能跑断言。

---

## 1. 步骤表

| 步号 | 标题 | 归属路 | 产物路径 | 依赖步 | 机械断言 |
|---|---|---|---|---|---|
| 1 | 归一化模型与校验器（`run`→`step`→`round` 三级，含 `null`/`unknown` 校验） | `collect/` | `collect/model/trace.py`、`collect/model/schema.json` | — | `file:exists:collect/model/trace.py`<br>`cmd:python -m collect.model.trace --self-test` |
| 2 | 返工夹具：手写一套「有返工 + 有坏行 + 有 unknown」的账本，另加一份「修好一条」的对照版 | `collect/` | `collect/fixtures/rework01/`、`collect/fixtures/rework01b/`（各含 `.plan/plan.json`、`.plan/ledger.jsonl`、`.plan/gates.log`、`artifacts/`） | 1 | `file:exists:collect/fixtures/rework01/.plan/ledger.jsonl`<br>`file:exists:collect/fixtures/rework01b/.plan/ledger.jsonl`<br>`cmd:python -m collect.fixtures.check` |
| 3 | 只读采集器：读 `.plan/` + 宿主 db + 产物目录 → 落 `run.json` | `collect/` | `collect/ingest/plan_dir.py`、`collect/ingest/host_db.py`<br>产出 `runs/fx-rework01/run.json`、`runs/fx-rework01b/run.json` | 1, 2 | `file:exists:runs/fx-rework01/run.json`<br>`grep:ledger\.bad_json@runs/fx-rework01/run.json`（坏行必须落进 `degraded_reasons`，不许静默）<br>`cmd:python -m collect.ingest.plan_dir --root collect/fixtures/rework01 --out runs/fx-rework01/run.json` |
| 4 | 判分器四类：exit code / 断言通过率 / 产物 diff / 成本与轮次 | `collect/` | `collect/score/__init__.py`、`collect/score/exitcode.py`、`collect/score/asserts.py`、`collect/score/diff.py`、`collect/score/cost.py` | 1, 3 | `file:exists:collect/score/exitcode.py`<br>`file:exists:collect/score/diff.py`<br>`cmd:python -m collect.score --self-test` |
| 5 | 回归对比：本次 vs 基准 → 新增失败 / 已修复 / 成本变化 / 断言变化 | `view/` | `view/regress.py`、`runs/regress-fx.json` | 4 | `file:exists:view/regress.py`<br>`cmd:python -m view.regress --base runs/fx-rework01/run.json --cur runs/fx-rework01b/run.json --out runs/regress-fx.json`<br>`grep:new_failures@runs/regress-fx.json` |
| 6 | 导出 `json` / `csv` / `md`（字段与格式见 trace.md §7.2–7.5） | `view/` | `view/export/jsonio.py`、`view/export/csvio.py`、`view/export/mdio.py`、`out/` | 3, 4 | `file:exists:view/export/csvio.py`<br>`cmd:python -m view.export --run runs/fx-rework01/run.json --format csv --out out/fx.csv`<br>`cmd:python -m view.export --run runs/fx-rework01/run.json --format md --out out/fx.md` |
| 7 | 看板（轮次/成本曲线、失败分布）+ `--json` 出口统一 | `view/` | `view/board.py`、`view/cli.py` | 5, 6 | `file:exists:view/board.py`<br>`cmd:python -m view.cli report --run runs/fx-rework01/run.json --json`<br>`nogrep:^\[@out/fx.md`（`md` 导出不许夹带进度行） |

> 依赖是「产物依赖」，不是「耐心依赖」：步 4 拿不到步 3 的 `run.json` 就不许开工；步 5、6 只吃 `run.json`，可以并行。

---

## 2. 两路边界

| 项 | `collect/`（路 A：采集 + 归一化 + 判分） | `view/`（路 B：看板 + 回归 + 导出） |
|---|---|---|
| 可写目录 | `collect/**`、`runs/**`（只写 `runs/<id>/run.json`）、`collect/fixtures/**` | `view/**`、`out/**`、`runs/regress-*.json` |
| 只读目录 | `.plan/**`、宿主 db（`?mode=ro`）、目标产物目录、`contract/**` | `runs/**/run.json`、`contract/**` |
| **绝对不碰** | `view/**`、`out/**` | `.plan/**`（**一次都不许读**，包括 `plan.json`）、宿主 db、目标产物目录、`collect/**` |
| 允许 import | 自己的子包 + 标准库 | 自己的子包 + 标准库（**不许 import `collect.*`**） |
| 交接物 | 写出 `run.json` | 只读 `run.json` |

**硬声明**：

1. **交接物只有 `run.json` 一个**。`view/` 需要的一切（轮次、tokens、cost、断言结果、产物 hash、`degraded_reasons`）都必须已经躺在 `run.json` 里；`view/` 一旦需要「再回去读一眼 `.plan/`」，说明 `run.json` 契约漏字段，改契约而不是越界读。
2. **两路不碰同一批文件**：`collect/` 只写 `runs/`，`view/` 只读 `runs/`；除此之外写集合交集为空。`runs/` 是唯一的单向传送带（A 写 B 读），不存在双向写。
3. **`contract/` 两路都只读**。契约改动只能由新的契约步骤完成，不许在实现步骤里顺手改契约来迁就代码。
4. 顺手校验：`cmd:` 断言里加一条覆盖性检查（步 7 的 `nogrep:`）不算越界；但任何步骤都**不许**用 `cmd:` 去跑「全仓测试」「全仓 grep」。

---

## 3. 第一个可见产物的位置

**步 3 之后** —— `runs/fx-rework01/run.json` 落盘那一刻，肉眼就能看到真实数据：

```
python -m collect.ingest.plan_dir --root collect/fixtures/rework01 --out runs/fx-rework01/run.json
type runs\fx-rework01\run.json
```

此时 `run.rounds` / `run.tokens` / `run.cost` / `steps[].asserts` 都是**从账本算出来的真数**（夹具里的数是我手写的示意值，换成真实目标目录再跑一次就是真账）：

```
python -m collect.ingest.plan_dir --root <真实目标目录> --out runs/real01/run.json
```

- 步 1、2 只有代码和夹具，看不到「运行数据」；步 4 之后看到的是**判分数**（还没格式化）。
- 所以：**数据首现于步 3，可读的结论首现于步 4，好看的首现于步 6/7**。这三者别混。

---

## 4. 怎么自测（造出「一次有返工的运行」）

夹具 = 手写的一份「假目标目录」，不依赖任何工具就能读。两个夹具**只差一条断言**，用来喂回归对比。

### 4.1 `collect/fixtures/rework01/`

```
collect/fixtures/rework01/
├── .plan/
│   ├── plan.json       # 2 步：n=1 "写契约"、n=2 "写采集器"；accept 各 2~4 条
│   ├── ledger.jsonl    # 4 行，见下
│   └── gates.log       # 步 1 先 FAIL 后 PASS（返工证据）
└── artifacts/
    ├── slices.md       # 步 1 产物
    └── plan_dir.py     # 步 2 产物
```

`ledger.jsonl` 设计（**示意数据，数字和哈希都是编的**）：

| 行 | 内容要点 |
|---|---|
| 1 | `{"ts":"...","step":1,"in":20000,"out":1100,"reqs":1,"cache_read":10000,"cost":0.0042,"src":"opencode.db","session":"fx-1"}` |
| 2 | `{"ts":"...","step":1,"in":21000,"out":1000,"reqs":1,"cache_read":10000,"cost":0.0049,"src":"opencode.db","session":"fx-1","note":"第一次 gate 没过，补了 collect/ 断言"}` |
| 3 | `{"ts":"...","step":2,"in":18000,"out":900,"reqs":1}` ← **故意缺 `cost`** → 用来验 `cost.usd` 是下限、`complete:false`、`unknown_rows:1` |
| 4 | `{"ts":"...","step":2,  这一行是坏 JSON` ← **故意写坏** → 用来验「跳过坏行 + `degraded_reasons` 里出现 `ledger.bad_json:4`」 |

`gates.log` 设计（示意）：

```
[2026-10-26T20:55:40] gate #1 写契约
  FAIL  grep:collect/@contract/slices.md
      文件不存在: .../contract/slices.md
  PASS  file:exists:contract/trace.md

[2026-10-26T21:02:10] gate #1 写契约
  PASS  file:exists:contract/trace.md
  PASS  grep:collect/@contract/slices.md
```

→ 期望判分结果：步 1 的 `rework: 1`、`first_failed: true`、`first_pass_rate: 0.5`、`final_pass_rate: 1.0`；步 2 的 `cost.complete: false`；全 run `degraded: true`，`degraded_reasons` 至少两条。

### 4.2 `collect/fixtures/rework01b/`

与 `rework01` 同形，只改两处：步 1 的 `gates.log` **只有一次 PASS**（返工消失）、步 2 的第 3 行补上 `cost: 0.0031`。

→ 拿它当「本次」、`rework01` 当「基准」，步 5 的回归对比必须得出：`fixed` 含步 1 的那条 `grep:collect/@...`、`cost.delta_usd > 0`、`asserts.delta > 0`、`verdict` 为 `improved` 或 `mixed`（取决于新失败是否为空，实现须按 trace.md §6 的优先级判）。

### 4.3 还要验的三件「不做」的事

| 验什么 | 怎么验 |
|---|---|
| 残缺体：没有 `.plan/` | 拿 `collect/fixtures/empty/`（只有一个 `artifacts/` 的空目录）跑采集器 → `steps: 0`、`asserts_total: null`、`verdict: "unknown"`、退出码 `3`；**输出里不许出现 `0/0` 或 `0%`** |
| `--json` 干净 | `python -m view.cli report ... --json > o.json 2> e.txt`，`python -c "import json;json.load(open('o.json'))"` 必须成功；`e.txt` 里才允许有人话 |
| 字节级一致 | 同一夹具连跑两次，`certutil -hashfile` 或 `sha256sum` 两次 `run.json` 必须相同 |

---

## 5. 每步断言的写法（硬约束）

**只允许这 6 种前缀**，别的一律算非法断言：

| 前缀 | 形状 | 回答的问题 |
|---|---|---|
| `file:exists:` | `file:exists:collect/model/trace.py` | 产物在了吗 |
| `file:absent:` | `file:absent:collect/score/__pycache__` | 不该有的东西没留下吗 |
| `grep:` | `grep:unknown@runs/fx-rework01/run.json` | 里面有没有这个内容（正则，`@` 后是文件；路径和模式都不许跨越多个文件） |
| `nogrep:` | `nogrep:^\[@out/fx.md` | 里面是不是混进了不该有的东西 |
| `cmd:` | `cmd:python -m collect.score --self-test` | 跑一下，退出码 0 算过 |
| `ack:` | `ack:看板在 1280 宽下不横向滚动（人眼确认）` | 机械判不了、只能人确认 |

**写法纪律**：

1. **断言只回答「这一步做完了吗」**。不许写「整个仓库都干净」「所有测试都通过」「全仓 grep 无 TODO」——那是全仓检查，一条断言承担不了，还会互相拖累。
2. **一断一言**。一条 `cmd:` 里不许塞 `;` 串多个命令；`&&` 也不许（串了就分不清谁破了）。
3. `grep:` / `nogrep:` 的 `@` 后面**只能是一个文件路径**，不许通配符、不许目录。
4. `cmd:` 只能是**自检**（`--self-test`）或**这一步自己的产物检查**；不许跑网络、不许跑 LLM、不许跑全仓测试、不许超 30 秒。
5. `ack:` 尽量别用（等于没验）。真要用，一条断言只挂一件小事，且必须在 `plan.json` 的 note 里写清「谁、什么时候、看了什么」；`ack:` 未确认时判分器必须算**未通过**（见 trace.md §5.2）。
6. `file:absent:` 用来钉住「不留垃圾」：至少一条断言 `file:absent:collect/fixtures/rework01/.plan/run.json`，证明采集器没往来源目录里写东西 —— 这是只读约束的唯一机械证据。
