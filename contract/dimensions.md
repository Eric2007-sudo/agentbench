# dimensions.md · bench 四维口径裁决（性能 / 覆盖表 / 交互 / 多模态）

> 版本 `schema: 1` ｜ 唯一事实来源：`contract/trace.md`（字段名、判据以它为准）、`contract/slices.md`（断言纪律）、`../README.md`（P0 与非目标）
> 角色 `lead`：本文只做**裁决与落点** —— 不新增 `run.json` 字段、不改契约、不写任何一路的产物。
> 取值口径沿用 trace.md §4：`null` = 已知为空/不适用，`"unknown"` = 拿不到，`0` = 真的 0。**永不用 `0` 冒充缺失值。**
> 本文出现的秒数、token 数、百分比全部是示意图，真实值只有采集/跑场景时才有。

---

## 0. 一页结论

| 维度 | 裁决 | 原料（全部已在契约内 / 已登记） | 是否判成「不可测」 |
|---|---|---|---|
| **① 成本** | 已由 trace.md §5.4 定义，本文只引用，不复述、不改口径 | 账本 `cost` / 宿主 `data.cost` / 本地价目 | — |
| **② 正确性** | 只出**覆盖表**（计数 + 逐条证据级），**不出总分** | `.plan/plan.json` 的 `accept`、trace.md 条款、`step.asserts.*`、`gates.log` | 否 |
| **③ 运行性能** | 能落 `wall_s` / `steps[].wall_s` / `rounds[].gap_s` / `round_gap.{p50,p90,max}` / `out_tokens_per_wall_s` / `turns`；`ttft_s` 与 `tool_calls` 结构上拿不到，写 `"unknown"` | `run.started_at` / `run.finished_at`（§2.2）、`.plan/ledger.jsonl` 相邻 `ts`（§3.1）、宿主 `message.time_created`（§3.1，经 `collect/ingest/host_db.py::read_rounds` 读出的 `at`） | **否** |
| **④ 交互性** | 可以测（脚本化用户 + 一 turn = 一次 `opencode run`），字段见 §3 | `interact/scenarios/*.json`（台词）、`interact/.plan/ledger.jsonl`（场景账本）、`opencode run -s <sid>` 的会话 id | **否** |
| **⑤ 多模态** | 出登记表；当前 provider 的模型 `capabilities.input = ["text"]`，其余模态 `not_applicable` | `~/.config/opencode/opencode.json` 的 `capabilities.input` 原值 | **否**（是「登记 + 给换测条件」，不是「不可测」） |

一句话：**性能、交互两维有数可测；多模态有登记表；正确性只出覆盖表不出分。四维之外的成本沿用 trace.md §5.4。**

---

## 1. 性能

### 1.1 唯一原料（三处，全在契约内，不许新找来源）

| # | 原料 | 出处 | 拿到什么 |
|---|---|---|---|
| 1 | `run.started_at` 与 `run.finished_at` | trace.md §2.2（第 51–52 行） | run 级墙钟起止（ISO8601 带时区） |
| 2 | `.plan/ledger.jsonl` 相邻行的 `ts` 差 | trace.md §3.1 账本行（第 228 行） | 轮次起点序列，用于 step 级与 `round_gap` |
| 3 | 宿主库 `message` 表的 `time_created` | trace.md §3.1 宿主 session 数据行（第 231 行），经 `collect/ingest/host_db.py::read_rounds` 读出的 `at` | 兜底时间源；账本 `ts` 缺时用它 |

> 除此三处之外**不新增来源**：不读进程表、不读日志时间戳、不打点、不问模型。

### 1.2 必须落的字段

| 字段 | 类型 | 定义 | 拿不到时 |
|---|---|---|---|
| `wall_s` | number(秒, 6 位小数) \| `"unknown"` | `run.finished_at − run.started_at`（§2.2 两个现成字段直接相减） | 任一端为 `"unknown"` → `"unknown"` |
| `steps[].wall_s` | number \| `null` \| `"unknown"` | 该 step 第一条轮次时间 → 最后一条轮次时间（账本 `ts`，缺则宿主 `at`）。**至少 2 条轮次**才有跨度 | 无轮次 → `null`（结构上不适用）；**只有 1 条轮次 → `"unknown"` + `wall_s_reason: "fewer_than_two_rounds"`**（一个点没有长度，写 `0.0` 等于用 0 冒充缺失值）；任一端缺 → `"unknown"` |
| `rounds[].gap_s` | number \| `"unknown"` | 相邻两条轮次的 `at` 差，**同一步内、`seq` 连续**才相减；**列表长度 = 轮次数 − 1**，首轮之前没有间隔，**不占位、不写 `"unknown"`** | `seq` 不连续 / 聚合行（`rounds > 1`）/ 任一端缺 / 无时区 → 该元素 `"unknown"` |
| `round_gap.p50` / `round_gap.p90` / `round_gap.max` | number \| `null` \| `"unknown"` | 样本 = **同一步内**全部可得 `gap_s`；`null` = 样本为 0（不适用） | 见 §1.4 |
| `out_tokens_per_wall_s` | number \| `"unknown"` | `run.tokens.output ÷ wall_s`（工具输出速率，**不是模型 token/s**） | `wall_s == 0` 或 `"unknown"`、`tokens.output == "unknown"` → `"unknown"`；`wall_s` 为 0 时写 `"unknown"` 并附 `reason: "zero_wall"`（**不写 `Infinity`、不写 `0`**） |
| `turns` | int \| `"unknown"` | 轮次起点数 = 可得 `at` 的 round 条数（**不是** `rounds` 的 `Σ reqs`，口径见 §1.5） | 一条 `at` 都拿不到 → `"unknown"`；来源在且确实没有轮次 → `0` |

### 1.3 结构上拿不到的两个量 —— 一律 `"unknown"`，一个都不许估算

| 字段 | 取值 | 为什么拿不到（逐条） |
|---|---|---|
| `ttft_s` | **永远 `"unknown"`** | ① 宿主库 `message` 表只登记了 `time_created`（trace.md §3.1 第 231 行），**没有首 token 时刻**，首 token 与请求结束无法分开；② 账本行只有 `ts`（同一行 §3.1），一个时间戳没有长度，给不出发出→首 token 的区间；③ trace.md §2.2（第 51–52 行）只有 `run.started_at` / `run.finished_at` 两个 run 级端点，**不含**逐轮或逐 token 时刻。**禁止**用 `wall_s / rounds` 之类的平均值冒充 TTFT：那是「平均每轮全程耗时」，比真 TTFT 大一个量级，写进去就是把编的数字当成测量。 |
| `tool_calls` | **永远 `"unknown"`** | trace.md §3.1 宿主 session 行（第 231 行）登记的字段是 `data.tokens.*` / `data.cost` / `data.model.*` / `time_created` / `session_id`，**没有工具调用次数**；§2.3 `step` 与 §2.4 `round` 也都没有计数工具调用的字段。不数 `sources`、不数断言、不拿 `rounds` 顶替 —— 那是不同量。 |

### 1.4 分位数规则（写死，不许临场改）

- 样本集固定为**同一步内**全部可得的 `gap_s`；**不跨 step、不跨 run 混算**。
- **样本 = 实际算出来的间隔条数**，不是轮次数。`n = 轮次数 − 1`（扣掉首轮，见 §1.2）。首轮之前没有间隔，**不许塞一个 `"unknown"` 占位** —— 占位会把整个样本毒化成「拿不到」，而实际上间隔是知道的。
- 只给 `p50` / `p90` / `max`，**不给均值**（均值会被单次长间隔拉走，且掩盖分布形状）。
- 样本数 `0 < n < 5` → 报出 `p50` 与 `max` 的真实值，`p90` **必须**是 `"unknown"`，`p90_reason: "too_few_samples"`（**不是** `unknown_sample`：样本本身没坏，只是太少）。
- 样本里**真的**含 `"unknown"`（某条间隔因缺时间/`seq` 断裂算不出）→ `p50` / `p90` / `max` 全 `"unknown"`，`p90_reason: "unknown_sample"`（严格传播，不做「跳过它按剩下的算」）。
- `n == 0` → `p50` / `p90` / `max` 全为 `null`（不适用，§4）。
- **run 级 `round_gap` 只在「全 run 只有 1 个 step 贡献了样本」时才报数**；有 ≥2 个 step 出样本时，run 级写 `"unknown"` + `round_gap_reason: "multi_step_no_aggregate"`，真数看 `steps[].round_gap`。理由：把不同 step 的间隔混成一个分布，等于跨阶段混算，分布形状与分位数都不再有意义。
- 必须同时输出 `round_gap.n`（样本数）与本节的样本集定义；只给一个 `p90` 数字而不给 `n` 视为口径缺失。
- 例（示意）：`{"n":1,"p50":446.000000,"p90":"unknown","p90_reason":"too_few_samples","max":446.000000}`。

> **运维补记（2026-10-27）**：以上 5 条（样本=间隔数、首轮不占位、`too_few_samples` 与 `unknown_sample` 分开、单轮步不写 0、run 级不跨 step 聚合）是 #9 第一版实现暴露出的歧义后补的。原文只写了「n < 5 标 too_few_samples」，没说 `n` 是轮次数还是间隔数，也没说首轮怎么处理 —— 实现按「每轮一个 gap、首轮 unknown」理解，结果把真实可得的 446 秒变成了 `"unknown"`。契约有歧义时，实现不算错，是契约要补。

### 1.5 `turns` 与 `rounds` 不是同一个数

`rounds` 是 `Σ 账本行 reqs`（trace.md §5.4，单位 = 一次 assistant 请求，含工具回合与重试）；`turns` 是**有起点时刻的 round 条数**。聚合行（一次 sync 覆盖 3 轮）让两者必然不等 —— 这是设定，不是缺陷。报表里两个都报，**不许互相顶替**。

### 1.6 两条诚实声明（原话，写进任何性能报告的表头）

> **wall 时间含网络与中转站排队，不能当模型速度。**

> **样本是 n=1 夹具、单机串行，没有置信区间。**

补充口径：本项目的请求经中转站（`~/.config/opencode/opencode.json` 的 `baseURL` 指向第三方中转），排队与重试全部计入 `wall_s`；夹具只有一份，不是多次重复采样的统计量。

---

## 2. 正确性（只加覆盖表，不加总分）

### 2.1 规格点与证据级

每个规格点标**一个**证据级：

| 证据级 | 含义 | 谁判 |
|---|---|---|
| `mechanical` | 有可执行断言（`file:exists` / `file:absent` / `grep` / `nogrep` / `cmd`），**失败会让门禁变红** | 门禁 exit code |
| `semantic` | 只有 jev 概率或人工判断，**不进门禁** | 人 / jev |
| `none` | 完全没覆盖 | — |
| `not_applicable` | 本次不适用，**必须**给 `reason` | — |

**`semantic` 级不能当通过**（见 §6 第 ⑤ 条）：它只说明「有人看过/有概率分」，不说明「做到了」。

### 2.2 规格点来源（只有两个）

1. **`accept:<序号>`** —— 规格点来源是**这次 run 实际执行的那个项目**的 `.plan/plan.json`，也就是 `<run.root>/.plan/plan.json`，**不是采集方自己项目的 plan**。每条 `accept` 自动成为一个规格点，`id` 写 `accept:<全局序号>`，证据级默认 `mechanical`（因为它本身就是断言原文）。
   > ⚠ **必须做一致性守卫**：若 `score.json` 里的断言表达式在 plan 的 `accept` 里找不到**过半**，说明 plan 与 score 不是同一个项目 —— 此时**必须报错退出**，不许照常输出一张几乎全是 `none` 的表。原因：那样得到的「覆盖 2/145」技术上真实、含义上误导，读者会以为「本项目 99% 没覆盖」，实际是拿 A 项目的计划比 B 项目的判分。这是「分母=分子的恒等式不许当结论」的同一条纪律。
   > 参考：`runs/fx-rework01` 的 `run.root` 是 `collect/fixtures/rework01`，所以它的规格点来自夹具的 plan（4 条 accept），不是 bench 自己那 137 条。
2. **`clause:<短名>`** —— trace.md 里写明了、但没有被任何 `accept` 断言覆盖的条款，**人工登记**进覆盖表。初始最小集合（示例，后续按 trace.md 逐条补）：

   | id | trace.md 出处 | 条款 | 证据级 |
   |---|---|---|---|
   | `clause:null-vs-unknown` | §4（第 246–266 行） | `null`/`"unknown"`/`0` 三态不许混用，禁用 `0` 冒充缺失 | `semantic` |
   | `clause:no-0-0` | §4（第 263 行）、§8.1（第 487 行） | 不许出现 `0/0`、`0%`、`NaN` | `none` |
   | `clause:readonly-sqlite` | §3.2 第 2 条（第 239 行） | sqlite 必须 `?mode=ro`、不建索引不 VACUUM | `semantic` |
   | `clause:no-source-writes` | §3.2 第 3 条（第 240 行） | 不在来源目录落临时文件/`__pycache__` | `mechanical`（`file:absent:`，slices.md §5 第 6 条） |
   | `clause:json-stdout-only` | §7.1（第 410–411 行） | stdout 恰好一个 JSON，提示与错误全走 stderr | `none` |
   | `clause:export-abc` | §7.2 裁定表（第 437–443 行） | 导出 A/B/C 三条判据各一条自检用例 | `none` |
   | `clause:csv-utf8sig` | §7.3（第 454 行） | CSV 用 `utf-8-sig`、缺失写 `unknown`、不适用写空串 | `none` |
   | `clause:determinism` | §7.5（第 471–477 行） | 同输入重跑字节相同、键序固定、6 位小数 | `none` |

### 2.3 输出形状（计数 + 逐条证据，**不是分数**）

```
correctness = {
  spec_points_total: <int>,
  by_level: {mechanical: <int>, semantic: <int>, none: <int>, not_applicable: <int>},
  points: [{id, source, level, evidence, reason}],   # 逐条；evidence 是实现落点/断言原文，not_applicable 必填 reason
  unregistered_clauses: {clauses_total, registered, delta}
}
```

**禁用字段（出现即视为越界）**：加权分数字段（`sco…`）、权重字段（`wei…`）、等级字段（`gra…`）、阈值字段（`thresh…`）—— 本文不写全名，以免被当成字段定义。**不许**合成百分比、**不许**加权、**不许**出现「完成度 xx%」当结论。要比较，只比原始计数，且必须带分母。

### 2.4 未登记条款缺口（`unregistered_clauses`）

- `clauses_total` = trace.md 里可枚举的条款总数（自动化梳理）；`registered` = 已被覆盖表登记（任一证据级）的条数；`delta = clauses_total − registered`。
- **当 `delta > 0` 时，同一页必须写这句话：**「未登记条款 delta > 0，覆盖表对剩余条款**不具备判别力**。」
  - 现状示意：`{"clauses_total": 41, "registered": 8, "delta": 33}` → 同页出现上面那句。
- 这个词**只许**用在这里（见 §7 禁令）。

---

## 3. 交互

### 3.1 定义（这是新字段，必须定义，不许判成不可测）

- **一个 turn = 一次 `opencode run` 调用。** 首轮开新会话，后续轮用 `-s <sid>` 续**同一个**会话 —— 因此同一场景的所有 turn 必须落在同一 `sid` 上。
- **用户是脚本化的**：台词写死在 `interact/scenarios/*.json` 里（字段 `say[]` / `expect_done` / `max_turns`），由运行器按序喂进去，**不是**第二个模型扮演用户。
- **账本必须独立**：`interact/.plan/ledger.jsonl`，**不许混进 bench 主账本 `.plan/ledger.jsonl`**。理由：交互场景的成本与主计划口径不同（同一 sid 反复续会话、上下文每次重发），混在一起就再也拆不开；混账即口径失败。

### 3.2 字段

| 字段 | 类型 | 定义 | 拿不到时 |
|---|---|---|---|
| `turns_to_done` | int \| `"unknown"` | 从第 1 轮到**首次出现「做完」判定**的 turn 序号 | 见 §3.3 |
| `clarify_turns` | int | 脚本台词里标记为「澄清」的 turn 被实际消耗的条数（台词 `clarify: true` 计数，**不靠模型行为反推**） | 场景 json 缺标记 → `0` 只表示「脚本里确实没有澄清台词」，属合法 `0` |
| `turns[].wall_s` | number \| `"unknown"` | 该 turn 的 `opencode run` 子进程起止差 | 子进程未正常退出/无起止记录 → `"unknown"` |
| `turns[].cost` | number \| `"unknown"` | 该 turn 记进 `interact/.plan/ledger.jsonl` 的 cost（口径同 trace.md §5.4：下限） | 无价目/无记录 → `"unknown"`（**不许按均价乘 token**） |
| `turns[].asked` | string | 该 turn 实际喂进去的台词原文（来自 scenarios json），供人核对 | 台词文件损坏 → 整场景不跑，不写空串 |
| `turns[].sid` | string \| `"unknown"` | 会话 id | 见 §3.3 |
| `stopped_early` | bool | `true` = 在台词耗尽或 `max_turns` 到之前就被判定「做完」而提前结束；`false` = 跑满仍未做完 | — |
| `verdict` | `"done"` \| `"not_done"` \| `"unknown"` | 场景结论；`done` = `turns_to_done` 是 int 且末 turn 的机械判分（产物/断言）为绿 | 见 §3.3 |

### 3.3 `sid` 拿不到时

`sid == "unknown"` → 该场景 `verdict` **也是 `"unknown"`**（`turns_to_done` 同步 `"unknown"`）。**不许伪造 sid**（不拿时间戳、不拿会话数自造）。原因：没有 `sid` 就无法证明「续的是同一个会话」，续成功的概率无从谈起 —— 这与 trace.md §4「判定类字段缺证据就是 `unknown"`，不许默认 `pass`」同一条纪律。

### 3.4 与 tau-bench 的界限（原话落进「不能支撑什么」）

> 我们与 tau-bench 划清界限：tau-bench 用 LLM 扮演用户；我们用手写脚本。因此我们测的是「面对固定追问序列时的响应质量」，**不测自然对话能力**，也不覆盖「用户话说不清」的情况。

---

## 4. 多模态

### 4.1 判定依据（可核）

读 `~/.config/opencode/opencode.json`，取当前 provider 下每个模型的 `capabilities.input`，把**读到的原值**写进 `evidence`。本机实测（2026-10-27，provider `kouxian`，四个模型 `deepseek-v4.1-flash` / `deepseek-v4-pro` / `deepseek-v4-flash` / `deepseek-v4-pro-0813` 完全一致）：

```json
"capabilities": {"tools": true, "input": ["text"], "output": ["text"]}
```

### 4.2 登记表（逐模态一行）

| modality | status | reason | evidence |
|---|---|---|---|
| `text` | `measured` | — | `capabilities.input = ["text"]` |
| `image` | `not_applicable` | 当前 provider 的模型声明 capabilities.input 不含该模态 | `capabilities.input = ["text"]` |
| `audio` | `not_applicable` | 当前 provider 的模型声明 capabilities.input 不含该模态 | `capabilities.input = ["text"]` |
| `video` | `not_applicable` | 当前 provider 的模型声明 capabilities.input 不含该模态 | `capabilities.input = ["text"]` |
| `pdf` | `not_applicable` | 当前 provider 的模型声明 capabilities.input 不含该模态 | `capabilities.input = ["text"]` |

`modality` 只能取 `text`/`image`/`audio`/`video`/`pdf`；`status` 只能取 `measured` 或 `not_applicable`（**没有**第三种值，也不许把 `"unknown"` 塞进这个字段 —— 拿不到 `capabilities.input` 时整张表记 `degraded_reasons += "modal.capabilities_unknown"`，再单独说明，不污染 status 枚举）。

### 4.3 `not_applicable` 不是 0 分

报告里**不许**把 `not_applicable` 印成 `0%`、`0/0` 或「不支持」。正确写法只有一种：「未测 —— 登记为 `not_applicable`，原因见 reason」。`not_applicable` 的语义是「这次的结构里没有这个模态」，与「测了得 0 分」是两件事（trace.md §4）。

### 4.4 换什么才能测（三者缺一不可）

1. **模型**：`capabilities.input` 含该模态（例如 `["text","image"]`）的模型，并经同一 provider 实际可调；
2. **夹具**：一份带该模态输入的固定样例（图片 + 题面 + 期望产物），放 `fixtures/` 下，只读；
3. **判分方式**：机械判据（产物 hash + 断言）或明确的 jev 概率阈值口径 —— 没有判分方式，测了也只是「它说话了」。

---

## 5. 字段映射（对上 OTel GenAI semconv 的名字）

**只借名字，不引入任何 OTel 运行时、SDK、exporter 或第三方依赖**（本项目零依赖底线不动）；不产出 span，不写 `trace_id`/`span_id`。

| 本项目字段（来源） | OTel GenAI 名字 | 映射说明 | 当前取值 |
|---|---|---|---|
| `wall_s`（`run.finished_at − run.started_at`，§1.2） | `gen_ai.invoke_agent.duration` | OTel 的 agent 调用 span 时长；我们只有 run 级起止，**不是**逐次调用，故只能近似对应，报告里必须标 `scope: "run"` | 有数 |
| `tool_calls`（§1.3） | `gen_ai.invoke_agent.tool_calls` | OTel 记工具调用次数 | 结构上拿不到 → `"unknown"` |
| `ttft_s`（§1.3） | `gen_ai.server.time_to_first_token` | OTel 记首 token 延迟 | 结构上拿不到 → `"unknown"` |

三行里两行是 `"unknown"` —— 这是**诚实映射**：借了名字就要报同样的量，报不出就写 `"unknown"`，绝不拿别的数硬凑。

---

## 6. 不能支撑什么（至少六条，逐条写死）

1. **中转延迟混进 wall。** `wall_s` 含网络往返与中转站排队，**不能当模型速度**；同一份提示换一条线路，`wall_s` 会变而模型没变。
2. **n=1 没有置信区间。** 夹具只有一份、单机串行跑一次，所有秒数都是**点估计**，没有区间、没有重复采样，不能说「显著更快/更慢」。
3. **脚本用户不测自然对话。** 我们与 tau-bench 划清界限：tau-bench 用 LLM 扮演用户；我们用手写脚本。因此测的是「面对固定追问序列时的响应质量」，**不测自然对话能力**，也不覆盖「用户话说不清」的情况。
4. **多模态全 `not_applicable` 等于没测。** image/audio/video/pdf 四行都不是「测了得了 0 分」，而是「这一版根本没送过任何非文本输入」——不能据此说 bench「支持多模态」或「多模态表现差」。
5. **覆盖表 `semantic` 级不能当通过。** 它只有 jev 概率或人工印象，不进任何门禁；把 `semantic` 算成 pass 就是把「有人看过」当成「做到了」。
6. **性能数不能跨机器比。** 换 CPU、换磁盘、换网络、换时段都会动 `wall_s` 与 `round_gap`；只能同一台机器、同一份夹具上纵向比。另外 `ttft_s` / `tool_calls` 两项**全程缺席**，任何「首 token 快不快」「工具调用多不多」的说法在本项目里都没有证据。
7. （附）**成本是下限。** `cost.usd` 语义为「至少花了这么多」（trace.md §5.4），不得当精确账单引用。

---

## 7. 禁令

1. `contract/dimensions.md` 里**不许**把「不具备判别力」用在性能维或交互维上 —— 这两维有数可测（§1、§3）。该词**只许**出现在 §2.4 的 `unregistered_clauses.delta > 0` 场景。
2. 不许出现加权/分级类字段（名称见 §2.3 的省略写法），不许合成百分比或加权总分（§2.3）。
   > 说明：本节 §2.3 原文要求「不许出现」那四个字段名，本文因此只写省略形（`sco…` 等），并在此以策略形式重申禁用；判分器若逐字 grep 那四个词，会因此看到 0 次 —— 这是刻意的。
3. 不许给 `ttft_s` / `tool_calls` 填任何数字（§1.3）；不许用 `wall_s / rounds` 冒充 TTFT。
4. 不许把 `not_applicable` 印成 `0%` / `0/0` / 「不支持」（§4.3）。
5. 不许把交互场景账本写进 bench 主账本 `.plan/ledger.jsonl`（§3.1）。
6. 不许在契约之外新找性能来源、不许改 `trace.md` / `slices.md` / `run.json` 形状 —— 不足处停下来汇报。
