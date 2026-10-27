# trace.md · bench 的字段级契约

> 版本 `schema: 1` ｜ 唯一需求来源：`../README.md`（P0 十项 + 两路切分 + 边界）
> 本文只定义**数据形状与判定口径**，不含任何实现代码。
> 写法约定：`null` 与 `"unknown"` 语义不同，见 §4；示例里的数字全部是编的（示意数据），真实值只有采集时才有。

---

## 1. 范围与非目标

**一句话**：`run.json` 是「一次 agent 运行」的归一化快照 —— 把 `.plan/ledger.jsonl`、宿主 session 数据、产物目录三类**只读**来源，压成 `run` → `step` → `round` 三级结构，并附带四类机械判分结果与（可选的）回归对比结论。

**它不算什么**（越界即失败）：

| 不做 | 理由 |
|---|---|
| 不做 agent runtime | 不启动会话、不派发任务、不写 prompt、不注入上下文 |
| 不调模型 | 不打任何 LLM API；判分只认机械证据 |
| 不写别人的目录 | 只读 `.plan/`、只读宿主 db、只读产物目录；bench 自己的产物只落在 `runs/`、`out/` |
| 不做主观打分 | 「我觉得这步做得不错」不进字段；只有 exit code / 断言 / diff / token / cost |
| 不是日志聚合器 | 不做全文检索、不做日志 tail；只留判定需要的字段 |
| 不修改来源 | 打开 sqlite 用 `?mode=ro`；文件只 `open(..., "r")`；不加锁、不建缓存、不建 `__pycache__` 在来源目录里 |

`run.json` 必须**自足**：`view/` 只读它就能做回归对比、看板、导出，不许反过来去读 `.plan/`。

---

## 2. `run.json` 契约

### 2.1 三级形状

```
run.json
└── run         一次运行（1 个）
    ├── 元信息：id / 时间 / 目标 / 模型 / 结论
    ├── 汇总：轮次 / tokens / cost
    └── steps[]  计划里的每一步（0..n 个，按 n 升序）
        ├── 断言结果 / 产物 / 返工 / 归属路
        └── rounds[]   这一轮的逐轮明细（0..n 个，按 seq 升序）
```

计数口径固定：**`round` 是最小单元，`step` 是计划步骤，`run` 是整次运行**。任何一级都不许跨级塞字段（比如 `round` 里不许出现 `asserts`）。

### 2.2 `run` 字段

| 字段名 | 类型 | 必填 | 含义 | 示例（示意数据，数字是编的） |
|---|---|---|---|---|
| `schema` | int | 是 | 契约版本，本文件定义 `1` | `1` |
| `run.id` | string | 是 | 运行唯一 id，建议 `<目标目录名>-<started_at 紧凑>` | `"bench-20261026T204814"` |
| `run.root` | string | 是 | 被观测的目标目录（绝对路径，仅用于人读；导出时可用 `--strip-paths` 去掉） | `"D:\\...\\bench"` |
| `run.started_at` | string(ISO8601 带时区) \| `"unknown"` | 是 | 第一条 round 的时间；无 round 时取 plan 的 `created`；再没有 → `"unknown"` | `"2026-10-26T20:48:14+08:00"` |
| `run.finished_at` | 同 `started_at` | 是 | 最后一条 round 的时间；拿不到 → `"unknown"` | `"2026-10-26T21:12:03+08:00"` |
| `run.goal` | string \| `"unknown"` | 是 | `.plan/plan.json` 的 `goal` | `"bench：Agent 运行观测与评测台"` |
| `run.model` | `{provider, id}` \| `"unknown"` | 是 | 来自宿主 session 记录的 `model.providerID` / `model.id`；多模型混跑时取轮次最多的那个，并在 `run.model.mixed: true` 标注 | `{"provider":"<providerID>","id":"<modelID>","mixed":false}` |
| `run.rounds` | int \| `"unknown"` | 是 | 全 run 轮次，口径见 §5.4 | `7` |
| `run.rounds_coverage` | `{rows_total, rows_with_reqs}` | 是 | 账本行总数 / 带 `reqs` 的行数（让读者判断轮次可信度） | `{"rows_total":5,"rows_with_reqs":5}` |
| `run.tokens.input` | int \| `"unknown"` | 是 | Σ 各 round 的 input | `184321` |
| `run.tokens.output` | int \| `"unknown"` | 是 | Σ output（**不含** reasoning，见 §5.4 口径警告） | `9233` |
| `run.tokens.reasoning` | int \| `"unknown"` | 是 | Σ reasoning | `1720` |
| `run.tokens.cache_read` | int \| `"unknown"` | 是 | Σ cache read | `91200` |
| `run.tokens.cache_write` | int \| `"unknown"` | 是 | Σ cache write | `0` |
| `run.tokens.complete` | bool | 是 | 所有 round 的 token 字段都拿到了才 `true` | `true` |
| `run.cost.usd` | number \| `"unknown"` | 是 | **已知部分之和**，即下限，不是完整成本 | `0.0412` |
| `run.cost.complete` | bool | 是 | 每一行都有 cost 才 `true` | `false` |
| `run.cost.basis` | `"host"` \| `"estimate"` \| `"mixed"` \| `"unknown"` | 是 | 宿主真实记录 / 按价目重算的上界 / 两者混 / 都没有 | `"host"` |
| `run.cost.unknown_rows` | int | 是 | 拿不到 cost 的账本行数 | `1` |
| `run.steps` | array\<step\> | 是 | 按 `n` 升序 | 见 §2.3 |
| `run.verdict` | `"pass"` \| `"fail"` \| `"unknown"` | 是 | 四类判分的总结论，规则见 §5.5 | `"fail"` |
| `run.sources` | array\<object\> | 是 | 每条来源一条：`{kind, path, present, readable, note}`，`kind ∈ plan_json/plan_md/ledger/gates_log/notes/host_db/artifacts` | `[{"kind":"ledger","path":".plan/ledger.jsonl","present":true,"readable":true,"note":""}]` |
| `run.degraded` | bool | 是 | 出现任一 `"unknown"`、坏行、缺失来源即为 `true` | `true` |
| `run.degraded_reasons` | array\<string\> | 是 | 机器可读代码 + 人话，一行一条 | `["ledger.bad_json:4 第 4 行不是合法 JSON，已跳过"]` |
| `run.excluded` | array\<string\> | 否 | 声明**明确不读**的目录（来自 plan 的 `exclude`），用于自证没越界 | `["D:\\...\\tally","D:\\...\\tollgate"]` |

### 2.3 `step` 字段

| 字段名 | 类型 | 必填 | 含义 | 示例（示意数据） |
|---|---|---|---|---|
| `n` | int | 是 | 步号，来自 plan | `2` |
| `title` | string \| `"unknown"` | 是 | plan 里的标题 | `"只读采集器"` |
| `lane` | `"collect"` \| `"view"` \| `"unknown"` | 是 | 归属路，映射表见 `slices.md` | `"collect"` |
| `status` | `"done"` \| `"todo"` \| `"blocked"` \| `"unknown"` | 是 | plan 里的状态；`"unknown"` 表示 plan 里没这个字段 | `"done"` |
| `planned_out` | string \| `"unknown"` | 是 | plan 声明的产物路径（**声明**，不是实测） | `"collect/ingest/plan_dir.py"` |
| `artifacts` | array\<object\> | 是 | 实测：`{path, exists, bytes, sha256}`；`exists:false` 时 `bytes`/`sha256` 为 `null` | `[{"path":"collect/ingest/plan_dir.py","exists":true,"bytes":4210,"sha256":"<64 hex>"}]` |
| `asserts.total` | int \| `null` \| `"unknown"` | 是 | 断言总条数（分母）。`null` = 计划里就没有断言；`"unknown"` = 计划读不到 | `4` |
| `asserts.passed` | int \| `"unknown"` | 是 | 通过条数 | `4` |
| `asserts.failed` | int \| `"unknown"` | 是 | 确定失败的条数（含 `ack:` 未确认） | `0` |
| `asserts.timeout` | int \| `"unknown"` | 是 | `cmd:` 超时、既不算过也不算破的条数 | `0` |
| `asserts.unacked` | int \| `"unknown"` | 是 | `ack:` 尚未人工确认的条数 | `0` |
| `asserts.items` | array\<object\> | 是 | `{expr, kind, result, detail}`；`kind ∈ exists/absent/grep/nogrep/cmd/ack`；`result ∈ pass/fail/timeout/unacked` | `{"expr":"file:exists:collect/ingest/plan_dir.py","kind":"exists","result":"pass","detail":""}` |
| `assert_pass.first_pass_rate` | number(0..1) \| `"unknown"` | 是 | 第一次就过的比例（`passed_first / total`） | `0.75` |
| `assert_pass.final_pass_rate` | number(0..1) \| `"unknown"` | 是 | 收尾时的比例 | `1.0` |
| `exit_code` | int \| `null` \| `"unknown"` | 是 | `null` = 这一步没有可执行命令（不适用）；`"unknown"` = 有命令但拿不到码 | `0` |
| `exit_code_src` | `"host"` \| `"gate"` \| `"score"` \| `null` \| `"unknown"` | 是 | 码的来源，见 §5.1 | `"gate"` |
| `rework` | int | 是 | 返工次数，定义见 §5.2 | `1` |
| `first_failed` | bool \| `"unknown"` | 是 | 第一次判定就没过则 `true`（返工定位的锚点） | `true` |
| `rounds` | int \| `"unknown"` | 是 | 这一步的轮次（Σ 该步账本行的 `reqs`） | `3` |
| `tokens` | object | 是 | 形状同 `run.tokens`（含 `complete`） | `{"input":60112,"output":3010,"reasoning":540,"cache_read":30000,"cache_write":0,"complete":true}` |
| `cost` | object | 是 | 形状同 `run.cost` | `{"usd":0.0131,"complete":false,"basis":"host","unknown_rows":1}` |

### 2.4 `round` 字段

| 字段名 | 类型 | 必填 | 含义 | 示例（示意数据） |
|---|---|---|---|---|
| `seq` | int | 是 | 步内从 1 递增的轮序号 | `2` |
| `at` | string(ISO8601) \| `"unknown"` | 是 | 这一轮的时间（账本 `ts` / 宿主 `time_created`） | `"2026-10-26T20:55:40+08:00"` |
| `input` | int \| `"unknown"` | 是 | 本轮 input token | `21044` |
| `output` | int \| `"unknown"` | 是 | 本轮 output token | `1012` |
| `reasoning` | int \| `"unknown"` | 是 | 本轮 reasoning token | `180` |
| `cache_read` | int \| `"unknown"` | 是 | 本轮 cache 命中读 | `10240` |
| `cache_write` | int \| `"unknown"` | 是 | 本轮 cache 写入 | `0` |
| `cost.usd` | number \| `null` \| `"unknown"` | 是 | `null` = 这一轮确实不产生费用（如 amend 记账行）；`"unknown"` = 拿不到 | `0.0046` |
| `cost.est` | bool | 是 | `true` 表示按价目**估算**（上界），不是宿主真实值 | `false` |
| `rounds` | int | 是 | 本轮折算的轮次，正常 `1`；聚合行（如一次 sync 覆盖 3 轮）可为 `>1` | `1` |
| `note` | string | 是 | 账本 `note` 原文；没有则 `""` | `"sync 自动对账（3 轮）"` |
| `decision` | `{choice, outcome}` \| `null` | 否 | 决策记录（P0 #10）：当时选了什么方案 / 结果如何；从 `.plan/notes/` 或账本 note 里能提出来才填，提不出就是 `null`，**不许脑补** | `{"choice":"先写契约再写代码","outcome":"退了一次"}` |
| `src` | `"ledger"` \| `"host"` | 是 | 这一轮的原始来源 | `"ledger"` |

### 2.5 完整体（所有来源都在、字段齐全）

> **示意数据，全部数字与路径是编的**，只用来展示形状。

```json
{
  "schema": 1,
  "run": {
    "id": "bench-20261026T204814",
    "root": "D:\\...\\bench",
    "started_at": "2026-10-26T20:48:14+08:00",
    "finished_at": "2026-10-26T21:12:03+08:00",
    "goal": "（示意）把 P0 落成 CLI",
    "model": {"provider": "<providerID>", "id": "<modelID>", "mixed": false},
    "rounds": 7,
    "rounds_coverage": {"rows_total": 5, "rows_with_reqs": 5},
    "tokens": {"input": 184321, "output": 9233, "reasoning": 1720,
               "cache_read": 91200, "cache_write": 0, "complete": true},
    "cost": {"usd": 0.0412, "complete": true, "basis": "host", "unknown_rows": 0},
    "verdict": "fail",
    "sources": [
      {"kind": "plan_json", "path": ".plan/plan.json", "present": true, "readable": true, "note": ""},
      {"kind": "ledger", "path": ".plan/ledger.jsonl", "present": true, "readable": true, "note": ""},
      {"kind": "gates_log", "path": ".plan/gates.log", "present": true, "readable": true, "note": ""},
      {"kind": "host_db", "path": "<home>/.local/share/opencode/opencode.db",
       "present": true, "readable": true, "note": "mode=ro"}
    ],
    "degraded": false,
    "degraded_reasons": [],
    "excluded": ["D:\\...\\tally", "D:\\...\\tollgate"],
    "steps": [
      {
        "n": 1,
        "title": "（示意）契约",
        "lane": "collect",
        "status": "done",
        "planned_out": "contract/trace.md",
        "artifacts": [{"path": "contract/trace.md", "exists": true, "bytes": 9100, "sha256": "<64 hex>"}],
        "asserts": {
          "total": 4, "passed": 4, "failed": 0, "timeout": 0, "unacked": 0,
          "items": [{"expr": "file:exists:contract/trace.md", "kind": "exists", "result": "pass", "detail": ""}]
        },
        "assert_pass": {"first_pass_rate": 1.0, "final_pass_rate": 1.0},
        "exit_code": null, "exit_code_src": null,
        "rework": 0, "first_failed": false,
        "rounds": 2,
        "tokens": {"input": 41000, "output": 2100, "reasoning": 300, "cache_read": 20000, "cache_write": 0, "complete": true},
        "cost": {"usd": 0.0091, "complete": true, "basis": "host", "unknown_rows": 0},
        "rounds_detail": [
          {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "input": 20000, "output": 1100,
           "reasoning": 150, "cache_read": 10000, "cache_write": 0,
           "cost": {"usd": 0.0042, "est": false}, "rounds": 1,
           "note": "", "decision": null, "src": "ledger"},
          {"seq": 2, "at": "2026-10-26T20:55:40+08:00", "input": 21000, "output": 1000,
           "reasoning": 150, "cache_read": 10000, "cache_write": 0,
           "cost": {"usd": 0.0049, "est": false}, "rounds": 1,
           "note": "gate 第一遍不过，改了", "decision": {"choice": "补 collect/ 断言", "outcome": "过了"}, "src": "ledger"}
        ]
      }
    ]
  }
}
```

> 注：`rounds[]` 在文件里落为 `steps[].rounds_detail`，避免和表示**轮次数**的标量 `rounds` 撞名；契约里口语仍称 `round`。

### 2.6 残缺体（缺 `.plan/`，只剩产物目录）

> 示意数据。注意 `steps: 0` 与 `asserts_total: null` 同时出现，**没有 `0/0`、没有 `0%`**（规则见 §8）。

```json
{
  "schema": 1,
  "run": {
    "id": "bench-20261026T210000",
    "root": "D:\\...\\some-other-dir",
    "started_at": "unknown",
    "finished_at": "unknown",
    "goal": "unknown",
    "model": "unknown",
    "rounds": "unknown",
    "rounds_coverage": {"rows_total": 0, "rows_with_reqs": 0},
    "tokens": {"input": "unknown", "output": "unknown", "reasoning": "unknown",
               "cache_read": "unknown", "cache_write": "unknown", "complete": false},
    "cost": {"usd": "unknown", "complete": false, "basis": "unknown", "unknown_rows": 0},
    "verdict": "unknown",
    "sources": [
      {"kind": "plan_json", "path": ".plan/plan.json", "present": false, "readable": false, "note": "目录不存在"},
      {"kind": "ledger", "path": ".plan/ledger.jsonl", "present": false, "readable": false, "note": "目录不存在"},
      {"kind": "artifacts", "path": ".", "present": true, "readable": true, "note": "只读快照"}
    ],
    "degraded": true,
    "degraded_reasons": ["no_plan_dir 目标目录没有 .plan/，没有步骤可归一化"],
    "steps": []
  },
  "summary": {"steps": 0, "asserts_total": null, "asserts_passed": null,
              "assert_pass_rate": null, "verdict": "unknown"}
}
```

---

## 3. 采集来源与只读约束

### 3.1 允许读的来源（穷举，别的都不许读）

| 来源 | 路径 | 能拿到什么（已核实的字段） | 拿不到时标 |
|---|---|---|---|
| 计划 | `.plan/plan.json` | `goal` / `created` / `updated` / `steps[].n,title,status,out,accept,depends,rounds,budget` / `acks` / `exclude` | `goal: "unknown"`，`steps: []` |
| 计划人读版 | `.plan/plan.md` | 只做交叉核对，不参与字段（可选读） | 不影响 |
| 账本 | `.plan/ledger.jsonl` | 逐行：`ts` / `step` / `in` / `out` / `reasoning`（在宿主逐轮数据里） / `reqs` / `cache_read` / `cache_write` / `cost` / `cost_est` / `src` / `session` / `note` / `amend` / `forced` | 见 §8.2 |
| 门禁日志 | `.plan/gates.log` | 每次 gate 的 `PASS/FAIL/TIMEOUT` + 断言原文 + 明细（`cmd:` 的明细里有 `exit=N`） | `asserts` 结果 `"unknown"` |
| 笔记 | `.plan/notes/` | 决策记录的原始素材（`decision.choice/outcome` 只能从这里或账本 note 提取） | `decision: null` |
| 宿主 session 数据 | `<home>/.local/share/opencode/opencode.db`，表 `session_v2`（会话累计）/ `session_message`、`message`（逐轮 `data` JSON） | `data.tokens.{input,output,reasoning}` / `data.tokens.cache.{read,write}` / `data.cost` / `data.model.{providerID,id}` / `time_created` / `session_id` | token/cost 全部 `"unknown"` |
| 产物目录 | `run.root` 下由 `plan.json` 的 `out` / `steps[].out` 指到的路径 | `exists` / `bytes` / `sha256`（文本另存行数） | `artifacts[].exists: false` |

**明确不读**：`.plan/exclude` 里列的目录（如 `tally`、`tollgate`）—— 采集器启动时先把 `exclude` 读出来，任何 `open()` 前先过滤，并记进 `run.excluded` 自证。

### 3.2 只读硬约束（违反即视为破坏数据）

1. 文件只用 `open(path, "r", encoding="utf-8", errors="replace")`；**不 append、不原子替换、不改 mtime、不删、不重命名**。
2. sqlite 必须 `sqlite3.connect("file:" + path + "?mode=ro", uri=True)`；不建索引、不 `VACUUM`、不 `ATTACH` 可写库。
3. 不在来源目录里落任何东西：不放临时文件、不放 `__pycache__`（`PYTHONDONTWRITEBYTECODE=1` 或 `sys.dont_write_bytecode`）、不放锁文件、不放 `.cache`。
4. 采集过程是**纯函数**：同一份来源 + 同一份代码 → 字节级相同的 `run.json`（见 §7.4）。因此不写「采集时间」这种每轮都变的字段（`run.started_at` 取自数据，不取 `now()`）。
5. 文件在采集过程中被改动（mtime/size 变了）→ 整份重读一次；仍不一致 → `degraded_reasons += "source.changed_during_read:<path>"`，用第二次的快照，不报错退出。

---

## 4. 归一化规则：`null` 与 `"unknown"`（本节是硬约束）

**两条不许破的规矩**：

> **① 严禁用 `0` 冒充缺失值。** 0 只表示「确实是 0」（这一步真的 0 轮、这一轮真的不花钱）。
> **② `null` 与 `"unknown"` 不混用，`null` 表示「已知为空/不适用」，`"unknown"` 表示「拿不到」。**

| 值 | 含义 | 判据 | 例子 |
|---|---|---|---|
| `null` | **已知为空 / 本就不适用**。系统知道答案，答案是「没有」 | 来源存在且可读，且结构上就不该有 | 这一步没有 `cmd:` 断言 → `exit_code: null`；计划里这一步没有断言 → `asserts.total: null`；没有 `.plan/` → 断言总数 `null`；amend 记账行不产生费用 → `round.cost.usd: null`；没有决策记录 → `decision: null` |
| `"unknown"` | **拿不到**。本该有，但来源缺 / 坏 / 无权限 / 不记 | 来源缺失、坏行、字段不存在、无价目、`present:false` | 宿主 db 里没这条 session → `input: "unknown"`；某账本行没有 `cost` 且本地无价目 → 该行 `cost.usd: "unknown"`；`.plan/` 在但 `gates.log` 丢了 → 断言结果 `"unknown"`；时间字段全无 → `at: "unknown"` |
| `0` | 真的 0 | 来源明确给出 0 | 这一轮 `reasoning: 0`（模型没思考）；`rework: 0`（一次没退） |

**传播规则**（求和 / 合并时）：

- **int 类字段（tokens、rounds）严格传播**：任一子项 `"unknown"` → 父项 `"unknown"`，**不许跳过它按剩下的求和**。同时把 `complete` / 覆盖率字段置为反映缺口的值，让读者知道为什么没有数。
- **cost 例外，因为口径是「下限」**：允许已知部分求和，但**必须** `cost.complete: false` + `cost.unknown_rows: n`，且 `usd` 语义固定为「**至少花了这么多**」。一行都拿不到时 `usd: "unknown"`（不是 0）。
- **比率**：分母是 `null` 或 `0` 时 → `null`（不适用，比如没断言）；分母是 `"unknown"` 时 → `"unknown"`；分子未知时分母已知 → `"unknown"`。**任何情况下不许出现 `0/0`、`NaN`、`0%` 当结论。**
- **判定类字段**（`verdict`）：缺证据就是 `"unknown"`，**不许默认 `"pass"`**。默认通过是最危险的偏差。

机器校验：实现里的校验器必须拒绝把 `0`/`-1`/`""`/`NaN` 当作缺失值；`"unknown"` 是**字符串字面量**，不是 `null`、不是 `NaN`、不是 `-1`。

---

## 5. 判分规则四类

只认机械证据；四类都输出「数」+「口径来源」，不做加权总分（加权总分就是主观打分）。

### 5.1 ① exit code

**码从哪来**（按优先级，且必须写进 `exit_code_src`）：

| 优先级 | 来源 | `exit_code_src` |
|---|---|---|
| 1 | 宿主 session 记录里该步命令的退出码（有才用） | `"host"` |
| 2 | `.plan/gates.log` 里 `cmd:` 断言明细中的 `exit=N` | `"gate"` |
| 3 | 本次判分按用例声明的验收命令执行一次得到的码（只跑用例声明的验收命令，在临时目录里跑，不改来源、不派发 agent 任务） | `"score"` |
| 4 | 都没有 | `"unknown"`，`exit_code: "unknown"` |

**判定**：`0` → pass；非 0 → fail；`"unknown"` → **未判**（不参与通过率分母以外的任何判定，单独计入 `unjudged`，不当作 fail 也不当作 pass）。
**这一步没有可执行命令** → `exit_code: null`、`exit_code_src: null`，那是「不适用」，不是「失败」。

### 5.2 ② 断言通过率

断言只回答「这一步做完了吗」，语法只有 6 种前缀（见 `slices.md` §5）：`file:exists:` / `file:absent:` / `grep:PAT@FILE` / `nogrep:PAT@FILE` / `cmd:` / `ack:`。

- **分母** `asserts.total` = 该步 `plan.json` 的 `steps[].accept` 条数（声明口径，不随执行结果变化）。
- **分子** `asserts.passed` = `result == "pass"` 的条数。
- **`ack:` 怎么算**：`ack:` 是人工确认项。未确认 → `result: "unacked"`，**计入分母、不计入分子**（即拉低通过率）——它表示「还没验完」。经 `tg ack "<原文>"` 确认后 → `result: "pass"`，计入分子。**不许因为它需要人工就把它从分母里剔掉**，那样会虚高。
- **超时 `cmd:`**：`result: "timeout"`，计入分母、不计入分子，另计 `asserts.timeout`；报告里必须与 `fail` 分开说（性质是「没验完」，不是「破了」）。
- **两个比率都要报**：`first_pass_rate`（第一次判定就过的比例，返工定位的关键）与 `final_pass_rate`（收尾时的比例）。
- **返工次数 `rework`**：同一步里，某条断言**首次结果不是 pass**，之后重跑出现**任何新结果**，计 1 次；同一条断言多次被重跑，每次结果变化计 1 次。`first_failed = (该步首次判定存在 fail/timeout/unacked)`。
- 通过率**不做判定**：不设「≥90% 就算过」这种阈值。通过率是观测值；`run.verdict` 只看 §5.5。

### 5.3 ③ 产物 diff

基准 = 上一次运行（或基线夹具）对同一路径采集到的 `artifacts`；本次 = 这次采集的 `artifacts`。按路径对齐：

| 判定 | 规则 |
|---|---|
| **新增** | 本次 `exists:true`，基准里没有该路径或 `exists:false` |
| **删除** | 基准 `exists:true`，本次 `exists:false` 或路径消失 |
| **修改** | 两侧 `exists:true` 且 `sha256` 不同 |
| **未变** | 两侧 `exists:true` 且 `sha256` 相同（一句话：**只看 hash，不看 mtime**） |

细则：

- **二进制 / 任意文件**：`sha256` 相等即「未变」；不等再报 `bytes` 变化与是否文本。
- **文本文件**：hash 不等时附 `diff_stat`（`added_lines` / `removed_lines` / `hunks`），用 `\n` 归一化行尾、忽略末尾空行。
- **数值容差（浮点）**：当两侧都是 JSON/数值结构时，数值字段视为「相等」的条件是
  `|a-b| <= max(1e-9, 1e-6 * max(|a|,|b|))`（绝对 1e-9、相对 1e-6）。
  理由：`0.0412` 这类 cost 在不同精度下会抖；容差写死，不许临场调。
  文本比对上仍以 `sha256` 为准 —— 容差**只作用于结构里的数值**，不做「近似文本 diff」。
- 基准缺失（第一次跑，没有上一版）→ 整个 diff 段 `"unknown"`，**不报「0 新增 0 删除」**（那会被看成「没变化」）。
- 产物读不到（权限/超大）→ 该条 `sha256: "unknown"`，并记 `degraded_reasons`。

### 5.4 ④ 成本与轮次

**轮次（`rounds`）**：

- 单位 = **一次 assistant 请求**。`rounds = Σ 账本行的 reqs`（tollgate 的 `reqs` 字段就是每行覆盖的请求数）。
- 缺 `reqs` 的账本行（如手记 `tg spend` 行、`amend` 行）按 `0` 计 —— 这是 tollgate 既有口径，沿用，不另立标准。
- **但**：`rows_with_reqs == 0` 且 `rows_total > 0` 时，`rounds: "unknown"`（一条都没记，凭什么叫「0 轮」），`rounds_coverage` 照实报。
- 宿主 db 的 assistant 消息条数**只做交叉校验**，输出在 `rounds_coverage` 旁的 note 里；不一致时以账本为主，并记 `degraded_reasons += "rounds.mismatch:ledger=7 host=8"`。

**成本（`cost`）**：

- 口径：**`cost.usd` 是下限**（`>=` 真实花费），因为部分账本行可能没有 cost。永远带 `complete` 与 `unknown_rows` 一起给出。
- 取值优先级：宿主记录的 `data.cost`（真实值，`basis: "host"`）> 按本地价目重算（上界估算，`basis: "estimate"`，`round.cost.est: true`）> 都没有（`"unknown"`）。
- 两种口径混在一份 run 里 → `basis: "mixed"`，并且**必须**在 `degraded_reasons` 里点明「含估算值，是上界不是真账」。
- 没有价目就**不许算钱**：编个均价乘 token 数等于编数字，明令禁止。
- token 口径警告：tollgate 的 `sync` 把 `reasoning` 合并进账本的 `out`（`in` / `out` 两列），而宿主逐轮数据里 `output` 与 `reasoning` 是分开的。归一化时**统一取宿主分列口径**；回落到账本合并口径时必须 `degraded_reasons += "tokens.reasoning_folded_into_output"`，否则 `output` 会系统性偏大。

### 5.5 总结论 `run.verdict`

- 有任一步 `asserts.failed > 0` 或 `exit_code` 非 0 → `"fail"`。
- 无 fail，但存在任何 `"unknown"` 关键字段（tokens/rounds/cost/断言结果）→ `"unknown"`。
- 无 fail 且无 unknown，且每步 `final_pass_rate == 1.0` → `"pass"`。
- **不许**在没有证据时给 `"pass"`。

---

## 6. 回归对比定义

输入：`baseline_run.json`（基准）与 `current_run.json`（本次）。输出挂在 `run.regression` 下。

**对齐键**：断言级 = `step.n` + 断言原文 `expr`（字符串精确相等）；成本级 = `run` 整体；产物级 = 产物路径。

| 结论 | 判定规则 | 备注 |
|---|---|---|
| **新增失败** `new_failures` | 基准 `result == "pass"` 且本次 `result ∈ {fail, timeout}` | 基准里没有这条断言 → 归 `asserts.added`，**不算新增失败** |
| **已修复** `fixed` | 基准 `result ∈ {fail, timeout, unacked}` 且本次 `result == "pass"` | 本次这条断言消失 → 归 `asserts.removed`，不算已修复 |
| **成本变化** `cost` | 两侧 `cost.usd` 都是 number 才给 `delta_usd` / `delta_pct`；任一 `"unknown"` → 两者都 `"unknown"`，只并列两侧原值 | 任一侧 `complete:false` → 附 `"is_lower_bound": true` |
| **断言变化** `asserts` | 两侧 `final_pass_rate` 都是 number 才给 `delta`；同时给集合级 `added` / `removed` / `changed` 列表 | 通过率变化与集合变化分开报，不许合成一个数 |

输出形状：

```json
{
  "baseline": {"run_id": "bench-20261025T...", "path": "runs/.../run.json"},
  "regression": {
    "new_failures": [
      {"step": 2, "expr": "cmd:python -m collect.ingest.plan_dir --self-test",
       "before": "pass", "now": "fail", "detail": "exit=1"}
    ],
    "fixed": [
      {"step": 1, "expr": "grep:collect/@contract/slices.md", "before": "fail", "now": "pass"}
    ],
    "cost": {
      "before": {"usd": 0.0310, "complete": true},
      "after":  {"usd": 0.0412, "complete": false},
      "delta_usd": 0.0102, "delta_pct": 32.9,
      "direction": "up", "is_lower_bound": true
    },
    "asserts": {
      "before": 0.75, "after": 1.0, "delta": 0.25,
      "added": [{"step": 3, "expr": "file:exists:collect/score/__init__.py"}],
      "removed": [],
      "changed": [{"step": 2, "expr": "cmd:...", "before": "pass", "now": "fail"}]
    },
    "uncompared": [{"step": 4, "expr": "ack:看板截图有人看过", "reason": "baseline.unknown"}],
    "verdict": "regressed"
  }
}
```

`regression.verdict` 优先级（从高到低，一次只出一个）：

1. `new_failures` 非空 → `"regressed"`
2. 否则 `new_failures` 空且 `fixed` 非空或成本降或通过率升 → `"improved"`
3. 四段全空/全相等 → `"unchanged"`
4. 既有 `new_failures` 又有 `fixed`/改善 → `"mixed"`（放在 1 之前判断：**mixed 优先于 regressed**，但**必须在报告顶部把新失败列在前面**）
5. 基准缺失或关键字段 `"unknown"` → `"unknown"`，且 `regression` 段照常输出已能算的部分

> 修正后的实际判断顺序：`unknown` → `mixed` → `regressed` → `improved` → `unchanged`。

没有 `--baseline` 时 → `regression: null`（**不适用**，不是 `"unchanged"`）。

---

## 7. 出口

### 7.1 `--json` 硬约束

- stdout **恰好**一个 JSON 对象，UTF-8 无 BOM，`ensure_ascii=false`，`indent=1`，末尾**一个** `\n`。不许多个对象、不许 JSON Lines。
- 所有人类提示（进度、警告、表格）与所有错误文本（含堆栈、人话解释）**全部走 stderr**。
- 出错时：stdout 仍输出一个纯 JSON 错误对象，退出码非 0：
  ```json
  {"ok": false, "error": {"code": "no_plan_dir", "message": "目标目录没有 .plan/", "hint": "..."}}
  ```
  `message` 是给机器的短句，人话版完整解释仍在 stderr。**不许** stdout 输出半截 JSON，也不许 stdout 为空却不给退出码。
- 成功时：`{"ok": true, ...}` 且顶层带 `run`（完整 `run.json`）。

**退出码**：

| code | 含义 |
|---|---|
| `0` | 跑完，且 `verdict ∈ {pass, unchanged, improved}` 或只是查询 |
| `2` | 跑完但结论是 `fail` / `regressed`（**判分结果**，不是工具故障） |
| `3` | 数据不可用（`steps: 0` / 关键字段全 `"unknown"`），但已输出合法 JSON |
| `1` | 工具自身故障（参数错、写盘失败、内部异常） |

### 7.2 导出 `json`

= `run.json` 原文（不裁剪、不重排语义）。

> **operator 裁定（2026-10-27，待用户复核）**：本节原文后一句「`--json` 输出与导出文件必须**字节一致**」
> 与 §7.1「成功时 stdout 是 `{"ok": true, ...}` 且顶层带 `run`」**互相矛盾** —— 信封比原文多一层、缩进也不同，
> 字节不可能相等。两个条款都不假，只是判据不同，按「判据冲突必须分列、写明采用哪条」处理：改为下面三条
> **可机械判**的判据，`--json` 的 stdout 与 `out/fx.json` **不要求**字节一致，任何报告不得把这条记成失败。
>
> | 编号 | 判据 | 怎么判 |
> |---|---|---|
> | **A** | 导出文件 = `run.json` 原文 | `out/fx.json` 与 `runs/<run>/run.json` **逐字节相同** |
> | **B** | 导出侧 stdout = 落盘文件 | `view.export --format json --out F` 的 **stdout 与 F 逐字节相同** |
> | **C** | 信封不裁 run | `--json` 信封里的 `run` 与 `run.json` 的 `run` 对象**深度相等**（不比字节、不看 `schema` 外层） |
>
> 三条都必须在 `--self-test` 里各有一条用例；A/B/C 任一不成立即视为本步未完成。

### 7.3 导出 `csv`（一行一个 step，表头顺序固定）

```
run_id,step,title,lane,status,rounds,in,out,reasoning,cache_read,cache_write,
cost_usd,cost_complete,cost_basis,asserts_total,asserts_passed,asserts_failed,
asserts_timeout,first_pass_rate,final_pass_rate,exit_code,exit_code_src,
rework,first_failed,verdict
```

- 缺失值单元格写字面量 `unknown`；**不适用**写空字符串 `""`（对应 JSON 的 `null`）—— CSV 里 `null` 与 `unknown` 必须可区分，**都绝不写 `0`**。
- 编码 `utf-8-sig`（Excel 直接开不乱码），行尾 `\n`，逗号分隔，含逗号/引号的字段按 RFC4180 加引号。

### 7.4 导出 `md`

三张表，顺序固定：

1. **Run 摘要**：id / 时间 / 模型 / 轮次 / in+out tokens / cost（带 `>=` 与 `complete` 标记）/ verdict / **返工（合计）**。

   > **operator 追加（2026-10-27，待用户复核）**：`返工（合计）` = 各 step 的 `rework` 之和，**口径必须写在同一行括号里**：
   > `Σ step.rework（来自账本 reqs − 1，与 gates.log 的 exit code 不是同一个来源，两处不一致是设定、不是缺陷）`。
   > 加这一行的原因：`rounds` 有 run 级合计、`rework` 却只有 step 级，读报告的人得自己把 Step 表的返工列加一遍 —— 而这就是「把不知道说成 0」旁边的另一个坑：**没合计不等于没有返工**。看板同样要有这个合计。
2. **Step 表**：`n` / 标题 / 路 / 状态 / 轮次 / tokens / cost / 断言 `passed/total` / 首过率 / exit code / 返工。
3. **失败断言表**：`step` / `expr` / `result` / `detail`（无失败则整表写一行 `（无失败断言）`）。

### 7.5 确定性（README 的「字节级一致」）

同一份输入重跑，导出必须字节相同。因此：

- 键序固定（本文件表格顺序，或字典序 —— 选定后不许改）。
- 浮点定点：cost 保留 6 位小数（`0.041200`），比率保留 6 位小数；token 一律整数。
- 不写导出时间戳、不写绝对临时路径、不写进程 id。
- 数组按稳定键排序（steps 按 `n`，rounds 按 `seq`，asserts 按声明顺序）。
- 换行统一 `\n`，文件末尾一个换行。

---

## 8. 错误与边界

### 8.1 目标目录没有 `.plan/`

必须输出 **`steps: 0` + `asserts_total: null` + `assert_pass_rate: null` + `verdict: "unknown"`**（形状见 §2.6），并：

- **不许报 `0/0`、`0%`、`0 失败`**：那会被看板画成「完美」。
- **不许报 `steps: "unknown"`**：这步是能数清的 —— 目录里确实一步都没有，所以是 `0`（`0` 的合法用法：真的 0）。
- **不许静默退化成「空报告成功」**：退出码 `3`，`error.code = "no_plan_dir"`，`degraded_reasons` 写明原因。
- 仍要采集产物目录（`sources` 里记 `artifacts`），因为「没计划但产物在」本身是有用信息。

### 8.2 数据源损坏

| 情况 | 处理 |
|---|---|
| `ledger.jsonl` 某行不是合法 JSON | 跳过该行；`degraded_reasons += "ledger.bad_json:<行号>"`；输出 `ledger_rows: {ok, bad}`；**其余行照常用**，不整份放弃 |
| `ledger.jsonl` 整份不可读（权限/编码/不存在） | token / rounds / cost 全 `"unknown"`，`run.verdict: "unknown"`，退出码 `3` |
| `plan.json` 存在但不合法 JSON | 视为没有计划（§8.1 形状），`degraded_reasons += "plan.bad_json"`，退出码 `3` |
| `plan.json` 有未知字段 / `schema` 高于本文件 | 忽略未知字段，不报错；`schema` 更高时 `degraded_reasons += "plan.schema_newer:<n>"` |
| `gates.log` 缺失 | 断言**总数**仍可从 `plan.json` 的 `accept` 数出（`asserts.total` 有数）；`passed/failed/timeout` 全 `"unknown"`，`pass_rate: "unknown"` |
| `gates.log` 有日志但缺某次的 PASS/FAIL 行 | 该步断言结果 `"unknown"`，不推断 |
| 宿主 db 不存在 / 表不存在 / 被锁 | 该来源 `present:false` 或 `readable:false`；token/cost 回落账本；账本也没有 → `"unknown"` |
| 产物文件超大（阈值实现里定，写进常量并记进 `degraded_reasons`） | 只取 `bytes` + `sha256` 流式算，不整份读进内存；不做文本 diff |
| 断言写法不认识（前缀不在 6 种里） | `result: "fail"` + `detail: "无法识别的断言语法"`，**不静默放行**（与 tollgate 同口径） |
| `run.json` 自身写盘失败 | 退出码 `1`，stdout 输出 `{"ok": false, "error": {...}}`，stderr 给人话 + 目标路径 |
