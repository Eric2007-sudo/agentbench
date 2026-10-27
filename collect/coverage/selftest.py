"""collect/coverage/selftest.py —— `python -m collect.coverage --self-test` 的用例。

每条打一行 `PASS` / `FAIL`，全过 exit 0。用例全部在内存里造数据（负例用临时目录），
不写 `.plan/`、不写宿主库、不读别人的产物。
"""

from __future__ import annotations

import json
import os
import tempfile

from . import table
from .table import LEVELS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spec", "points.json")


def _plan(*steps):
    return {"schema": 2, "steps": [{"n": n, "accept": list(accepts)} for n, accepts in steps]}


def _score(*items, step=1):
    return {"asserts": {"by_step": [{"step": step, "items": [dict(item) for item in items]}]}}


def _levels(document):
    return [item["level"] for item in document["items"]]


def _point(point_id, level, reason="人工依据：看过", evidence="contract/trace.md:251"):
    return {"id": point_id, "clause": "示例条款", "level": level,
            "evidence": evidence, "reason": reason}


# ---------------------------------------------------------------- 用例

def case_accept_ids_are_global():
    """accept:<n> 跨步连续编号（不按步号、不按步内序号），条数由数据数出来。"""
    plan = _plan((1, ["a", "b"]), (2, []), (3, ["c", "d", "e"]))
    document = table.build_table(plan, {}, [], 10)
    ids = [item["id"] for item in document["items"]]
    assert ids == ["accept:1", "accept:2", "accept:3", "accept:4", "accept:5"], ids
    assert table.count_accepts(plan) == 5
    assert document["counts"]["total"] == len(document["items"]) == 5


def case_mechanical_needs_exact_expr():
    """mechanical ⟺ expr 完全相等且 result ∈ {pass, fail}；差一个字符就不算。"""
    plan = _plan((1, ["file:exists:x", "cmd:y", "grep:z"]))
    score = _score({"expr": "file:exists:x", "result": "pass"},
                   {"expr": "cmd:y ", "result": "pass"})
    document = table.build_table(plan, score, [], 10)
    assert _levels(document) == ["mechanical", "none", "none"], _levels(document)
    evidence = document["items"][0]["evidence"]
    assert "by_step[step=1].items[0]" in evidence and "pass" in evidence, evidence
    assert document["items"][1]["evidence"] == "", document["items"][1]


def case_unfinished_asserts_are_not_mechanical():
    """timeout / unacked / unknown 都只是「没验完」，不是机械证据（也不许当通过）。"""
    plan = _plan((1, ["cmd:a", "ack:b", "cmd:c"]))
    score = _score({"expr": "cmd:a", "result": "timeout"},
                   {"expr": "ack:b", "result": "unacked"},
                   {"expr": "cmd:c", "result": "unknown"})
    document = table.build_table(plan, score, [], 10)
    assert _levels(document) == ["none", "none", "none"], _levels(document)


def case_clause_points_keep_declared_level():
    """clause 条目按登记表给级别；delta == 0 时不许出现判别力声明。"""
    points = [_point("clause:a", "semantic"), _point("clause:b", "none"),
              _point("clause:c", "not_applicable", reason="本次不适用：夹具没有该形态")]
    document = table.build_table(_plan((1, ["x"])), {}, points, len(points))
    assert _levels(document) == ["none", "semantic", "none", "not_applicable"], _levels(document)
    assert document["items"][0]["prefix"] == "accept:", document["items"][0]
    assert document["items"][1]["prefix"] == "clause:", document["items"][1]
    assert document["counts"] == {"mechanical": 0, "semantic": 1, "none": 2,
                                  "not_applicable": 1, "total": 4}, document["counts"]
    assert "discriminating_power" not in document
    assert table.check_document(document) == [], table.check_document(document)


def case_not_applicable_needs_reason():
    """not_applicable 必须带 reason；counts 与 items 必须一致 —— 两条都要被校验器抓住。"""
    bad = {
        "counts": {"mechanical": 0, "semantic": 0, "none": 0, "not_applicable": 1, "total": 1},
        "items": [{"id": "accept:1", "source": "accept", "level": "not_applicable",
                   "expr": "x", "evidence": "", "reason": ""}],
        "unregistered_clauses": {"clauses_total": 1, "registered": 1, "delta": 0},
    }
    problems = table.check_document(bad)
    assert any("not_applicable" in text and "reason" in text for text in problems), problems

    bad["items"][0]["reason"] = "本次不适用：夹具没有该形态"
    bad["counts"]["total"] = 2
    problems = table.check_document(bad)
    assert any("total" in text for text in problems), problems


def case_counts_self_consistent():
    """counts 每个证据级都等于 items 里的实际计数，总数等于长度。"""
    plan = _plan((1, ["file:exists:x", "nope"]))
    score = _score({"expr": "file:exists:x", "result": "fail"})
    points = [_point("clause:a", "semantic")]
    document = table.build_table(plan, score, points, 41)
    assert document["counts"] == {"mechanical": 1, "semantic": 1, "none": 1,
                                  "not_applicable": 0, "total": 3}, document["counts"]
    assert table.check_document(document) == [], table.check_document(document)


def case_delta_drives_discriminating_power():
    """delta > 0 必须写「不具备判别力」；delta == 0 必须不写。"""
    points = [_point("clause:a", "semantic")]
    open_gap = table.build_table(_plan((1, ["x"])), {}, points, 41)
    assert open_gap["unregistered_clauses"] == {"clauses_total": 41, "registered": 1, "delta": 40}
    assert open_gap.get("discriminating_power") == "不具备判别力", open_gap.get("discriminating_power")
    assert table.check_document(open_gap) == [], table.check_document(open_gap)

    closed = table.build_table(_plan((1, ["x"])), {}, points, 1)
    assert "discriminating_power" not in closed


def case_deleting_one_assert_downgrades():
    """破坏性用例：从 score.json 删一条断言，只有对应的那条规格点掉级，别的不动。"""
    plan = _plan((1, ["file:exists:x", "cmd:y"]), (2, ["grep:z"]))
    score = _score({"expr": "file:exists:x", "result": "pass"},
                   {"expr": "cmd:y", "result": "pass"}, step=1)
    before = table.build_table(plan, score, [], 10)
    assert _levels(before) == ["mechanical", "mechanical", "none"], _levels(before)

    score["asserts"]["by_step"][0]["items"] = [
        item for item in score["asserts"]["by_step"][0]["items"]
        if item["expr"] != "file:exists:x"
    ]
    after = table.build_table(plan, score, [], 10)
    assert _levels(after) == ["none", "mechanical", "none"], _levels(after)
    assert after["counts"]["mechanical"] == 1, after["counts"]


def case_output_is_byte_identical():
    """同一份输入连跑两次，落盘文本逐字节相同：indent=1、末尾恰好一个换行。"""
    plan = _plan((1, ["a", "b"]), (2, ["c"]))
    points = [_point("clause:a", "semantic")]
    first = table.dumps(table.build_table(plan, {}, points, 41))
    second = table.dumps(table.build_table(plan, {}, points, 41))
    assert first == second
    assert first.endswith("\n") and not first.endswith("\n\n"), repr(first[-3:])
    assert "\r" not in first
    reloaded = json.loads(first)
    assert reloaded["counts"]["total"] == 4, reloaded["counts"]


def case_banned_keys_are_caught_by_key_name():
    """禁用词按**键名**判：degraded_reasons 这种值/键不许误伤（"grade" 是子串的陷阱）。"""
    base = {
        "counts": {"mechanical": 0, "semantic": 0, "none": 0, "not_applicable": 0, "total": 0},
        "items": [],
        "unregistered_clauses": {"clauses_total": 0, "registered": 0, "delta": 0},
    }
    assert table.check_document(base) == [], table.check_document(base)

    base["degraded_reasons"] = ["grade 只是这个键里的一段字母"]
    assert table.check_document(base) == [], table.check_document(base)

    for banned in ("score", "weight", "grade", "threshold", "rate"):
        probe = json.loads(table.dumps(base))
        probe[banned] = 1
        problems = table.check_document(probe)
        assert any(banned in text for text in problems), (banned, problems)
        probe_nested = json.loads(table.dumps(base))
        probe_nested["items"] = [{"id": "accept:1", "nested": {banned: "1"}}]
        problems = table.check_document(probe_nested)
        assert any(banned in text for text in problems), (banned, problems)


def case_empty_plan_is_zero_not_unknown():
    """计划里一条 accept 都没有 → total 是 0（真的 0），不是 unknown。"""
    document = table.build_table(_plan((1, []), (2, [])), {}, [], 41)
    assert document["counts"]["total"] == 0
    assert document["items"] == []
    assert table.check_document(document) == [], table.check_document(document)


def case_guard_blocks_foreign_plan():
    """一致性守卫：交集不过半 → ok=False；同一项目 → ok=True；没有 score → 不比。"""
    fixture_plan = _plan((1, ["file:exists:contract/trace.md", "cmd:x"]),
                         (2, ["grep:y", "file:exists:z"]))
    same_score = _score({"expr": "file:exists:contract/trace.md", "result": "pass"},
                        {"expr": "cmd:x", "result": "pass"},
                        {"expr": "grep:y", "result": "pass"},
                        {"expr": "file:exists:z", "result": "pass"})
    matched = table.consistency_guard(fixture_plan, same_score)
    assert matched["ok"], matched
    assert (matched["plan_total"], matched["score_total"], matched["intersection"]) == (4, 4, 4), matched

    foreign = _plan(*[(n, ["bench:%d:%d" % (n, i) for i in range(20)]) for n in range(1, 8)])
    blocked = table.consistency_guard(foreign, same_score)
    assert not blocked["ok"], blocked
    assert (blocked["plan_total"], blocked["score_total"], blocked["intersection"]) == (140, 4, 0), blocked
    assert blocked["plan_sample"] and blocked["score_sample"], blocked

    no_score = table.consistency_guard(foreign, {})
    assert no_score["ok"] and no_score["score_total"] == 0, no_score


def case_guard_cli_stops_without_writing():
    """守卫失败：CLI 非 0 退出、不留下 coverage.json；--json 时 stdout 仍是错误对象。"""
    import contextlib
    import io
    from .__main__ import main as cli_main

    with tempfile.TemporaryDirectory() as tmp:
        plan_path = os.path.join(tmp, "plan.json")
        score_path = os.path.join(tmp, "score.json")
        out_path = os.path.join(tmp, "coverage.json")
        with open(plan_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(_plan((1, ["cmd:this-project"]), (2, ["file:exists:other"])),
                      handle, ensure_ascii=False)
        with open(score_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(_score({"expr": "cmd:that-project", "result": "pass"}),
                      handle, ensure_ascii=False)

        code = cli_main(["--plan", plan_path, "--score", score_path, "--out", out_path])
        assert code != 0, code
        assert not os.path.exists(out_path), out_path

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--plan", plan_path, "--score", score_path,
                             "--out", out_path, "--json"])
        assert code != 0, code
        assert not os.path.exists(out_path), out_path
        payload = json.loads(buffer.getvalue())
        assert payload.get("ok") is False, payload
        assert payload["error"]["code"] == "plan_score_mismatch", payload


def case_points_validation_rejects_bad_entries():
    """登记表校验：id 重复、级别非法、evidence 指向不存在的文件 —— 一律报错，不静默吞。"""
    good = {"points": [_point("clause:a", "semantic")]}
    bad_duplicate = {"points": [_point("clause:a", "semantic"), _point("clause:a", "none")]}
    bad_level = {"points": [_point("clause:a", "maybe")]}
    bad_prefix = {"points": [_point("point:a", "none")]}
    for document, label in ((bad_duplicate, "duplicate"), (bad_level, "level"),
                            (bad_prefix, "prefix")):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "points.json")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, ensure_ascii=False)
            try:
                table.load_points(path)
            except ValueError:
                continue
            raise AssertionError("%s 的登记表没有被拒绝" % label)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "points.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(good, handle, ensure_ascii=False)
        assert len(table.load_points(path)) == 1
        problems = table.check_point_evidence(
            [_point("clause:a", "semantic", evidence="contract/trace.md:999999")], ROOT)
        assert problems and "超出" in problems[0], problems


def case_trace_enumeration_is_deterministic():
    """clauses_total 的自动口径：算两遍一样；标题、代码块、表格分隔行不计。"""
    sample = "\n".join([
        "# 标题里也写着 必须，但这不是条款",
        "- 必须做甲",
        "- 普通一句",
        "```",
        "不许 出现在代码块里",
        "```",
        "| 必须做乙 |",
        "|---|---|",
        "",
    ])
    assert table.count_clauses(sample) == 2, table.count_clauses(sample)
    assert table.count_clauses(sample) == table.count_clauses(sample)

    trace_path = os.path.join(ROOT, "contract", "trace.md")
    with open(trace_path, "r", encoding="utf-8", errors="replace") as handle:
        trace_text = handle.read()
    first = table.count_clauses(trace_text)
    assert first == table.count_clauses(trace_text)
    points = table.load_points(SPEC)
    assert first >= len(points), (first, len(points))


def case_real_spec_is_valid():
    """真登记表：至少 6 条、id 不重、evidence 指向真实文件与真实行号。"""
    points = table.load_points(SPEC)
    assert len(points) >= 6, len(points)
    assert len({entry["id"] for entry in points}) == len(points)
    problems = table.check_point_evidence(points, ROOT)
    assert problems == [], problems
    for entry in points:
        assert entry["level"] in LEVELS, entry
        assert str(entry["clause"]).strip(), entry


CASES = (
    ("accept 编号跨步连续且条数不写死", case_accept_ids_are_global),
    ("mechanical 只认原文完全相等的断言", case_mechanical_needs_exact_expr),
    ("未验完的断言不算 mechanical", case_unfinished_asserts_are_not_mechanical),
    ("clause 条目按登记级别落表", case_clause_points_keep_declared_level),
    ("not_applicable 必须带 reason", case_not_applicable_needs_reason),
    ("counts 与 items 自洽", case_counts_self_consistent),
    ("delta 决定判别力声明", case_delta_drives_discriminating_power),
    ("删一条断言只掉对应那一级", case_deleting_one_assert_downgrades),
    ("同输入两次输出逐字节相同", case_output_is_byte_identical),
    ("禁用键名按键名判、不误伤 degraded_reasons", case_banned_keys_are_caught_by_key_name),
    ("空计划是 0 不是 unknown", case_empty_plan_is_zero_not_unknown),
    ("计划与判分不是同一个项目会被守卫拦下", case_guard_blocks_foreign_plan),
    ("守卫失败时 CLI 非 0 且不写表", case_guard_cli_stops_without_writing),
    ("登记表坏条目被拒绝", case_points_validation_rejects_bad_entries),
    ("条款梳理口径确定且不小于已登记数", case_trace_enumeration_is_deterministic),
    ("真登记表合法且 evidence 可核", case_real_spec_is_valid),
)


def main(argv=None) -> int:
    failures = 0
    for name, case in CASES:
        try:
            case()
        except AssertionError as exc:
            failures += 1
            print("FAIL %s: %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001 —— 自检要把任何异常都报成 FAIL，不吞
            failures += 1
            print("FAIL %s: %s: %s" % (name, exc.__class__.__name__, exc))
        else:
            print("PASS %s" % name)
    print("自检 %d 条，失败 %d 条" % (len(CASES), failures))
    return 0 if failures == 0 else 1
