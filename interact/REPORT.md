# interact/ · 多轮交互驱动 + 多模态登记

真跑日期 2026-10-27，provider 已从 `kouxian` 切到 `deepseek` 直连，模型 `deepseek/deepseek-flash`（命令里**显式**带 `--model`）。
**台词是编的，不代表真实用户**：全部来自 `interact/scenarios/*.json`，由脚本按序喂入，不由模型生成、不由模型扮演用户。

## 1. 驱动怎么做的

- `interact/driver.py`：一个 turn = 一次 `opencode run --format json --model deepseek/deepseek-flash`。
  第 1 轮开新会话（`--title interact-<id>`），后续轮 `--session <sid>` 续**同一个**会话；`sid` 只从事件流的 `sessionID` 取，
  取不到写 `"unknown"`（不拿时间戳、不拿会话数自造），该场景 `verdict` / `turns_to_done` 同步 `"unknown"`。
- 每轮落 `{turn, asked, wall_s, tokens, cost, sid, response, asked_back, error}` 到 `interact/out/<id>.json`：
  `asked` 逐字等于台词原文；`wall_s` = 子进程墙钟（6 位小数）；`tokens`/`cost` = 宿主库 `session_v2` 的**累计增量**（`?mode=ro` 只读），
  拿不到写 `"unknown"`，**不按均价乘 token**。
- `done` 的机械判分：`checks`（`file:exists` / `grep` / `nogrep`）+ 各轮 `expect_claim` 子串比对 + `done_after_turn` 之前不判 done。
  断言前清空沙箱 `interact/out/<id>/work/`，保证判的是本次产物、不是上一轮残留。
- `--dry-run`：不调模型，只打印将执行的命令与场景校验结果，exit 0（已跑，exit=0）。
- `--self-test`：11 条用例全过（exit 0）——场景解析 / 空台词拒绝 / 免责声明必需 / 预算累计刹车 / sid 缺失→unknown /
  `asked` 逐字 / 断言前缀判定 / cost 缺失→unknown / 无 `--force` / 命令含显式 `--model` 与 `--session` / 传送带形状。
- `--max-usd` 是**累计口径**（含本场景此前已花的）：`已花 >= 上限` 就不派下一轮，并写 `stopped_early: true`；**没有 `--force`**。

## 2. 真实运行（三场景全跑，不是 dry-run）

| 场景 | turn | 墙钟 s | 花费 $ | 反问（机械判据） |
|---|---|---|---|---|
| clarify | 1 | 31.790883 | 0.002376756 | **是** |
| clarify | 2 | 14.027090 | 0.000836286 | 否 |
| correct | 1 | 5.636552 | 0.001113762 | 否 |
| correct | 2 | 7.968633 | 0.000318672 | 否 |
| confirm | 1 | 9.156104 | 0.001214658 | 否 |
| confirm | 2 | 3.851114 | 0.000161646 | 否 |
| confirm | 3 | 2.680142 | 0.000056664 | 否 |

- **clarify**（3 轮台词，实跑 2 轮）：首轮台词故意不说清（「我那个小脚本又跑不起来了」），模型**没有动手编文件**，而是列出它核实到的情况
  并索要「脚本路径 / 怎么运行的 / 报错原文」，句中出现问号 → 记为反问。第 2 轮给出 `calc.py` 后它建好文件并通过三条断言，
  `turns_to_done = 2`、`clarify_turns = 1`、`stopped_early = true`（后两轮台词未消耗）。
  18:54 那次试探跑里它同样在索要信息，但通篇没用问号，旧判据（只认问号）漏判，于是把判据放宽为「问号或明确索要信息短语」后重跑；那一跑的 3 行成本仍在账本里，见 §3。
- **correct**（3 轮台词，实跑 2 轮）：第 1 轮写入 `version=1`；第 2 轮改需求后文件唯一一行变成 `version=2`，
  `grep:version=2` 通过、`nogrep:version=1` 通过（旧值没留下），`turns_to_done = 2`。
- **confirm**（4 轮台词，实跑 3 轮）：第 2、3 轮两次被要求复述 `notes.md` 那行原文，两次都返回「结论：样本 n=3。」，
  与文件内容一致、`expect_claim` 命中，没有编一个新结论；`turns_to_done = 3`（`done_after_turn=3`），第 4 轮未消耗。

三个场景 `verdict` 均为 `done`，明细在 `interact/out/{clarify,correct,confirm}.json`；
汇总落在传送带 `runs/fx-rework01/interact.json`（`schema: 1` + `scenarios[]` + `cost` + `provider_changed`）。

## 3. 账本（单独立账）

- `interact/.plan/ledger.jsonl`：10 行，一行一个 turn，字段 `{ts, scenario, turn, sid, tokens, cost, src: "host_db"}`，
  `cost` 取宿主库 `session_v2.cost` 增量。合计 **$0.010684842**。
- 多出的 3 行是 clarify 的第一次试探跑（18:54，因反问判据从「只认问号」放宽为「问号或明确索要信息短语」而重跑）；
  重跑结果覆盖了 `interact/out/clarify.json`，旧行的成本仍留在账本里，没有被抹掉。
- `runs/fx-rework01/interact.json` 的 `cost = 0.006078444` 只算本次三个场景的 turn 之和；与账本总额的差就是上面那 3 行。
  另有一次早期手工冒烟调用（`opencode run` 回「收到」，约 $0.00064）不在账本内，据此声明，不计入任何汇总。
- **没有**调 `tg sync`，**没有**写 bench 主账本 `.plan/ledger.jsonl`（混账即口径失败，见 `contract/dimensions.md` §3.1）。

## 4. 多模态登记（只调用、不改口径）

`python -m collect.measure --modalities --out interact/modalities.json` 生成（exit 0）：`text = measured`；
`image` / `audio` / `video` / `pdf` 四条全部 `not_applicable`，逐条 reason = 「当前 provider 的模型声明 capabilities.input 不含该模态」，
evidence = `capabilities.input = ["text"]`。这是「这一版没送过非文本输入」，**不是** 0 分，也不是「不支持」；
要换测需同时满足模型 / 夹具 / 判分方式三者（见 `modalities.json` 的 `to_measure`）。

## 5. 不能支撑什么

脚本用户不测自然对话（我们与 tau-bench 划清界限）：测的是「面对固定追问序列时的响应质量」，不覆盖用户话说不清的情况。
n=1 单机串行，所有秒数都是点估计、没有置信区间；`wall_s` 含网络与中转排队，不能当模型速度。成本来自宿主记录，是**下限**不是精确账单。

## 6. 边界

只写了 `interact/**` 与 `runs/fx-rework01/interact.json`；没碰 `collect/`、`view/`、`contract/`、`.plan/`、`.orch/`。偏离契约：无。
两处实现口径登记在案：① `done` 用产物断言 + `expect_claim` 判，`done_after_turn` 之前不判；② `stopped_early` 按字面「台词没跑完就结束」，
本次三次运行都是提前判定 done 结束，故均为 `true`。
