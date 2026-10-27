"""collect/score —— 路 A 的判分层：四类机械判分（`contract/trace.md` §5）。

四类，各出一份「数」+「口径来源」，**不做加权总分**：

| 段 | 判什么 | 允许的结论 |
|---|---|---|
| `exitcode` | 这一步声明的命令退出码（host > gate > score > unknown） | pass / fail / unjudged / not_applicable |
| `asserts`  | 断言通过率（分母=plan 的 accept 条数，声明口径；不做阈值判定） | 两个比率 + 计数 |
| `diff`     | 产物 diff（按路径对齐，只看 sha256，不看 mtime） | added / removed / modified / unchanged / unknown |
| `cost`     | 成本（下限口径）与轮次（Σ reqs） | 数 + complete / unknown_rows / is_lower_bound |

子模块：
- `base.py`      共用原语：`"unknown"` 字面量、固定容差、比率与求和的传播规则
- `exitcode.py`  ① exit code
- `asserts.py`   ② 断言通过率
- `diff.py`      ③ 产物 diff
- `cost.py`      ④ 成本与轮次
- `judge.py`     合成 `score.json`，并按 §5.5 重算 `verdict`
- `verify.py`    `--verify`：拿 `fx-rework01` 的钉死期望逐条核 `score.json`
- `selftest.py`  `--self-test`：造退化场景，一条一条 PASS/FAIL

出口（`python -m collect.score`）：

```
python -m collect.score --self-test
python -m collect.score --verify runs/fx-rework01/score.json
python -m collect.score --run runs/fx-rework01/run.json --baseline runs/fx-rework01/run.json \
                        --out runs/fx-rework01/score.json
```

只读：本包不写 `.plan/`、不写夹具、不开可写 sqlite；唯一写盘动作是 `--out` 指定的 `score.json`。
"""

import sys

#: 判分时不许在来源目录里留 `__pycache__`（`contract/trace.md` §3.2 第 3 条）。
sys.dont_write_bytecode = True
