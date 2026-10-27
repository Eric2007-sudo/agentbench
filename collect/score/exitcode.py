"""collect/score/exitcode.py —— ① exit code 判分（`contract/trace.md` §5.1）。

码从哪来，按优先级，且**必须写进 `exit_code_src`**：

| 优先级 | 来源 | `exit_code_src` |
|---|---|---|
| 1 | 宿主 session 记录里该步命令的退出码 | `"host"` |
| 2 | `gates.log` 里 `cmd:` 断言明细中的 `exit=N`（采集器已归一化进 `run.json` 的 `asserts.items[].detail`） | `"gate"` |
| 3 | 本次判分按用例声明的验收命令执行一次得到的码（**默认关闭**，只有显式 `--run-declared` 才跑；在临时目录里跑，不改来源） | `"score"` |
| 4 | 都没有 | `"unknown"` |

三种结果不许混（`contract/trace.md` §4 / §5.1）：

- `0` → `pass`；非 `0` → `fail`；
- 声明了 `cmd:` 却拿不到码 → `exit_code: "unknown"`、`result: "unjudged"`（既不算过也不算破，**单独计入
  `unjudged`，不从分母里剔掉**）；
- 这一步压根没声明 `cmd:` → `exit_code: null` / `exit_code_src: null` / `result: "not_applicable"`，
  那是「不适用」，**不是失败**。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from .base import UNKNOWN, is_number

#: `gates.log` / `run.json` 明细里退出码的写法。
EXIT_RE = re.compile(r"exit=(-?\d+)")

CMD_PREFIX = "cmd:"

#: 第 3 优先级「真跑一遍」的超时（`contract/slices.md` §5 第 4 条：单条不许超 30 秒）。
DECLARED_TIMEOUT_S = 30

PRIORITY = ["host", "gate", "score", "unknown"]

BASIS = (
    "码来源优先级 host > gates.log:exit=N > score(本次执行，默认关闭) > unknown（trace.md §5.1）；"
    "码来源逐条写进 exit_code_src"
)

RESULT_RULE = "0→pass；非 0→fail；拿不到码→unjudged（不算过也不算破，不从分母剔除）；没声明 cmd→null(不适用)"


def cmd_items(step) -> list:
    """这一步声明了哪些 `cmd:` 断言（来自 `run.json` 的 `asserts.items`，全是声明口径）。"""
    asserts = step.get("asserts") or {}
    items = asserts.get("items") or []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "cmd" or str(item.get("expr", "")).startswith(CMD_PREFIX):
            out.append(item)
    return out


def _gate_codes(items) -> list:
    """从 `cmd:` 条目的 `detail` 里抠 `exit=N`；抠不到的不进结果。"""
    found = []
    for item in items:
        match = EXIT_RE.search(str(item.get("detail", "")))
        if match:
            found.append((item.get("expr", ""), int(match.group(1))))
    return found


def run_declared_commands(expressions, timeout: int = DECLARED_TIMEOUT_S) -> list:
    """第 3 优先级：只跑用例**声明**的验收命令，在临时目录里跑，不改来源。

    这是可选路径（默认不开），因为它会把「纯函数式判分」变成「跑一次看看」——
    只有显式 `--run-declared` 才走到这里。
    """
    results = []
    workdir = tempfile.mkdtemp(prefix="collect-score-declared-")
    try:
        for expr in expressions:
            command = expr[len(CMD_PREFIX):].strip() if expr.startswith(CMD_PREFIX) else expr
            if not command:
                results.append((expr, None, "空命令"))
                continue
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=workdir,
                    timeout=timeout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                results.append((expr, proc.returncode, "临时目录 %s 内执行一次" % os.path.basename(workdir)))
            except subprocess.TimeoutExpired:
                results.append((expr, None, "执行超时（>%ds），码拿不到" % timeout))
            except OSError as exc:
                results.append((expr, None, "无法执行：%s" % exc.__class__.__name__))
    finally:
        try:
            os.rmdir(workdir)
        except OSError:
            pass
    return results


def score_step(step, run_declared: bool = False) -> dict:
    """判一步的 exit code。输入是 `run.json` 里的一个 step。"""
    items = cmd_items(step)
    entry = {
        "step": step.get("n"),
        "title": step.get("title", UNKNOWN),
        "cmd_declared": bool(items),
        "cmd_exprs": [item.get("expr", "") for item in items],
        "cmd_expr": items[0].get("expr", "") if items else None,
    }

    if not items:
        entry.update({
            "exit_code": None,
            "exit_code_src": None,
            "result": "not_applicable",
            "evidence": "plan 里这一步没有 cmd: 断言 → 不适用（null，不是失败）",
        })
        return entry

    # 1) 宿主记录（采集器已经把它归一化进 step.exit_code / exit_code_src）
    ingest_code = step.get("exit_code")
    ingest_src = step.get("exit_code_src")
    if ingest_src == "host" and is_number(ingest_code):
        entry.update({
            "exit_code": int(ingest_code),
            "exit_code_src": "host",
            "result": "pass" if int(ingest_code) == 0 else "fail",
            "evidence": "宿主 session 记录里该步命令的退出码",
        })
        return entry

    # 2) gates.log 的 exit=N（采集器落到 asserts.items[].detail）
    codes = _gate_codes(items)
    if codes:
        code = next((value for _, value in codes if value != 0), codes[0][1])
        expr = next((name for name, value in codes if value == code), codes[0][0])
        entry.update({
            "exit_code": code,
            "exit_code_src": "gate",
            "result": "pass" if code == 0 else "fail",
            "evidence": "gates.log 的 cmd: 明细（%s）里 exit=%d" % (expr, code),
        })
        if len(codes) > 1:
            entry["codes_seen"] = [{"expr": name, "exit": value} for name, value in codes]
        return entry

    # 3) 本次执行（默认关闭）
    if run_declared:
        declared = run_declared_commands([item.get("expr", "") for item in items])
        numeric = [(expr, code, note) for expr, code, note in declared if is_number(code)]
        if numeric:
            expr, code, note = numeric[0]
            entry.update({
                "exit_code": int(code),
                "exit_code_src": "score",
                "result": "pass" if int(code) == 0 else "fail",
                "evidence": "本次判分执行声明的验收命令：%s（%s）" % (expr, note),
            })
            return entry
        timeout_note = "; ".join(note for _, _, note in declared) or "未执行"
        entry.update({
            "exit_code": UNKNOWN,
            "exit_code_src": UNKNOWN,
            "result": "unjudged",
            "evidence": "声明了 cmd: 但本次执行拿不到码（%s）" % timeout_note,
        })
        return entry

    # 4) 都没有
    entry.update({
        "exit_code": UNKNOWN,
        "exit_code_src": UNKNOWN,
        "result": "unjudged",
        "evidence": "声明了 cmd: 但 gates.log 里没有 exit=、宿主也没记，且未开启本次执行 → 拿不到码（未判）",
    })
    return entry


def score(run_doc, run_declared: bool = False) -> dict:
    """整份 `run.json` 的 exit code 段。"""
    steps = (run_doc.get("run") or {}).get("steps") or []
    by_step = [score_step(step, run_declared=run_declared) for step in steps]
    counts = {"pass": 0, "fail": 0, "unjudged": 0, "not_applicable": 0}
    for entry in by_step:
        counts[entry["result"]] = counts.get(entry["result"], 0) + 1
    unjudged = [
        {"step": entry["step"], "expr": entry.get("cmd_expr"), "reason": entry["evidence"]}
        for entry in by_step
        if entry["result"] == "unjudged"
    ]
    return {
        "basis": BASIS,
        "result_rule": RESULT_RULE,
        "priority": PRIORITY,
        "counts": counts,
        "unjudged": unjudged,
        "unjudged_count": len(unjudged),
        "by_step": by_step,
    }
