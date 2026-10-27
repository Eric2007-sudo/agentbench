"""`collect/measure` 的内置自检：一条硬规矩一条用例，一条一行 `PASS`/`FAIL`，全过 exit 0。

用例覆盖 `contract/dimensions.md` §1 / §4 / §5 与简报「二、这一轮要改的 7 个真错」+「三、3 个自测误报」：

| 编号 | 规矩 |
|---|---|
| r1 | `ttft_s` / `tool_calls` 恒为 `"unknown"`；扫数字时跳过 `otel_map`（它是映射说明，不是测量值） |
| r2 | p50/p90/max 用最近秩法；`n < 5` → `too_few_samples`；样本坏 → `unknown_sample`；`n == 0` → 三个 `null` |
| r3 | 缺字段一律 `"unknown"`（不写 0、不写 null 冒充）；`finished_at` 缺 → `wall_s` unknown |
| r4 | `wall_s` unknown 或 0 → `out_tokens_per_wall_s` 为 `"unknown"` |
| r5 | 禁用词只按**键名**判定（`degraded_reasons` 不算 `grade`）；整份文本里也不出现这些子串 |
| r6 | 幂等：同一份 run.json 连跑两次 measure.json 逐字节相同；输入目录不许多出文件 |
| r7 | 模态登记表：枚举合法、读不到 capabilities 时不猜 |
| r8 | `--json` 出口：stdout 恰好一个 JSON、`ensure_ascii=False`、`indent=1`、末尾一个换行 |
| r9 | 夹具期望值：wall/turns/速率 + 两 step 的 gap_s / round_gap（首轮不占位、n=间隔数） |
| r10 | `too_few_samples` 与 `unknown_sample` 分开；n=0 时三个分位数是 null |
| r11 | `steps[].wall_s`：无轮次 null、单轮 unknown + reason、≥2 轮才是数 |
| r12 | run 级 `round_gap` 不跨 step 聚合（≥2 个 step 出样本 → unknown + reason） |
| r13 | `--run --out` 的 measure.json 必须带 `modalities` 块（每次读配置，不缓存） |
"""

import sys

sys.dont_write_bytecode = True

import hashlib
import json
import os
import tempfile

from . import base, modalities, perf
from . import __main__ as cli

RESULTS = []


def check(name, func):
    try:
        func()
    except AssertionError as exc:
        RESULTS.append((name, False, str(exc)))
    except Exception as exc:  # 自检里任何异常都算 FAIL，不许静默
        RESULTS.append((name, False, "%s: %s" % (type(exc).__name__, exc)))
    else:
        RESULTS.append((name, True, ""))


def _no_dir():
    """一个不存在的目录：自检不读工作目录里的真账本，纯靠传入的 run.json。"""
    return os.path.join(tempfile.gettempdir(), "__measure_no_such_dir__")


def _run_doc(started="2026-10-26T20:48:14+08:00", finished="2026-10-26T21:05:00+08:00",
             output=3000, steps=None):
    if steps is None:
        steps = [
            {
                "n": 1,
                "rounds_detail": [
                    {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "rounds": 1},
                    {"seq": 2, "at": "2026-10-26T20:55:40+08:00", "rounds": 1},
                ],
            }
        ]
    return {
        "schema": 1,
        "run": {
            "id": "selftest-run",
            "root": "selftest",
            "started_at": started,
            "finished_at": finished,
            "tokens": {"input": 100, "output": output, "complete": True},
            "rounds": 3,
            "steps": steps,
        },
    }


def _fixture_doc():
    """与 `runs/fx-rework01/run.json` 同形状：step1 两轮、step2 一轮。"""
    return _run_doc(steps=[
        {
            "n": 1,
            "rounds_detail": [
                {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "rounds": 1},
                {"seq": 2, "at": "2026-10-26T20:55:40+08:00", "rounds": 1},
            ],
        },
        {
            "n": 2,
            "rounds_detail": [
                {"seq": 1, "at": "2026-10-26T21:05:00+08:00", "rounds": 1},
            ],
        },
    ])


def _measure(doc):
    return perf.measure(doc, run_dir=_no_dir(), db_path="__no_host_db__")


# ---------------------------------------------------------------- r1


def per_scan(obj, names, path="", hits=None):
    """扫全树里这些键的值；**跳过 `otel_map` 子树**（§5 的映射说明不是测量值）。"""
    hits = [] if hits is None else hits
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = "%s.%s" % (path, key)
            if key == "otel_map":
                continue
            if key in names and not base.is_unknown(value):
                hits.append(here)
            per_scan(value, names, here, hits)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            per_scan(value, names, "%s[%d]" % (path, index), hits)
    return hits


def _r1():
    doc = _run_doc()
    doc["run"]["steps"][0]["rounds_detail"][0]["time"] = {"created": 1, "completed": 2}
    doc["run"]["steps"][0]["rounds_detail"][1]["time"] = {"created": 3, "completed": 4}
    doc["run"]["started_at"] = 1
    doc["run"]["finished_at"] = 4
    out = _measure(doc)
    assert out["ttft_s"] == "unknown", "ttft_s 必须是 unknown，实际 %r" % (out["ttft_s"],)
    assert out["tool_calls"] == "unknown", "tool_calls 必须是 unknown，实际 %r" % (out["tool_calls"],)
    assert isinstance(out["ttft_s"], str) and isinstance(out["tool_calls"], str), "必须是字符串字面量"
    assert per_scan(out, ("ttft_s", "tool_calls")) == [], "otel_map 之外不许出现 ttft/tool_calls 的数值"
    assert out["otel_map"]["ttft_s"]["otl"] == "gen_ai.server.time_to_first_token", "映射说明按 §5 保留"


# ---------------------------------------------------------------- r2


def _r2():
    ordered = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert base.nearest_rank(ordered, 50) == 5.0, "p50 应为第 5 个（ceil(0.5×10)）"
    assert base.nearest_rank(ordered, 90) == 9.0, "p90 应为第 9 个（ceil(0.9×10)）"
    assert base.nearest_rank([1.0, 2.0], 50) == 1.0, "n=2 的 p50 应为第 1 个"
    assert base.nearest_rank([], 50) is None, "空集应为 None"
    few = base.percentiles([412.31, 861.0042])
    assert few["n"] == 2 and few["p50"] == 412.31 and few["max"] == 861.0042, few
    assert few["p90"] == "unknown" and few["p90_reason"] == "too_few_samples", few
    just = base.percentiles([1, 2, 3, 4, 5])
    assert just["p90"] == 5.0 and "p90_reason" not in just, just
    empty = base.percentiles([])
    assert empty["p50"] is None and empty["p90"] is None and empty["max"] is None, empty
    assert empty["n"] == 0, empty
    mixed = base.percentiles([1.0, "unknown", 3.0])
    assert mixed["p50"] == "unknown" and mixed["p90"] == "unknown" and mixed["max"] == "unknown", mixed
    assert mixed["p90_reason"] == "unknown_sample", mixed
    assert not base.is_unknown(None) and not base.is_unknown(0), "null / 0 都不是 unknown"
    loaded = set(sys.modules)
    assert not ({"numpy", "pandas"} & loaded), "不许 import numpy/pandas"


# ---------------------------------------------------------------- r3


def _r3():
    doc = _run_doc(finished="unknown")
    out = _measure(doc)
    assert out["wall_s"] == "unknown", "finished_at 缺 → wall_s unknown，实际 %r" % (out["wall_s"],)
    assert out["wall_s"] is not None and out["wall_s"] != 0, "不许写 null / 0 冒充"
    numbers = _run_doc()
    full = _measure(numbers)
    assert full["wall_s"] == 1006.0, "20:48:14 → 21:05:00 = 1006s，实际 %r" % (full["wall_s"],)
    missing_token = _run_doc()
    missing_token["run"]["tokens"].pop("output")
    nope = _measure(missing_token)
    assert nope["out_tokens_per_wall_s"] == "unknown", "tokens.output 缺 → unknown"
    no_tz = _run_doc(started="2026-10-26T20:48:14", finished="2026-10-26T21:05:00")
    assert _measure(no_tz)["wall_s"] == "unknown", "无时区端点不许算差"
    no_ledger = _measure(_run_doc())
    assert no_ledger["turns"] == 2, "run.json 里有 2 条 round，turns 应为 2"
    blank = _measure({"run": {"id": "x", "started_at": "unknown", "finished_at": "unknown",
                              "steps": [{"n": 1, "rounds_detail": []}]}})
    assert blank["steps"][0]["wall_s"] is None, "该步无轮次 → wall_s null（不适用）"
    assert blank["turns"] == 0, "run.json 在、确实没有轮次 → turns 0（合法 0）"
    assert blank["out_tokens_per_wall_s"] == "unknown", blank["out_tokens_per_wall_s"]
    gap_run = _run_doc(steps=[{"n": 1, "rounds_detail": [
        {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "rounds": 1},
        {"seq": 3, "at": "2026-10-26T20:55:40+08:00", "rounds": 1},
        {"seq": 4, "at": "2026-10-26T21:05:00+08:00", "rounds": 1},
    ]}])
    gaps = _measure(gap_run)["steps"][0]["gap_s"]
    assert gaps == ["unknown", 560.0], "seq 断裂 → 该条 unknown；正常相邻照算，实际 %r" % (gaps,)
    assert _measure(gap_run)["steps"][0]["round_gap"]["p90_reason"] == "unknown_sample", "样本里有坏条"


# ---------------------------------------------------------------- r4


def _r4():
    zero = _measure(_run_doc(started="2026-10-26T20:48:14+08:00", finished="2026-10-26T20:48:14+08:00"))
    assert zero["wall_s"] == 0.0, "同一时刻两端 → wall_s 真是 0"
    assert zero["out_tokens_per_wall_s"] == "unknown", "wall_s 为 0 → unknown（不写 Infinity、不写 0）"
    assert zero["out_tokens_unknown_reason"] == "zero_wall", zero["out_tokens_unknown_reason"]
    unknown_wall = _measure(_run_doc(finished="unknown"))
    assert unknown_wall["out_tokens_per_wall_s"] == "unknown", "wall_s unknown → unknown"
    ok = _measure(_run_doc())
    assert ok["out_tokens_per_wall_s"] == base.round6(3000 / 1006.0), ok["out_tokens_per_wall_s"]
    assert ok["out_tokens_unknown_reason"] is None, ok["out_tokens_unknown_reason"]


# ---------------------------------------------------------------- r5


def _r5():
    # 判据只有一条：**字段名本身**等于禁用词。`degraded_reasons` 里含 `grade` 不算。
    assert base.forbidden_hits({"degraded_reasons": []}) == [], "子串不算，键名才算"
    assert base.forbidden_hits({"grade": 1}) == ["grade"], "键名等于禁用词必须被抓到"
    assert base.forbidden_hits({"nested": {"score": 1}}) == ["nested.score"], "嵌套键也要抓"
    for word in base.FORBIDDEN_KEYS:
        assert base.forbidden_hits({word: 1}) == [word], word

    payload = _measure(_fixture_doc())
    assert base.forbidden_hits(payload) == [], "measure 里出现禁用字段: %r" % (base.forbidden_hits(payload),)
    text = base.dumps(payload).lower()
    for word in base.FORBIDDEN_SUBSTRINGS:
        assert word not in text, "整份文本里出现禁用子串 %s（门禁是文件级 grep，会红）" % word
    reg = modalities.registry(doc={}, config_path="__none__")
    assert base.forbidden_hits(reg) == [], "模态登记表里出现禁用字段"


# ---------------------------------------------------------------- r6


def _r6():
    tmp = tempfile.mkdtemp(prefix="measure-selftest-")
    source = os.path.join(tmp, "in")
    os.makedirs(source)
    run_path = os.path.join(source, "run.json")
    base.write_text(run_path, base.dumps(_fixture_doc()))
    before = sorted(os.listdir(source))
    out_a = os.path.join(tmp, "a", "measure.json")
    out_b = os.path.join(tmp, "b", "measure.json")
    with _quiet():
        code_a = cli.main(["--run", run_path, "--out", out_a])
        code_b = cli.main(["--run", run_path, "--out", out_b])
    assert code_a == 0 and code_b == 0, "两次都要 exit 0，实际 %r/%r" % (code_a, code_b)
    with open(out_a, "rb") as handle:
        blob_a = handle.read()
    with open(out_b, "rb") as handle:
        blob_b = handle.read()
    assert blob_a == blob_b, "同一份 run.json 两次产出字节不同"
    assert hashlib.sha256(blob_a).hexdigest() == hashlib.sha256(blob_b).hexdigest(), "sha256 不一致"
    assert not blob_a.startswith(b"\xef\xbb\xbf"), "不许带 BOM"
    assert blob_a.endswith(b"}\n") and not blob_a.endswith(b"\n\n"), "末尾恰好一个换行"
    after = sorted(os.listdir(source))
    assert after == before, "不许往输入目录里落任何东西：%r → %r" % (before, after)


# ---------------------------------------------------------------- r7


def _r7():
    doc = {"providers": {"p1": {"models": {"m1": {"capabilities": {"input": ["text"]}}}}}}
    reg = modalities.registry(doc=doc)
    assert modalities.validate(reg) == [], modalities.validate(reg)
    by = {row["modality"]: row for row in reg["modalities"]}
    assert by["text"]["status"] == "measured", by["text"]
    for name in ("image", "audio", "video", "pdf"):
        assert by[name]["status"] == "not_applicable", by[name]
        assert by[name]["reason"], "not_applicable 必须给出 reason"
    assert '["text"]' in by["image"]["evidence"], by["image"]["evidence"]
    blind = modalities.registry(doc={}, config_path="__none__")
    assert "modal.capabilities_unknown" in blind["degraded_reasons"], blind["degraded_reasons"]
    for row in blind["modalities"]:
        assert row["status"] in ("measured", "not_applicable"), "status 不许是 unknown"
        assert not base.is_unknown(row["status"]), "status 不许是 unknown"
    image_model = {"providers": {"p": {"models": {"m": {"capabilities": {"input": ["text", "image"]}}}}}}
    reg2 = modalities.registry(doc=image_model)
    by2 = {row["modality"]: row for row in reg2["modalities"]}
    assert by2["image"]["status"] == "measured", by2["image"]
    assert modalities.validate({"modalities": [{"modality": "text", "status": "unknown"}]}), \
        "校验器必须拒绝 unknown status"


# ---------------------------------------------------------------- r8


class _StringOut(object):
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    def flush(self):
        pass


class _quiet(object):
    """自检里把 CLI 的人话（stderr）收起来，stdout 只留 PASS/FAIL。"""

    def __enter__(self):
        self._stderr = sys.stderr
        sys.stderr = _StringOut()
        return self

    def __exit__(self, exc_type, exc, tb):
        sys.stderr = self._stderr
        return False


def _r8():
    tmp = tempfile.mkdtemp(prefix="measure-selftest-json-")
    run_path = os.path.join(tmp, "run.json")
    base.write_text(run_path, base.dumps(_fixture_doc()))
    original = sys.stdout
    buffer = _StringOut()
    sys.stdout = buffer
    try:
        with _quiet():
            code = cli.main(["--run", run_path, "--json"])
            bad_code = cli.main(["--run", os.path.join(tmp, "nope.json"), "--json"])
    finally:
        sys.stdout = original
    assert code == 0 and bad_code == 3, "exit code 应为 0 / 3，实际 %r / %r" % (code, bad_code)
    assert len(buffer.parts) == 2, "两端各恰好一次写入，实际 %r 次" % (len(buffer.parts),)
    first = json.loads(buffer.parts[0])
    assert first["ok"] is True and "run_id" in first, "成功信封要带内容"
    assert isinstance(first["modalities"], list) and len(first["modalities"]) == 5, "信封里也要有模态登记表"
    assert buffer.parts[0].endswith("\n") and not buffer.parts[0].endswith("\n\n"), "末尾一个换行"
    second = json.loads(buffer.parts[1])
    assert second["ok"] is False and second["error"]["code"] == "data_unavailable", second
    assert "中文" in base.dumps({"k": "中文"}), "ensure_ascii=False"
    assert base.dumps({"k": 1}) == '{\n "k": 1\n}\n', base.dumps({"k": 1})


# ---------------------------------------------------------------- r9


def _r9():
    """简报「五、本夹具的期望值」逐条对上。"""
    out = _measure(_fixture_doc())
    assert out["wall_s"] == 1006.0, out["wall_s"]
    assert out["turns"] == 3, out["turns"]
    assert out["out_tokens_per_wall_s"] == 2.982107, out["out_tokens_per_wall_s"]
    assert out["ttft_s"] == "unknown" and out["tool_calls"] == "unknown", "两个结构上拿不到的量"

    assert [s["n"] for s in out["steps"]] == [1, 2], "steps 按 n 升序"
    one, two = out["steps"]

    assert one["turns"] == 2 and one["wall_s"] == 446.0, one
    assert one["gap_s"] == [446.0], "首轮不占位：长度 = 轮次数 − 1，实际 %r" % (one["gap_s"],)
    assert one["round_gap"] == {"n": 1, "p50": 446.0, "p90": "unknown", "max": 446.0,
                                "p90_reason": "too_few_samples"}, one["round_gap"]

    assert two["turns"] == 1, two
    assert two["wall_s"] == "unknown", "单轮步不许写 0.0，实际 %r" % (two["wall_s"],)
    assert two["wall_s_reason"] == "fewer_than_two_rounds", two.get("wall_s_reason")
    assert two["gap_s"] == [], "单轮步没有间隔，不是 [unknown]，实际 %r" % (two["gap_s"],)
    assert two["round_gap"] == {"n": 0, "p50": None, "p90": None, "max": None}, two["round_gap"]

    assert out["round_gap"] == {"n": 1, "p50": 446.0, "p90": "unknown", "max": 446.0,
                                "p90_reason": "too_few_samples"}, out["round_gap"]
    assert out["round_gap"]["n"] == len(one["gap_s"]), "run 级 n 是间隔条数"


# ---------------------------------------------------------------- r10


def _r10():
    too_few = base.percentiles([446.0])
    assert too_few == {"n": 1, "p50": 446.0, "p90": "unknown", "max": 446.0,
                       "p90_reason": "too_few_samples"}, too_few
    dirty = base.percentiles([446.0, "unknown", 300.0, 200.0, 100.0])
    assert dirty["p50"] == "unknown" and dirty["p90"] == "unknown" and dirty["max"] == "unknown", dirty
    assert dirty["p90_reason"] == "unknown_sample", "样本坏 ≠ 样本少"
    assert "p90_reason" not in base.percentiles([1.0] * 5), "干净足量时不该有 reason"
    none_gap = _measure(_run_doc(steps=[{"n": 1, "rounds_detail": []}]))
    assert none_gap["steps"][0]["round_gap"] == {"n": 0, "p50": None, "p90": None, "max": None}, \
        "n=0 是「不适用」→ null，不是 unknown"


# ---------------------------------------------------------------- r11


def _r11():
    empty_step = _measure(_run_doc(steps=[{"n": 1, "rounds_detail": []}]))["steps"][0]
    assert empty_step["wall_s"] is None and empty_step["wall_s_reason"] is None, empty_step
    single = _measure(_run_doc(steps=[{"n": 1, "rounds_detail": [
        {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "rounds": 1}]}]))["steps"][0]
    assert single["wall_s"] == "unknown", "一个时间点没有长度"
    assert single["wall_s_reason"] == "fewer_than_two_rounds", single
    assert single["gap_s"] == [], single
    pair = _measure(_fixture_doc())["steps"][0]
    assert pair["wall_s"] == 446.0 and pair["wall_s_reason"] is None, pair
    broken = _measure(_run_doc(steps=[{"n": 1, "rounds_detail": [
        {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "rounds": 1},
        {"seq": 2, "at": "unknown", "rounds": 1}]}]))["steps"][0]
    assert broken["wall_s"] == "unknown" and broken["wall_s_reason"] == "endpoint_unknown", broken
    no_detail = _measure(_run_doc(steps=[{"n": 1}]))["steps"][0]
    assert no_detail["wall_s"] == "unknown" and no_detail["wall_s_reason"] == "rounds_detail_missing", no_detail


# ---------------------------------------------------------------- r12


def _r12():
    doc = _run_doc(steps=[
        {"n": 1, "rounds_detail": [
            {"seq": 1, "at": "2026-10-26T20:48:14+08:00", "rounds": 1},
            {"seq": 2, "at": "2026-10-26T20:55:40+08:00", "rounds": 1}]},
        {"n": 2, "rounds_detail": [
            {"seq": 1, "at": "2026-10-26T21:05:00+08:00", "rounds": 1},
            {"seq": 2, "at": "2026-10-26T21:08:30+08:00", "rounds": 1}]},
    ])
    out = _measure(doc)
    assert out["steps"][0]["round_gap"]["n"] == 1, out["steps"][0]["round_gap"]
    assert out["steps"][1]["round_gap"]["n"] == 1, out["steps"][1]["round_gap"]
    assert out["round_gap"]["p50"] == "unknown" and out["round_gap"]["max"] == "unknown", \
        "两个 step 都出样本 → run 级不许合并分布"
    assert out["round_gap"]["round_gap_reason"] == "multi_step_no_aggregate", out["round_gap"]


# ---------------------------------------------------------------- r13


def _r13():
    tmp = tempfile.mkdtemp(prefix="measure-selftest-modal-")
    config = os.path.join(tmp, "opencode.json")
    base.write_text(config, base.dumps({
        "providers": {"p": {"models": {"m": {"capabilities": {"input": ["text"], "output": ["text"]}}}}}
    }))
    run_path = os.path.join(tmp, "run.json")
    base.write_text(run_path, base.dumps(_fixture_doc()))
    out_path = os.path.join(tmp, "measure.json")
    with _quiet():
        code = cli.main(["--run", run_path, "--out", out_path, "--config", config])
    assert code == 0, "exit 0，实际 %r" % (code,)
    with open(out_path, "r", encoding="utf-8", errors="replace") as handle:
        doc = json.load(handle)
    assert "modalities" in doc, "measure.json 必须带 modalities 块"
    by = {row["modality"]: row for row in doc["modalities"]}
    assert sorted(by) == ["audio", "image", "pdf", "text", "video"], sorted(by)
    assert by["text"]["status"] == "measured", by["text"]
    for name in ("image", "audio", "video", "pdf"):
        assert by[name]["status"] == "not_applicable", by[name]
        assert by[name]["reason"], "not_applicable 必须带 reason"
    assert by["image"]["evidence"] == 'capabilities.input = ["text"]', by["image"]["evidence"]
    assert doc["modalities_source"]["declared_inputs"] == ["text"], doc["modalities_source"]
    # 独立入口：--modalities --out 也要能产登记表
    reg_path = os.path.join(tmp, "modalities.json")
    with _quiet():
        code2 = cli.main(["--modalities", "--out", reg_path, "--config", config])
    assert code2 == 0, "exit 0，实际 %r" % (code2,)
    with open(reg_path, "r", encoding="utf-8", errors="replace") as handle:
        reg = json.load(handle)
    assert modalities.validate(reg) == [], modalities.validate(reg)


# ---------------------------------------------------------------- 出口


def run():
    check("r1 ttft_s/tool_calls 恒为 unknown（扫数字时跳过 otel_map）", _r1)
    check("r2 p50/p90/max 最近秩法 + n<5 → too_few_samples + 零依赖", _r2)
    check("r3 缺字段一律 unknown（不写 0/null），finished_at 缺 → wall_s unknown", _r3)
    check("r4 wall_s unknown 或 0 → out_tokens_per_wall_s unknown", _r4)
    check("r5 禁用词只按键名判定（degraded_reasons 不算 grade）", _r5)
    check("r6 同一份 run.json 连跑两次逐字节相同 + 输入目录不落文件", _r6)
    check("r7 模态登记表枚举合法、读不到 capabilities 时不猜", _r7)
    check("r8 --json 出口：恰好一个 JSON、ensure_ascii=False、indent=1、末尾一个换行", _r8)
    check("r9 夹具期望值：gap_s 首轮不占位、round_gap.n = 间隔条数", _r9)
    check("r10 too_few_samples 与 unknown_sample 分开；n=0 → 三个 null", _r10)
    check("r11 steps[].wall_s：无轮次 null、单轮 unknown + reason", _r11)
    check("r12 run 级 round_gap 不跨 step 聚合", _r12)
    check("r13 measure.json 必须带 modalities 块（每次读配置）", _r13)

    failed = 0
    for name, ok, detail in RESULTS:
        if ok:
            sys.stdout.write("PASS %s\n" % name)
        else:
            failed += 1
            sys.stdout.write("FAIL %s —— %s\n" % (name, detail))
    sys.stdout.write("-- %d/%d PASS\n" % (len(RESULTS) - failed, len(RESULTS)))
    sys.stdout.flush()
    return 0 if failed == 0 else 1
