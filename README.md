# agentbench

**把 agent 的一次运行拆成四个能机械判的维度：花了多少 / 跑了多久 / 覆盖了多少 / 交互了几轮。**
第五项多模态只做登记表 —— 没送过非文本输入就写 `not_applicable`，不写成 0 分。

零依赖（纯标准库）、单机自用、Windows 优先。不给加权总分：四个数并列，各自带分母。

```
                       ┌──────────────────────────────────────┐
                       │  传送带 runs/<run_id>/                │
   原始产物 ──────────▶ │  run.json / measure.json              │
   (.plan/ + 宿主 db)   │  coverage.json / interact.json        │
                       └──────────────────┬───────────────────┘
                                          │ 只读
                                          ▼
                       ┌──────────────────────────────────────┐
                       │  四维并列（不合成总分）                │
                       │  成本 · 性能 · 覆盖 · 交互 · 多模态    │
                       └──────────────────┬───────────────────┘
                                          ▼
                              json / csv / md / 看板
```

---

## 1. 实现了什么

### 1.1 四维 + 一个登记表

| 维度 | 产物 | 谁产出 | 判什么 |
|---|---|---|---|
| **成本** | `runs/<id>/run.json` | `collect/` | 每一轮题面 → tokens / cost / 产物 diff / 断言结果 |
| **性能** | `runs/<id>/measure.json` | `collect/measure/` | `wall_s`、`turns`、轮间隔分位数、输出速率 |
| **正确性** | `runs/<id>/coverage.json` | `collect/coverage/` | 每条规格点标「有机械断言 / 只有语义判断 / 完全没覆盖 / 不适用」 |
| **交互** | `runs/<id>/interact.json` | `interact/` | 脚本化用户场景：几轮做完、反问几次、每轮墙钟与花费 |
| **多模态** | `measure.json` 的 `modalities` | `collect/measure/` | 五个模态逐行登记 `measured` / `not_applicable` |

导出与看板在 `view/`：`out/fx.{json,csv,md}` + `out/board.html` + `out/dimensions.json`。

### 1.2 四个必须说清的口径

**① `unknown` 不是 0，`null` 不是 0。**
拿不到的写 `"unknown"`（宿主库没记首 token 时刻）；结构上不适用的写 `null`（单轮步骤没有跨度）。

**② `ttft_s` 与 `tool_calls` 恒为 `"unknown"`。**
宿主 sqlite 只记 `time_created` 与 token 计数，不记首 token 时刻、不记工具调用次数。
**不估算**，也不用 `wall_s / rounds` 顶替 —— 那是「平均每轮全程耗时」，比真 TTFT 大一个量级。

**③ 分位数只给 `p50` / `p90` / `max`，不给均值。**
样本 < 5 时 `p90` 必须是 `"unknown"` + `p90_reason: "too_few_samples"`；
样本里真的算不出的才叫 `unknown_sample`。两者不混用。

**④ 覆盖表不出百分比、不加权。**
只给「计数 + 分母 + 未登记数」。当 `unregistered_clauses.delta > 0`，
同页必须写「不具备判别力」—— 这句话不能省。

### 1.3 不是什么

- **不是 agent runtime**：不派发任务、不改宿主数据，只读 `.plan/` 与宿主 sqlite。
- **不是模型评测榜**：`wall_s` 含网络与中转排队，n=1、单机串行，**没有置信区间**。
- **不给总分**：四维并列。要比较只比原始计数，且必须带分母。
- **多模态这一版等于没测**：四个模态全 `not_applicable`。

---

## 2. 如何使用

### 2.1 环境

- Python 3.8+，零第三方依赖
- 需要 [tollgate](https://github.com/Eric2007-sudo/tollgate) 提供 `.plan/` 计划格式与 `tg gate`（可选：单独用 `collect/` 与 `view/` 也能跑，只是没有门禁那一层）

```bash
git clone https://github.com/Eric2007-sudo/agentbench.git
cd agentbench
python -m collect.measure --self-test      # 任一模块都能自检
```

### 2.2 从夹具跑一遍完整链

仓库自带一份夹具（`collect/fixtures/rework01`，一份假目标目录的一次运行：有返工 + 坏行 + unknown）。
四步生成全部产物：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'

# 1) 采集：读夹具的 .plan/ 与宿主 db，归一化成 run.json
python -m collect.ingest.plan_dir --root collect/fixtures/rework01 --out runs/fx-rework01/run.json

# 2) 判分：四类判据（exit code / 断言 / 产物 diff / 成本轮次）
python -m collect.score --run runs/fx-rework01/run.json --out runs/fx-rework01/score.json

# 3) 性能 + 模态登记
python -m collect.measure --run runs/fx-rework01/run.json --out runs/fx-rework01/measure.json

# 4) 覆盖表（plan 必须是 run.root 那个项目的）
python -m collect.coverage --run runs/fx-rework01/run.json `
  --score runs/fx-rework01/score.json `
  --plan collect/fixtures/rework01/.plan/plan.json `
  --out runs/fx-rework01/coverage.json
```

导出与看板：

```powershell
python -m view.cli report --run runs/fx-rework01/run.json --json
python -m view.board --run runs/fx-rework01/run.json --out out/board.html
```

交互维度要真调模型（每轮一次 `opencode run`）：

```powershell
python -m interact.driver --scenario clarify --dry-run     # 不花钱，只看会执行什么
python -m interact.driver --scenario clarify --max-usd 0.05 --model <provider/model>
```

### 2.3 接自己的项目

`run.json` 的采集只认两样东西：目标目录的 `.plan/`（计划 + 账本 + 门禁日志）与宿主 sqlite。
所以把 `--root` 指向任何用 tollgate 跑过的项目即可：

```powershell
python -m collect.ingest.plan_dir --root D:\myproj --out runs\myproj\run.json
```

字段级口径见 `contract/trace.md`（唯一事实来源）、四维口径见 `contract/dimensions.md`、步骤与断言纪律见 `contract/slices.md`。

### 2.4 机器可读出口

`collect.*` 与 `view.*` 都支持 `--json`：stdout 恰好一个 JSON，人话全走 stderr。
出错时 stdout 仍是 `{"ok": false, "error": {...}}` 且非 0 退出。

---

## 3. 实测结果

以下全部来自 `runs/fx-rework01`（夹具）与本项目自身的账本，没有估算。

### 3.1 性能（夹具 `fx-rework01`）

| 字段 | 值 | 说明 |
|---|---|---|
| `wall_s` | **1006.000000** | `run.finished_at − run.started_at`，含网络与中转排队 |
| `turns` | **3** | 有起点时刻的轮次（≠ 断言数 ≠ 工具调用数） |
| `out_tokens_per_wall_s` | **2.982107** | 3000 / 1006，**工具输出速率，不是模型 token/s** |
| `round_gap` | `{n:1, p50:446.0, max:446.0, p90:"unknown", p90_reason:"too_few_samples"}` | 首轮之前没有间隔，不占位 |
| `ttft_s` | `"unknown"` | 宿主不记首 token 时刻 |
| `tool_calls` | `"unknown"` | 宿主不记工具调用数 |

单轮步骤（step 2）的 `wall_s` 是 `"unknown"` + `fewer_than_two_rounds`，**不是 0** —— 一个时间点没有长度。

### 3.2 覆盖表

```
counts = {mechanical: 4, semantic: 2, none: 6, not_applicable: 0, total: 12}
unregistered_clauses = {clauses_total: 43, registered: 8, delta: 35}
discriminating_power = "不具备判别力"
```

规格点 = `run.root` 那个项目的 4 条 `accept` + 8 条人工条款。
**`delta = 35 > 0`，所以覆盖表对剩余条款不具备判别力** —— 这句话必须和数字一起出现。

### 3.3 交互（真跑，非 dry-run）

| 场景 | `turns_to_done` | `verdict` | 成本 USD |
|---|---|---|---|
| clarify | 2 | done | 0.003213042 |
| correct | 2 | done | 0.001432434 |
| confirm | 3 | done | 0.001432968 |

三个会话 id 全部能在宿主库 `session_v2` 查到，token 逐项对得上
（例：clarify `input 8795 = 7850 + 945`）。

**方法瑕疵（必须说）**：`clarify` 跑了两遍。第一遍模型索要信息但通篇没用问号，
旧判据「只认问号」判它没反问；于是把判据放宽成「问号 **或** 明确索要信息的短语」后重跑。
两遍成本都留在账本里（合计 $0.007819）没有抹掉，但 `clarify_turns=1` 是在**放宽后的尺子**下得出的。

### 3.4 多模态

| modality | status |
|---|---|
| text | measured |
| image / audio / video / pdf | not_applicable |

`evidence = capabilities.input = ["text"]`，读自 `~/.config/opencode/opencode.json`。
四个 `not_applicable` **等于没测**，不是「测了得 0 分」。

### 3.5 这个项目自己花了多少

| 账 | 步骤 | provider | 金额 |
|---|---|---|---|
| 主账本 `.plan/ledger.jsonl` | #1–#8 | 中转 | **$0.432160** |
| 主账本 `.plan/ledger.jsonl` | #9–#12 | 直连 | **$0.261842** |
| 主账本合计 | #1–#12 | — | **$0.694001** |
| 交互独立账本 `interact/.plan/ledger.jsonl` | 场景 10 轮 | 直连 | **$0.010685** |
| **实际总花费** | | | **$0.704686** |

上限 $3.00，占用 23.5%。

**分两段报的原因**：第 9 步开跑时中转站整组不可用（418 / 503 / 403），
切到直连续完 #9–#12。两段单价不同，所以**不给「平均每步成本」**，也不说「后半段变快/变慢」——
provider 同时变了，速度差异分不清是模型、线路还是排队。

**token 预算**：实际 **2476.9k** / 预算 **978k** = **253%**。不是功能变贵，是预算按「一次成型」估轮数、没算返工。

### 3.6 验证强度

- 12 步门禁全过，`tg reconcile` 重跑 12 步**无回归**
- 三个独立验证器另写（子会话不许改）：`verify_bench9.py` 43 项、`verify_bench10.py` 19 项、`verify_bench12.py` 59 项，
  合计 **121 项断言，0 FAIL / 0 SKIP**

**`reconcile` 抓到过一次门禁和独立验证器都没抓到的问题**：第 12 步为了塞四维，
把 `out/fx.json` 从「`run.json` 的逐字节副本」改成带附加字段 —— 单步门禁全绿、验证器全绿，
只有重跑历史断言才红。修法是把四维挪到 `out/dimensions.json`，保住原不变式。

---

## 4. 参考资料

### 4.1 机制来源

| 机制 | 来源 | 改动 |
|---|---|---|
| 计划 / 门禁 / 账本 / 产物 diff | [tollgate](https://github.com/Eric2007-sudo/tollgate) | 本项目拿它当采集来源与判分依据，不重复实现 |
| 逐轮 tokens / cost | OpenCode 本地 sqlite（`session_v2` / `session_message`） | 只读（`?mode=ro`），不建索引、不写回 |
| 多轮交互的用户侧 | [tau-bench](https://github.com/sierra-research/tau-bench) | **改成脚本化用户**（写死台词），不是 LLM 扮演；因此不测自然对话能力 |
| 多轮 + model-graded 评测框架 | [inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) | 只借「多轮 + 可判分」的结构，不引依赖 |
| 指令 + 测试脚本 + oracle | Terminal-Bench | 断言写成 shell 命令，`exit code` 说话 |
| 字段命名 | OpenTelemetry GenAI semantic conventions | **只借名字**（`gen_ai.invoke_agent.duration` 等），不引 OTel 运行时 |
| 覆盖表的四级证据 | 本项目提出 | `mechanical` / `semantic` / `none` / `not_applicable`，不出百分比 |

### 4.2 深入文档

| 文件 | 内容 |
|---|---|
| `contract/trace.md` | `run.json` 字段级契约（唯一事实来源） |
| `contract/dimensions.md` | 四维口径裁决：性能字段、覆盖表四级、交互定义、模态登记 |
| `contract/slices.md` | 步骤切分与断言纪律 |
| `README.md` | 本文件 |

### 4.3 版本库收什么

只收代码、契约、文档与夹具。`.plan/`、`.orch/`（账本与编排状态）、`runs/`、`out/`（运行产物）
一律不进 —— 它们是**这台机器上的历史**，换台机器没有意义。
想复现上面那些数字，按 §2.2 的四步命令自己跑一遍。

---

## 5. 设计约束（不破）

1. **零依赖**：纯标准库，Python 3.8+。
2. **只读**：采集只读 `.plan/` 与宿主 sqlite，不写别人的目录。
3. **不编数**：拿不到写 `"unknown"`，不适用写 `null`，`0` 只表示真的 0。
4. **四维并列**：不给加权总分；要排名只比原始计数，且必须带分母。

## License

MIT
