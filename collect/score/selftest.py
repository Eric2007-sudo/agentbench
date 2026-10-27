"""collect/score/selftest.py —— `python -m collect.score --self-test`。

不跑真数据：这里**造**出退化场景，一条一条打 `PASS` / `FAIL`，最后打总条数。
每条罚的都是同一个毛病：**把「不知道」说成 `0`**（或者说成 `0/0`、`0%`、`0 新增 0 删除`、`-1`、`NaN`）。

场景清单（对应 `.plan/plan.json` 步 4 的派发要求，以及 `contract/trace.md` §4 / §5）：

1. 账本 `rows_with_reqs = 0` 而 `rows_total > 0` → `rounds: "unknown"`，不许报 0 轮
2. 断言 0 条 → 通过率 `null`，不许报 0/0 或 0%
3. 基准缺失 → diff 整段 `"unknown"`，不许报「0 新增 0 删除」
4. 两侧 `cost.usd` 都是 `"unknown"` → 不许相减得 delta
5. 声明了 `cmd:` 但 `gates.log` 里没有 `exit=` → `exit_code: "unknown"` 且计入 `unjudged`
6. 压根没声明 `cmd:` → `exit_code: null` / `not_applicable`（不适用，不是失败，也不算 unjudged）
7. 浮点容差写死：`|a-b| <= max(1e-9, 1e-6*max(|a|,|b|))`
8. `ack:` 未确认：计入分母、不计入分子（不许因为要人工就剔掉）
9. `cmd:` 超时与 `fail` 分开报
10. 没有价目就不许算钱：一行 cost 都拿不到时 `usd: "unknown"`，不是 0
11. 文本 `diff_stat`：`\\n` 归一化行尾、忽略末尾空行
12. 同一份输入判两次 → 字节级相同（纯函数）
13. 产物里没有 -1 / NaN / "nan" 冒充缺失值
14. `verdict` 三条分支：非 0 退出码 → fail；全齐 → pass；有 unknown 关键字段 → unknown
"""

from __future__ import annotations

import json

from . import asserts as asserts_mod
from . import cost as cost_mod
from . import diff as diff_mod
from . import exitcode as exitcode_mod
from .base import UNKNOWN, find_fake_missing, is_number, is_unknown, numbers_close
from .judge import dumps, judge

CLEAN_SHA = "aa" * 32


def _clean_run(**overrides) -> dict:
    """一份「四类都齐、没有一处 unknown」的最小 run.json（可逐项覆盖）。"""
    step = {
        "n": 1,
        "title": "自检步",
        "lane": "collect",
        "status": "done",
        "planned_out": "a.txt",
        "artifacts": [{"path": "a.txt", "exists": True, "bytes": 3, "sha256": CLEAN_SHA}],
        "asserts": {
            "total": 1, "passed": 1, "failed": 0, "timeout": 0, "unacked": 0,
            "items": [{"expr": "file:exists:a.txt", "kind": "exists", "result": "pass", "detail": ""}],
        },
        "assert_pass": {"first_pass_rate": 1.0, "final_pass_rate": 1.0},
        "exit_code": None,
        "exit_code_src": None,
        "rework": 0,
        "first_failed": False,
        "rounds": 1,
        "tokens": {"input": 100, "output": 10, "reasoning": 0, "cache_read": 0,
                   "cache_write": 0, "complete": True},
        "cost": {"usd": 0.001, "complete": True, "basis": "host", "unknown_rows": 0},
        "rounds_detail": [{
            "seq": 1, "at": "2026-01-01T00:10:00+08:00", "input": 100, "output": 10,
            "reasoning": 0, "cache_read": 0, "cache_write": 0,
            "cost": {"usd": 0.001, "est": False}, "rounds": 1,
            "note": "", "decision": None, "src": "ledger",
        }],
    }
    document = {
        "schema": 1,
        "run": {
            "id": "selftest-20260101T000000",
            "root": ".",
            "started_at": "2026-01-01T00:00:00+08:00",
            "finished_at": "2026-01-01T00:10:00+08:00",
            "goal": "判分器自检（示意数据）",
            "model": {"provider": "selftest", "id": "selftest", "mixed": False},
            "rounds": 1,
            "rounds_coverage": {"rows_total": 1, "rows_with_reqs": 1},
            "tokens": {"input": 100, "output": 10, "reasoning": 0, "cache_read": 0,
                       "cache_write": 0, "complete": True},
            "cost": {"usd": 0.001, "complete": True, "basis": "host", "unknown_rows": 0},
            "verdict": "pass",
            "sources": [],
            "degraded": False,
            "degraded_reasons": [],
            "steps": [step],
        },
    }
    run = document["run"]
    for key, value in overrides.items():
        if key in ("tokens", "cost", "rounds", "rounds_coverage", "verdict"):
            run[key] = value
        elif key == "step":
            run["steps"] = [value]
        else:
            raise KeyError(key)
    return document


# --- 场景 ------------------------------------------------------------------

def case_rounds_unknown_without_reqs():
    document = _clean_run(rounds="unknown",
                          rounds_coverage={"rows_total": 2, "rows_with_reqs": 0})
    section = cost_mod.score(document)
    if section["rounds"] != UNKNOWN:
        return False, "rows_with_reqs=0 且 rows_total>0 时应为 unknown，实际 %r" % (section["rounds"],)
    if "rows_with_reqs=0" not in " ".join(section["notes"]):
        return False, "没有说明为什么是 unknown：%r" % (section["notes"],)
    return True, "rows_with_reqs=0 / rows_total=2 → rounds==\"unknown\"（没报 0 轮）"


def case_rate_null_without_asserts():
    step = dict(_clean_run()["run"]["steps"][0])
    step["asserts"] = {"total": None, "passed": UNKNOWN, "failed": UNKNOWN,
                       "timeout": UNKNOWN, "unacked": UNKNOWN, "items": []}
    step["assert_pass"] = {"first_pass_rate": UNKNOWN, "final_pass_rate": UNKNOWN}
    section = asserts_mod.score(_clean_run(step=step))
    entry = section["by_step"][0]
    if entry["final_pass_rate"] is not None:
        return False, "没有断言时应为 null，实际 %r" % (entry["final_pass_rate"],)
    if section["final_pass_rate"] is not None:
        return False, "整 run 通过率也应为 null，实际 %r" % (section["final_pass_rate"],)
    step["asserts"] = {"total": 0, "passed": 0, "failed": 0, "timeout": 0, "unacked": 0, "items": []}
    entry = asserts_mod.score(_clean_run(step=step))["by_step"][0]
    if entry["final_pass_rate"] is not None:
        return False, "total=0 时不许出现 0/0，实际 %r" % (entry["final_pass_rate"],)
    return True, "0 条断言 / total=0 → 通过率 null（没有 0/0、没有 0%）"


def case_diff_unknown_without_baseline():
    section = judge(_clean_run())["diff"]
    if section["status"] != "unknown":
        return False, "基准缺失时整段应为 unknown，实际 %r" % (section["status"],)
    zeros = [key for key, value in (section["summary"] or {}).items()
             if is_number(value) and value == 0]
    if zeros:
        return False, "基准缺失时不许报 0：%r" % (zeros,)
    return True, "基准缺失 → diff 整段 unknown，summary 全 unknown（没有「0 新增 0 删除」）"


def case_cost_no_delta_when_unknown():
    result = cost_mod.compare_cost({"usd": UNKNOWN, "complete": False},
                                   {"usd": UNKNOWN, "complete": False})
    if not is_unknown(result["delta_usd"]) or not is_unknown(result["delta_pct"]):
        return False, "两侧 unknown 时不许相减：%r" % (result["delta_usd"],)
    if result["direction"] != UNKNOWN:
        return False, "方向也应为 unknown，实际 %r" % (result["direction"],)
    if result["is_lower_bound"] is not True:
        return False, "complete:false 时应标 is_lower_bound"
    numbers = cost_mod.compare_cost({"usd": 0.01, "complete": True}, {"usd": 0.02, "complete": True})
    if not is_number(numbers["delta_usd"]) or not numbers_close(numbers["delta_usd"], 0.01):
        return False, "两侧都是真数时应给 delta，实际 %r" % (numbers["delta_usd"],)
    return True, "两侧 unknown → delta 双双 unknown；两侧真数 → delta=0.01"


def case_exit_unknown_counts_as_unjudged():
    step = dict(_clean_run()["run"]["steps"][0])
    step["asserts"] = {
        "total": 1, "passed": 1, "failed": 0, "timeout": 0, "unacked": 0,
        "items": [{"expr": "cmd:python -m x --self-test", "kind": "cmd",
                   "result": "pass", "detail": "没有码"}],
    }
    section = exitcode_mod.score({"run": {"steps": [step]}})
    entry = section["by_step"][0]
    if entry["exit_code"] != UNKNOWN or entry["exit_code_src"] != UNKNOWN:
        return False, "拿不到码时应为 unknown，实际 %r/%r" % (entry["exit_code"], entry["exit_code_src"])
    if entry["result"] != "unjudged":
        return False, "应为 unjudged（不算过也不算破），实际 %r" % (entry["result"],)
    if section["unjudged_count"] != 1 or len(section["unjudged"]) != 1:
        return False, "应计入 unjudged，实际 %r" % (section["unjudged_count"],)
    return True, "声明了 cmd 但没有 exit= → exit_code unknown + unjudged=1"


def case_exit_null_when_no_cmd():
    step = dict(_clean_run()["run"]["steps"][0])
    section = exitcode_mod.score({"run": {"steps": [step]}})
    entry = section["by_step"][0]
    if entry["exit_code"] is not None or entry["exit_code_src"] is not None:
        return False, "没有 cmd: 断言时应为 null，实际 %r" % (entry["exit_code"],)
    if entry["result"] != "not_applicable":
        return False, "应为 not_applicable（不适用），实际 %r" % (entry["result"],)
    if section["unjudged_count"] != 0:
        return False, "不适用不许算 unjudged，实际 %r" % (section["unjudged_count"],)
    return True, "没有声明 cmd → exit_code null / not_applicable（不是 fail，也不是 unjudged）"


def case_tolerance_is_pinned():
    samples = [
        (0.0412, 0.0412 + 1e-11, True, "绝对容差 1e-9 内算相等"),
        (0.0412, 0.0414, False, "0.0002 的差远超容差"),
        (100.0, 100.00005, True, "相对容差 1e-6*100=1e-4 内算相等"),
        (1e-9, 3e-9, False, "差 2e-9 > 绝对容差 1e-9"),
    ]
    for a, b, expected, label in samples:
        if numbers_close(a, b) is not expected:
            return False, "%s：numbers_close(%r, %r) 应为 %r" % (label, a, b, expected)
    if numbers_close(UNKNOWN, 0.0412):
        return False, "unknown 不许参与数值比较"
    return True, "容差按 max(1e-9, 1e-6*max(|a|,|b|)) 走，四条样例全对"


def case_ack_unacked_in_denominator():
    step = dict(_clean_run()["run"]["steps"][0])
    step["asserts"] = {
        "total": 2, "passed": 1, "failed": 0, "timeout": 0, "unacked": 1,
        "items": [
            {"expr": "file:exists:a.txt", "kind": "exists", "result": "pass", "detail": ""},
            {"expr": "ack:看板截图有人看过", "kind": "ack", "result": "unacked", "detail": ""},
        ],
    }
    entry = asserts_mod.score(_clean_run(step=step))["by_step"][0]
    if entry["total"] != 2:
        return False, "ack 不许被剔出分母，实际 total=%r" % (entry["total"],)
    if entry["passed"] != 1 or entry["unacked"] != 1:
        return False, "ack 未确认不许算分子，实际 passed=%r unacked=%r" % (entry["passed"], entry["unacked"])
    if not numbers_close(entry["final_pass_rate"], 0.5):
        return False, "通过率应为 0.5，实际 %r" % (entry["final_pass_rate"],)
    return True, "ack 未确认：分母 2、分子 1、通过率 0.5（没虚高成 1.0）"


def case_timeout_separate_from_fail():
    step = dict(_clean_run()["run"]["steps"][0])
    step["asserts"] = {
        "total": 2, "passed": 1, "failed": 0, "timeout": 1, "unacked": 0,
        "items": [
            {"expr": "file:exists:a.txt", "kind": "exists", "result": "pass", "detail": ""},
            {"expr": "cmd:python -m x --self-test", "kind": "cmd", "result": "timeout", "detail": ">30s"},
        ],
    }
    entry = asserts_mod.score(_clean_run(step=step))["by_step"][0]
    if entry["timeout"] != 1 or entry["failed"] != 0:
        return False, "超时应单列，实际 timeout=%r failed=%r" % (entry["timeout"], entry["failed"])
    if not numbers_close(entry["final_pass_rate"], 0.5):
        return False, "超时计入分母不计入分子，实际 %r" % (entry["final_pass_rate"],)
    return True, "cmd 超时 → timeout=1 / failed=0，通过率 0.5"


def case_no_money_without_price_table():
    step = dict(_clean_run()["run"]["steps"][0])
    step["cost"] = {"usd": UNKNOWN, "complete": False, "basis": UNKNOWN, "unknown_rows": 1}
    step["rounds_detail"] = [dict(step["rounds_detail"][0], cost={"usd": UNKNOWN, "est": False})]
    section = cost_mod.score(_clean_run(step=step,
                                        cost={"usd": UNKNOWN, "complete": False,
                                              "basis": UNKNOWN, "unknown_rows": 1}))
    if section["usd"] != UNKNOWN:
        return False, "一行 cost 都拿不到时 usd 应为 unknown（不是 0），实际 %r" % (section["usd"],)
    if section["pricing"]["table"] != "none":
        return False, "自检不该有任何价目表，实际 %r" % (section["pricing"]["table"],)
    if section["pricing"]["estimated_rows"] != 0:
        return False, "不许自己估算，实际 estimated_rows=%r" % (section["pricing"]["estimated_rows"],)
    if section["is_lower_bound"]:
        return False, "usd 是 unknown 时不应标「有下限」"
    return True, "没有价目 → usd unknown、estimated_rows=0（一次乘法都没做）"


def case_text_diff_stat():
    stat = diff_mod.text_diff_stat("a\r\nb\r\n\r\n", "a\nc\n")
    if stat != {"added_lines": 1, "removed_lines": 1, "hunks": 1}:
        return False, "实际 %r" % (stat,)
    same = diff_mod.text_diff_stat("a\nb\n\n\n", "a\r\nb")
    if same != {"added_lines": 0, "removed_lines": 0, "hunks": 0}:
        return False, "行尾差异与末尾空行不该算改动，实际 %r" % (same,)
    return True, "diff_stat 只算真改动（CRLF 与末尾空行被归一化掉）"


def case_deterministic():
    document = _clean_run()
    first = dumps(judge(document, document, run_json_path=None, baseline_path=None))
    second = dumps(judge(document, document, run_json_path=None, baseline_path=None))
    if first != second:
        return False, "同一份输入判两次结果不一致（不是纯函数）"
    if not first.endswith("\n") or first.endswith("\n\n"):
        return False, "导出应以单个 \\n 结尾"
    if json.loads(first) != json.loads(second):
        return False, "两次判分语义不一致"
    return True, "同一份输入 → 字节级相同的 score 快照"


def case_no_fake_missing_values():
    problems = find_fake_missing(judge(_clean_run()))
    if problems:
        return False, "判分产物里出现了假缺失值：%r" % (problems[:3],)
    dirty = {"cost": {"usd": -1}, "first_pass_rate": float("nan"), "rounds": "none"}
    if len(find_fake_missing(dirty)) < 3:
        return False, "扫描器漏抓了 -1 / NaN / \"none\""
    return True, "产物干净；计数/成本/比率字段上的 -1 / NaN / \"none\" 会被抓住"


def case_verdict_branches():
    fail_step = dict(_clean_run()["run"]["steps"][0])
    fail_step["asserts"] = {
        "total": 1, "passed": 0, "failed": 1, "timeout": 0, "unacked": 0,
        "items": [{"expr": "cmd:python -m x --self-test", "kind": "cmd",
                   "result": "fail", "detail": "exit=1"}],
    }
    fail_step["exit_code"] = 1
    fail_step["exit_code_src"] = "gate"
    fail_step["assert_pass"] = {"first_pass_rate": 0.0, "final_pass_rate": 0.0}
    verdict_fail = judge(_clean_run(step=fail_step))["verdict"]
    if verdict_fail != "fail":
        return False, "非 0 退出码 + 失败断言应为 fail，实际 %r" % (verdict_fail,)

    verdict_pass = judge(_clean_run())["verdict"]
    if verdict_pass != "pass":
        return False, "四类全齐、通过率 1.0 应为 pass，实际 %r" % (verdict_pass,)

    unknown_tokens = {"input": 100, "output": 10, "reasoning": UNKNOWN,
                      "cache_read": 0, "cache_write": 0, "complete": False}
    verdict_unknown = judge(_clean_run(tokens=unknown_tokens))["verdict"]
    if verdict_unknown != UNKNOWN:
        return False, "有 unknown 关键字段应为 unknown（不许默认 pass），实际 %r" % (verdict_unknown,)
    return True, "verdict 三条分支：fail / pass / unknown 各就各位"


CASES = (
    ("rounds:rows_with_reqs=0", case_rounds_unknown_without_reqs),
    ("asserts:0 条断言→通过率 null", case_rate_null_without_asserts),
    ("diff:基准缺失→整段 unknown", case_diff_unknown_without_baseline),
    ("cost:两侧 unknown 不相减", case_cost_no_delta_when_unknown),
    ("exitcode:声明 cmd 但没码→unknown+unjudged", case_exit_unknown_counts_as_unjudged),
    ("exitcode:没声明 cmd→null 不适用", case_exit_null_when_no_cmd),
    ("容差:max(1e-9,1e-6*max)", case_tolerance_is_pinned),
    ("asserts:ack 未确认计入分母", case_ack_unacked_in_denominator),
    ("asserts:超时与 fail 分开", case_timeout_separate_from_fail),
    ("cost:没有价目就不许算钱", case_no_money_without_price_table),
    ("diff:文本 diff_stat 归一化", case_text_diff_stat),
    ("确定性:两次判分字节一致", case_deterministic),
    ("缺失值:没有 -1/NaN 冒充", case_no_fake_missing_values),
    ("verdict:fail/pass/unknown", case_verdict_branches),
)


def main(out=print, err=None) -> int:
    import sys

    err = err or sys.stderr
    failed = 0
    for name, function in CASES:
        try:
            ok, detail = function()
        except Exception as exc:  # 自检自己炸了也算 FAIL，不静默
            ok, detail = False, "自检抛异常：%s: %s" % (exc.__class__.__name__, exc)
        if ok:
            out("PASS %s —— %s" % (name, detail))
        else:
            failed += 1
            out("FAIL %s" % name)
            print("     %s" % detail, file=err)
    out("共 %d 条：PASS %d / FAIL %d" % (len(CASES), len(CASES) - failed, failed))
    return 0 if failed == 0 else 1
