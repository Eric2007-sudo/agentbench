"""collect/score/verify.py —— `--verify runs/fx-rework01/score.json`。

拿 `.plan/plan.json` 步 4 钉死的期望值，对着落盘的 `score.json` **逐条**比：
全对 exit 0；任一条不符 → exit 1，明细打 stderr（stdout 只留 PASS/FAIL 行与总条数）。

这组期望值来自契约与夹具（`contract/trace.md` §5 / `contract/slices.md` §4.1 / `collect/fixtures/README.md`），
不是「跑一下看看」出来的：改期望值等于改验收口径，得先改契约。
"""

from __future__ import annotations

import json

from .base import UNKNOWN, is_number, is_unknown, numbers_close, tolerance_block

#: 夹具 `rework01` 的钉死期望（手算值，见 `collect/fixtures/README.md`）。
EXPECTED = {
    "cost_usd": 0.0091,          # 0.0042 + 0.0049（第 3 行故意没 cost，不计入已知部分）
    "unknown_rows": 1,
    "rounds": 3,                 # Σ reqs = 1 + 1 + 1
    "rows_total": 4,             # 账本 4 行（第 4 行是坏 JSON，仍计入行数）
    "rows_with_reqs": 3,
    "sha_slices": "5a82c01a",
    "sha_plan_dir": "471dd0e5",
    "step1": {"total": 2, "passed": 2, "first_pass_rate": 0.5, "final_pass_rate": 1.0,
              "rework": 1, "first_failed": True},
    "step2": {"total": 2, "passed": 2, "first_pass_rate": 1.0, "final_pass_rate": 1.0,
              "rework": 0, "first_failed": False},
}

#: 不许出现的字段（加权总分 / 主观分 / 阈值）。
FORBIDDEN_KEYS = ("score", "total_score", "weight", "weights", "grade", "rating",
                  "threshold", "min_pass_rate", "pass_mark")


def _by_step(section, n):
    for entry in section.get("by_step") or []:
        if entry.get("step") == n:
            return entry
    return {}


def _item(section, path):
    for entry in section.get("items") or []:
        if entry.get("path") == path:
            return entry
    return {}


def _eq(name, actual, expected):
    ok = actual == expected
    return name, ok, "期望 %r，实际 %r" % (expected, actual)


def _close(name, actual, expected):
    ok = is_number(actual) and numbers_close(actual, expected)
    return name, ok, "期望 %r（容差 %s），实际 %r" % (expected, tolerance_block()["formula"], actual)


def _keys_walk(document, prefix=""):
    found = []
    if isinstance(document, dict):
        for key, value in document.items():
            path = "%s.%s" % (prefix, key) if prefix else key
            found.append((path, key))
            found.extend(_keys_walk(value, path))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(_keys_walk(value, "%s[%d]" % (prefix, index)))
    return found


def check(document: dict) -> list:
    """跑完全部检查，返回 [(name, ok, detail)]。"""
    checks = []

    # 形状 ---------------------------------------------------------------
    checks.append(_eq("schema==1", document.get("schema"), 1))
    checks.append(_eq("kind=='score'", document.get("kind"), "score"))
    for section in ("exitcode", "asserts", "diff", "cost"):
        body = document.get(section) or {}
        checks.append(("段 %s 有口径来源" % section,
                       bool(body.get("basis") or body.get("rule")),
                       "basis/rule 为空"))

    # ① exitcode ---------------------------------------------------------
    exit_section = document.get("exitcode") or {}
    step1_exit = _by_step(exit_section, 1)
    step2_exit = _by_step(exit_section, 2)
    checks.append(_eq("exitcode 步1 没声明 cmd → exit_code 为 null（不适用，不是失败）",
                      step1_exit.get("exit_code"), None))
    checks.append(_eq("exitcode 步1 exit_code_src 为 null", step1_exit.get("exit_code_src"), None))
    checks.append(_eq("exitcode 步1 result=='not_applicable'（不适用≠失败）",
                      step1_exit.get("result"), "not_applicable"))
    checks.append(_eq("exitcode 步2 exit_code==0（gates.log exit=0）", step2_exit.get("exit_code"), 0))
    checks.append(_eq("exitcode 步2 exit_code_src=='gate'", step2_exit.get("exit_code_src"), "gate"))
    checks.append(_eq("exitcode 步2 result=='pass'", step2_exit.get("result"), "pass"))
    unjudged = exit_section.get("unjudged")
    checks.append(("exitcode 里有 unjudged 列表（未判单列，不算过也不算破）",
                   isinstance(unjudged, list), "unjudged 不是列表：%r" % (unjudged,)))
    checks.append(_eq("exitcode unjudged_count == 0（本例两条 cmd 都有 exit=）",
                      exit_section.get("unjudged_count"), 0))

    # ② asserts ----------------------------------------------------------
    asserts_section = document.get("asserts") or {}
    for step_n in (1, 2):
        expected = EXPECTED["step%d" % step_n]
        entry = _by_step(asserts_section, step_n)
        for field in ("total", "passed"):
            checks.append(_eq("asserts 步%d %s" % (step_n, field), entry.get(field), expected[field]))
        checks.append(_close("asserts 步%d first_pass_rate" % step_n,
                             entry.get("first_pass_rate"), expected["first_pass_rate"]))
        checks.append(_close("asserts 步%d final_pass_rate" % step_n,
                             entry.get("final_pass_rate"), expected["final_pass_rate"]))
        checks.append(_eq("asserts 步%d rework" % step_n, entry.get("rework"), expected["rework"]))
        checks.append(_eq("asserts 步%d first_failed" % step_n,
                          entry.get("first_failed"), expected["first_failed"]))
    checks.append(_eq("asserts 不做阈值判定（thresholds 为 false）",
                      asserts_section.get("thresholds"), False))
    checks.append(_eq("asserts 分母是声明口径（计数 total==4）",
                      (asserts_section.get("counts") or {}).get("total"), 4))

    # ③ diff -------------------------------------------------------------
    diff_section = document.get("diff") or {}
    checks.append(_eq("diff 状态是 compared（基准可读）", diff_section.get("status"), "compared"))
    slices = _item(diff_section, "artifacts/slices.md")
    plan_dir = _item(diff_section, "artifacts/plan_dir.py")
    checks.append(_eq("diff artifacts/slices.md 未变（sha256 相同）", slices.get("status"), "unchanged"))
    checks.append(_eq("diff artifacts/plan_dir.py 未变（sha256 相同）",
                      plan_dir.get("status"), "unchanged"))
    checks.append(("diff slices.md 的 sha256 以 5a82c01a 开头",
                   str(slices.get("after_sha256", "")).startswith(EXPECTED["sha_slices"]),
                   "实际 %r" % (slices.get("after_sha256"),)))
    checks.append(("diff plan_dir.py 的 sha256 以 471dd0e5 开头",
                   str(plan_dir.get("after_sha256", "")).startswith(EXPECTED["sha_plan_dir"]),
                   "实际 %r" % (plan_dir.get("after_sha256"),)))
    checks.append(_eq("diff 汇总 added==0", (diff_section.get("summary") or {}).get("added"), 0))
    checks.append(_eq("diff 汇总 modified==0", (diff_section.get("summary") or {}).get("modified"), 0))
    checks.append(_eq("diff 汇总 unchanged==2", (diff_section.get("summary") or {}).get("unchanged"), 2))
    tol = diff_section.get("tolerance") or {}
    checks.append(_close("diff 容差 abs 写死 1e-9", tol.get("abs"), 1e-9))
    checks.append(_close("diff 容差 rel 写死 1e-6", tol.get("rel"), 1e-6))
    checks.append(("diff 容差公式与契约一字不差",
                   tol.get("formula") == tolerance_block()["formula"],
                   "实际 %r" % (tol.get("formula"),)))

    # ④ cost -------------------------------------------------------------
    cost_section = document.get("cost") or {}
    checks.append(_close("cost.usd==0.0091（已知部分之和）", cost_section.get("usd"),
                         EXPECTED["cost_usd"]))
    checks.append(_eq("cost.complete==false", cost_section.get("complete"), False))
    checks.append(_eq("cost.unknown_rows==1", cost_section.get("unknown_rows"),
                      EXPECTED["unknown_rows"]))
    checks.append(_eq("cost.basis=='host'", cost_section.get("basis"), "host"))
    checks.append(_eq("cost.is_lower_bound==true（下限口径）", cost_section.get("is_lower_bound"), True))
    checks.append(_eq("cost 里 rounds==3（Σ reqs）", cost_section.get("rounds"), EXPECTED["rounds"]))
    coverage = cost_section.get("rounds_coverage") or {}
    checks.append(_eq("cost.rounds_coverage.rows_total==4", coverage.get("rows_total"),
                      EXPECTED["rows_total"]))
    checks.append(_eq("cost.rounds_coverage.rows_with_reqs==3", coverage.get("rows_with_reqs"),
                      EXPECTED["rows_with_reqs"]))
    pricing = cost_section.get("pricing") or {}
    checks.append(_eq("cost 没有价目就不算钱（table=='none'）", pricing.get("table"), "none"))
    checks.append(_eq("cost 没有一行是估算的（estimated_rows==0）", pricing.get("estimated_rows"), 0))

    # 总论 ---------------------------------------------------------------
    checks.append(_eq("run.verdict=='unknown'（有 unknown 关键字段就不许给 pass）",
                      document.get("verdict"), UNKNOWN))
    degraded = document.get("degraded") or {}
    reasons = " ".join(degraded.get("run_degraded_reasons") or [])
    checks.append(("degraded 里保留 run.json 的 ledger.bad_json:4", "ledger.bad_json:4" in reasons,
                   "实际 %r" % (reasons[:200],)))

    # 反主观 -------------------------------------------------------------
    hit = [path for path, key in _keys_walk(document) if key.lower() in FORBIDDEN_KEYS]
    checks.append(("产物里没有加权总分 / 阈值 / 主观分字段", not hit,
                   "命中：%s" % ", ".join(hit[:6])))
    return checks


def run(path: str, out=print, err=None) -> int:
    """跑 `--verify`，返回退出码。"""
    import sys

    err = err or sys.stderr
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            document = json.load(handle)
    except OSError as exc:
        print("FAIL verify: 读不到 %s（%s）" % (path, exc.__class__.__name__), file=err)
        return 1
    except ValueError as exc:
        print("FAIL verify: %s 不是合法 JSON（%s）" % (path, exc), file=err)
        return 1

    checks = check(document)
    failed = 0
    for name, ok, detail in checks:
        if ok:
            out("PASS %s" % name)
        else:
            failed += 1
            out("FAIL %s" % name)
            print("     %s" % detail, file=err)
    out("共 %d 条：PASS %d / FAIL %d（对象 %s）" % (len(checks), len(checks) - failed, failed, path))
    return 0 if failed == 0 else 1
