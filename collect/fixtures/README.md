# collect/fixtures —— 手写的「假目标目录」夹具

用途：给 `collect/ingest`（采集器）与 `collect/score`（判分器）喂一份**不依赖任何工具就能读**的运行现场，
其中故意留了三种脏：**返工**、**坏行**、**unknown**。口径来源是 `contract/slices.md` §4 与 `contract/trace.md` §5.2 / §5.4 / §8.2。

> **本目录里的数字是编的**（示意数据）。token、cost、轮次、时间戳、哈希全部是手写值，
> 不是任何一次真实运行的真账，只用来钉住字段形状与判定口径。

## 目录

```
collect/fixtures/
├── check.py            # 夹具自检：python -m collect.fixtures.check
├── rework01/           # 正本：有返工 + 有坏行 + 有 unknown
│   ├── .plan/plan.json     # 2 步：n=1「写契约」、n=2「写采集器」，accept 各 2 条
│   ├── .plan/ledger.jsonl  # 4 行（第 3 行缺 cost、第 4 行是坏 JSON）
│   ├── .plan/gates.log     # 步 1 先 FAIL 后 PASS（返工证据）、步 2 一次 PASS（带 exit=0）
│   └── artifacts/          # slices.md、plan_dir.py —— 步 1 / 步 2 的产物
└── rework01b/          # 对照：与 rework01 同形，只改两处
    └── （同样四件套，见下）
```

## `rework01` 里埋的三种脏

| 脏 | 位置 | 期望被采集/判分成什么 |
|---|---|---|
| 返工 | 步 1 的 `gates.log` 有两次 gate：第一次 `FAIL grep:collect/@contract/slices.md`，第二次全 `PASS` | 步 1 `rework: 1`、`first_failed: true`、`first_pass_rate: 0.5`、`final_pass_rate: 1.0`（trace.md §5.2） |
| 坏行 | `ledger.jsonl` 第 4 行不是合法 JSON | 跳过该行 + `degraded_reasons` 里出现 `ledger.bad_json:4`，前 3 行照常用（trace.md §8.2） |
| unknown | `ledger.jsonl` 第 3 行**故意没有 `cost`** | `cost.usd` = 已知部分之和 `0.0091`（下限）、`complete: false`、`unknown_rows: 1`；账本没有 `reasoning` 列 → `reasoning: "unknown"`（trace.md §5.4） |

顺带的固定事实：`rounds_coverage = {rows_total: 4, rows_with_reqs: 3}`、`rounds = Σ reqs = 3`、
全 run `degraded: true`，`degraded_reasons` 至少两条（坏行 + 缺 cost）。

## `rework01b` 只改哪两处（用来喂回归对比）

1. 步 1 的 `gates.log` **只剩一次全 PASS** → 返工消失，`first_pass_rate` 变 1.0；
2. `ledger.jsonl` 第 3 行**补上 `cost: 0.0031`** → 每一行都有 cost，`cost.complete` 变 true。

因此拿 `rework01` 当基准、`rework01b` 当本次跑回归（步 5），应当得出：
`fixed` 含步 1 的 `grep:collect/@contract/slices.md`、`cost.delta_usd = 0.0031 > 0`，
`verdict` 为 `improved`（新失败为空时）或 `mixed`（按 trace.md §6 的优先级判）。

断言层面的差别只有**首过**这一处，两份夹具的重复事实先说清：步 1 两次 gate 的断言条目集合相同（各 2 条），
收尾时两侧都是全 PASS。所以 `first_pass_rate` 是 0.5 vs 1.0（`rework: 1` vs `0`），
而 `final_pass_rate` 两侧都是 1.0 —— `slices.md` §4.2 期望的「`asserts.delta > 0`」对的是首过率的差；
按 `trace.md` §6「两侧 `final_pass_rate` 都拿得到才给 `delta`」的口径，收尾通过率之差为 0。
实现步 5 时以 `trace.md` 为准，这里只把事实摆出来，不替它选口径。

除这两处之外，两份夹具逐字节相同（`plan.json` 与 `artifacts/` 完全一致，`check.py` §7 会核）。

## 只读约束的机械证据

夹具是**来源**，采集器只许读：`rework01/.plan/` 与 `rework01b/.plan/` 下只有 `plan.json` / `ledger.jsonl` / `gates.log` 三件，
**不许出现 `run.json`**（那是采集器写进 `runs/<id>/` 的交接物）、也不许出现临时文件或 `__pycache__`。
`check.py` 与 gate 的 `file:absent:collect/fixtures/rework01/.plan/run.json` 一起钉住这一条。

## 自检

```
python -m collect.fixtures.check      # 全绿 exit 0，有不符合 exit 1（明细走 stderr）
```

`check.py` 纯读、不写任何文件、不 import 别的子包，只按上面写死的期望值把两份夹具核一遍。
