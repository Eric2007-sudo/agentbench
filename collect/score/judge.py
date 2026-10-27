"""collect/score/judge.py —— 把四类机械判分合成一份判分快照（`runs/<run_id>/score.json`）。

输入：`run.json`（路 A 的唯一交接物，`contract/trace.md` §2）+ 可选的基准 `run.json`（产物 diff 用）。
输出：四段 —— `exitcode` / `asserts` / `diff` / `cost`，**每段都带「数」与「口径来源」**。

三条不许破的规矩：

1. **不出现加权总分**：四类不做加权求和，也不出现「我觉得这一步做得不错」这种主观分。
   （所以本文件里没有 `score` / `weight` / `grade` 一类字段。）
2. **缺证据不许默认 pass**：`verdict` 按 `contract/trace.md` §5.5 重算，任何关键字段是
   `"unknown"` 就记 `"unknown"`。
3. **产物是纯函数**：同一份 `run.json` + 同一份代码 → 字节级相同的 `score.json`
   （不写导出时间戳、不写进程 id、键序固定、数组按 step / path 排序、`\\n` 结尾，见 §7.4 / §7.5）。
"""

from __future__ import annotations

import hashlib
import os

from . import asserts as asserts_mod
from . import cost as cost_mod
from . import diff as diff_mod
from . import exitcode as exitcode_mod
from .base import SCHEMA, UNKNOWN, is_number, is_unknown

BASIS = "四类机械判分（contract/trace.md §5）：只认 exit code / 断言 / 产物 diff / token 与 cost，不做加权总分"

VERDICT_RULE = (
    "有任一步 asserts.failed>0 或 exit_code 非 0 → fail；否则存在任何 unknown 关键字段"
    "（tokens/rounds/cost/断言结果）→ unknown；都齐且每步 final_pass_rate==1.0 → pass（trace.md §5.5）"
)


def file_digest(path):
    """(bytes, sha256) —— 只读，流式，不把整份文件吃进内存。"""
    if not path:
        return None, UNKNOWN
    try:
        digest = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
                size += len(chunk)
        return size, digest.hexdigest()
    except OSError:
        return None, UNKNOWN


def _source_block(run_json_path, baseline_meta, run_json_digest) -> dict:
    size, digest = run_json_digest if run_json_digest else (None, UNKNOWN)
    return {
        "run_json": run_json_path,
        "baseline": baseline_meta,
        "readonly": True,
        "bytes": size,
        "sha256": digest,
    }


def _verdict(run_doc, exitcode_section, asserts_section, cost_section) -> dict:
    """按 `contract/trace.md` §5.5 重算总论：缺证据就是 unknown，不许默认 pass。"""
    run = run_doc.get("run") or {}
    reasons = []
    failed_asserts = [entry for entry in asserts_section.get("by_step", [])
                      if is_number(entry.get("failed")) and entry["failed"] > 0]
    nonzero_exit = [entry for entry in exitcode_section.get("by_step", [])
                    if is_number(entry.get("exit_code")) and entry["exit_code"] != 0]

    if failed_asserts:
        reasons.append("asserts.failed:%s" % ",".join(
            "step%s=%d" % (entry["step"], entry["failed"]) for entry in failed_asserts))
    if nonzero_exit:
        reasons.append("exit_code.nonzero:%s" % ",".join(
            "step%s=%s" % (entry["step"], entry["exit_code"]) for entry in nonzero_exit))
    if reasons:
        return {"verdict": "fail", "basis": VERDICT_RULE, "reasons": reasons}

    unknown_keys = []
    tokens = run.get("tokens") or {}
    for field in ("input", "output", "reasoning", "cache_read", "cache_write"):
        if is_unknown(tokens.get(field, UNKNOWN)):
            unknown_keys.append("run.tokens.%s" % field)
    if is_unknown(run.get("rounds", UNKNOWN)):
        unknown_keys.append("run.rounds")
    run_cost = run.get("cost") or {}
    if is_unknown(run_cost.get("usd", UNKNOWN)):
        unknown_keys.append("run.cost.usd")
    if not run_cost.get("complete", False):
        unknown_keys.append("run.cost.complete=false")
    if is_unknown(asserts_section.get("final_pass_rate", UNKNOWN)):
        unknown_keys.append("asserts.final_pass_rate")
    for entry in asserts_section.get("by_step", []):
        if is_unknown(entry.get("final_pass_rate", UNKNOWN)):
            unknown_keys.append("asserts.step%s.final_pass_rate" % entry.get("step"))
        if not is_number(entry.get("failed")):
            unknown_keys.append("asserts.step%s.failed" % entry.get("step"))
    if is_unknown(cost_section.get("rounds", UNKNOWN)):
        unknown_keys.append("cost.rounds")
    if exitcode_section.get("unjudged_count"):
        unknown_keys.append("exitcode.unjudged=%d" % exitcode_section["unjudged_count"])

    if unknown_keys:
        return {"verdict": UNKNOWN, "basis": VERDICT_RULE, "reasons": unknown_keys}

    steps = asserts_section.get("by_step", [])
    if not steps:
        return {"verdict": UNKNOWN, "basis": VERDICT_RULE, "reasons": ["steps:0 没有步骤可判，不许给 pass"]}
    not_full = [entry for entry in steps
                if not (is_number(entry.get("final_pass_rate")) and entry["final_pass_rate"] == 1.0)]
    if not_full:
        return {"verdict": "fail", "basis": VERDICT_RULE,
                "reasons": ["final_pass_rate<1.0:%s" % ",".join(
                    "step%s" % entry["step"] for entry in not_full)]}
    return {"verdict": "pass", "basis": VERDICT_RULE,
            "reasons": ["每步 final_pass_rate==1.0，四类里没有 fail、没有 unknown 关键字段"]}


def judge(run_doc, baseline_doc=None, run_json_path=None, baseline_path=None,
          run_declared=False) -> dict:
    """合成判分快照。`baseline_doc is None` → diff 整段 `"unknown"`（不适用，不是「没变化」）。"""
    if not isinstance(run_doc, dict) or not isinstance(run_doc.get("run"), dict):
        raise ValueError("run.json 形状不对：缺顶层 run 对象")

    run = run_doc["run"]
    exitcode_section = exitcode_mod.score(run_doc, run_declared=run_declared)
    asserts_section = asserts_mod.score(run_doc)
    cost_section = cost_mod.score(run_doc)

    if baseline_doc is None:
        baseline_meta = None
    elif baseline_path and os.path.normcase(os.path.abspath(baseline_path)) == os.path.normcase(
            os.path.abspath(run_json_path or "")):
        baseline_meta = {"path": baseline_path, "run_id": (baseline_doc.get("run") or {}).get("id", UNKNOWN),
                         "mode": "self", "note": "基准就是本次 run.json 自己（夹具自比）"}
    else:
        baseline_meta = {"path": baseline_path,
                         "run_id": (baseline_doc.get("run") or {}).get("id", UNKNOWN),
                         "mode": "given", "note": "上一次运行的 run.json"}

    diff_section = diff_mod.compare(run_doc, baseline_doc, baseline_meta)
    verdict = _verdict(run_doc, exitcode_section, asserts_section, cost_section)

    degraded_reasons = list(run.get("degraded_reasons") or [])
    score_notes = []
    for section in (exitcode_section, asserts_section, cost_section):
        score_notes.extend(section.get("notes") or [])

    document = {
        "schema": SCHEMA,
        "kind": "score",
        "basis": BASIS,
        "source": _source_block(run_json_path, baseline_meta,
                                file_digest(run_json_path) if run_json_path else (None, UNKNOWN)),
        "run_id": run.get("id", UNKNOWN),
        "run_json_verdict": run.get("verdict", UNKNOWN),
        "verdict": verdict["verdict"],
        "verdict_rule": verdict["basis"],
        "verdict_reasons": verdict["reasons"],
        "exitcode": exitcode_section,
        "asserts": asserts_section,
        "diff": diff_section,
        "cost": cost_section,
        "degraded": {
            "run_degraded": bool(run.get("degraded", False)),
            "run_degraded_reasons": degraded_reasons,
            "score_notes": score_notes,
        },
    }
    if run.get("verdict") is not None and run.get("verdict") != verdict["verdict"]:
        document["degraded"]["score_notes"].append(
            "verdict.mismatch:run.json 记 %r，本次按 §5.5 重算得 %r（以重算为准，差异已列进 verdict_reasons）"
            % (run.get("verdict"), verdict["verdict"]))
    return document


def dumps(document: dict) -> str:
    """固定出口格式：`indent=1`、`ensure_ascii=False`、`\\n` 结尾，键序即构造序（§7.1 / §7.4）。"""
    import json

    return json.dumps(document, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
