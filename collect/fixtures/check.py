"""夹具自检 —— `python -m collect.fixtures.check`

只做一件事：把 `collect/fixtures/rework01/` 与 `collect/fixtures/rework01b/` 按
`slices.md` §4.1 / §4.2 写死的期望值核一遍（**纯读**，不写任何文件，不 import 别的子包）。

口径来源：`contract/trace.md`（§3.1 来源字段、§5.2 返工、§5.4 成本与轮次）与
`contract/slices.md` §4.1 / §4.2 / §5（断言 6 种前缀）。

退出码：0 = 全部符合；1 = 有不符合（明细走 stderr）。
"""

import sys

sys.dont_write_bytecode = True

import hashlib
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
A = FIXTURES / "rework01"
B = FIXTURES / "rework01b"

# slices.md §5：只允许这 6 种前缀
VALID_PREFIXES = ("file:exists:", "file:absent:", "grep:", "nogrep:", "cmd:", "ack:")

# slices.md §4.1 写死的期望值（示意数据）
EXPECT_COST_STEP1 = 0.0042 + 0.0049          # rework01 前两行都有 cost
EXPECT_COST_B_ROW3 = 0.0031                  # rework01b 第 3 行补上的 cost
EXPECT_REQS_SUM = 3                          # 3 条带 reqs 的行，每行 1 轮

REQUIRED = (
    ".plan/plan.json",
    ".plan/ledger.jsonl",
    ".plan/gates.log",
    "artifacts/slices.md",
    "artifacts/plan_dir.py",
)

FAILS = []
COUNT = [0]


def ok(name, cond, detail=""):
    COUNT[0] += 1
    if cond:
        print("  PASS  %s" % name)
    else:
        FAILS.append("%s%s" % (name, ("  " + detail) if detail else ""))
        print("  FAIL  %s%s" % (name, ("  " + detail) if detail else ""))


def eq(name, got, want):
    ok(name, got == want, "got=%r want=%r" % (got, want))


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_ledger(fx):
    """按行读账本：返回 (ok_rows, bad_line_numbers, physical_line_count)。"""
    rows, bad, physical = [], [], 0
    for lineno, line in enumerate(read_text(fx / ".plan" / "ledger.jsonl").split("\n"), 1):
        if line == "" and lineno == 1 and physical == 0:
            continue  # 空文件兜底
        if line == "":
            continue
        physical = lineno
        if line.strip() == "":
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            bad.append(lineno)
    return rows, bad, physical


def parse_gates(fx):
    """把 gates.log 切块：每块 = 一个 gate 的 header + 断言结果行。

    header 形如 `[2026-10-26T20:55:40] gate #1 写契约`；
    结果行形如 `  PASS  file:exists:contract/trace.md`；
    更深的缩进行（`文件不存在: ...` / `exit=0`）是明细，不算结果。
    """
    blocks = []
    for line in read_text(fx / ".plan" / "gates.log").split("\n"):
        if line.startswith("["):
            head = line.split("]", 1)[1].strip()
            step = None
            for token in head.split():
                if token.startswith("#"):
                    try:
                        step = int(token[1:])
                    except ValueError:
                        step = None
                    break
            blocks.append({"header": line, "step": step, "results": [], "details": []})
            continue
        stripped = line.strip()
        if not blocks:
            continue
        for verdict in ("PASS", "FAIL", "TIMEOUT"):
            if stripped.startswith(verdict + " "):
                expr = stripped[len(verdict):].strip()
                blocks[-1]["results"].append((verdict, expr))
                break
        else:
            if stripped and not stripped.startswith("["):
                blocks[-1]["details"].append(stripped)
    return blocks


def step_blocks(blocks, step):
    return [b for b in blocks if b["step"] == step]


def main():
    print("collect/fixtures/check —— 夹具自检")
    print("[1] 文件齐不齐（slices.md §4.1 / §4.2 的四件套）")
    for fx in (A, B):
        for rel in REQUIRED:
            p = fx / rel
            ok("%s/%s 存在" % (fx.name, rel), p.is_file())

    print("[2] 来源目录里没有采集器留下的东西（只读约束的机械证据）")
    for fx in (A, B):
        ok("%s/.plan/run.json 不存在" % fx.name, not (fx / ".plan" / "run.json").exists())
        strays = sorted(
            p.name for p in (fx / ".plan").iterdir()
            if p.name not in ("plan.json", "ledger.jsonl", "gates.log")
        )
        eq("%s/.plan/ 只有三件来源（无临时文件/无 __pycache__）" % fx.name, strays, [])

    print("[3] plan.json：2 步、n 升序、accept 只用 6 种前缀、两份同形")
    plans = {}
    for fx in (A, B):
        plan = json.loads(read_text(fx / ".plan" / "plan.json"))
        plans[fx.name] = plan
        eq("%s plan.schema" % fx.name, plan.get("schema"), 1)
        steps = plan.get("steps", [])
        eq("%s 步数" % fx.name, len(steps), 2)
        eq("%s 步号升序" % fx.name, [s.get("n") for s in steps], [1, 2])
        bad_prefix = [
            expr for s in steps for expr in s.get("accept", [])
            if not str(expr).startswith(VALID_PREFIXES)
        ]
        eq("%s accept 全是合法前缀" % fx.name, bad_prefix, [])
        eq(
            "%s 每步 accept 条数 2~4" % fx.name,
            [2 <= len(s.get("accept", [])) <= 4 for s in steps],
            [True, True],
        )
    eq(
        "两份 plan.json 字节一致（§4.2：只允许 gates.log 与 ledger 第 3 行不同）",
        sha256_of(A / ".plan" / "plan.json"),
        sha256_of(B / ".plan" / "plan.json"),
    )

    print("[4] ledger.jsonl：坏行只坏第 4 行、前 3 行可用（trace.md §8.2）")
    for fx in (A, B):
        rows, bad, physical = read_ledger(fx)
        eq("%s 物理行数" % fx.name, physical, 4)
        eq("%s 可用行数" % fx.name, len(rows), 3)
        eq("%s 坏行行号" % fx.name, bad, [4])
        eq("%s 各行 step" % fx.name, [r.get("step") for r in rows], [1, 1, 2])
        eq(
            "%s rows_with_reqs / rows_total（§5.4）" % fx.name,
            (len([r for r in rows if r.get("reqs")]), physical),
            (3, 4),
        )
        eq(
            "%s Σ reqs = 轮次" % fx.name,
            sum(r.get("reqs", 0) for r in rows),
            EXPECT_REQS_SUM,
        )
        line4 = read_text(fx / ".plan" / "ledger.jsonl").split("\n")[3]
        ok("%s 第 4 行不是空行（确实是坏 JSON，不是漏写）" % fx.name, line4.strip() != "")
        try:
            json.loads(line4)
            parses = True
        except ValueError:
            parses = False
        eq("%s 第 4 行解析失败" % fx.name, parses, False)
        for idx, row in enumerate(rows, 1):
            ok(
                "%s 第 %d 条可用行（step=%r）有 ts / in / out"
                % (fx.name, idx, row.get("step")),
                isinstance(row.get("ts"), str) and isinstance(row.get("in"), int)
                and isinstance(row.get("out"), int),
            )

    print("[5] 成本：rework01 是下限（缺 1 行），rework01b 补齐后变上界（trace.md §5.4）")
    rows_a, _, _ = read_ledger(A)
    rows_b, _, _ = read_ledger(B)
    cost_a = [r.get("cost") for r in rows_a]
    cost_b = [r.get("cost") for r in rows_b]
    eq("rework01 已知 cost 行数", len([c for c in cost_a if isinstance(c, (int, float))]), 2)
    eq("rework01 unknown_rows", len([c for c in cost_a if c is None]), 1)
    eq("rework01 cost.usd（已知部分之和 = 下限）", round(sum(c for c in cost_a if c is not None), 6), round(EXPECT_COST_STEP1, 6))
    eq("rework01 cost.complete", all(c is not None for c in cost_a), False)
    eq("rework01b 每一行都有 cost", all(isinstance(c, (int, float)) for c in cost_b), True)
    eq("rework01b cost.complete", all(c is not None for c in cost_b), True)
    eq(
        "rework01b 第 3 行补上的 cost",
        rows_b[2].get("cost"),
        EXPECT_COST_B_ROW3,
    )
    delta = round(sum(cost_b) - sum(c for c in cost_a if c is not None), 6)
    eq("回归对比的 cost.delta_usd > 0（slices.md §4.2）", delta, round(EXPECT_COST_B_ROW3, 6))
    ok("cost.delta_usd 是正数", delta > 0, "delta=%r" % delta)
    eq(
        "两份 ledger 除第 3 行的 cost 外逐行一致（§4.2：只改一处）",
        [
            {k: v for k, v in ra.items() if k != "cost"}
            for ra in rows_a
        ],
        [
            {k: v for k, v in rb.items() if k != "cost"}
            for rb in rows_b
        ],
    )
    eq(
        "rework01 第 3 行确实没有 cost 字段",
        "cost" in rows_a[2],
        False,
    )

    print("[6] gates.log：rework01 有返工、rework01b 返工消失（trace.md §5.2）")
    gates_a, gates_b = parse_gates(A), parse_gates(B)
    s1_a, s1_b = step_blocks(gates_a, 1), step_blocks(gates_b, 1)
    s2_a, s2_b = step_blocks(gates_a, 2), step_blocks(gates_b, 2)
    eq("rework01 步 1 的 gate 次数", len(s1_a), 2)
    eq("rework01b 步 1 的 gate 次数", len(s1_b), 1)
    eq(
        "rework01 第一次 gate 有 FAIL（首过率 0.5 的来源）",
        [v for v, _ in s1_a[0]["results"]],
        ["FAIL", "PASS"],
    )
    eq(
        "rework01 第二次 gate 全 PASS（final_pass_rate 1.0）",
        [v for v, _ in s1_a[1]["results"]],
        ["PASS", "PASS"],
    )
    eq(
        "rework01b 唯一一次 gate 全 PASS（返工消失）",
        [v for v, _ in s1_b[0]["results"]],
        ["PASS", "PASS"],
    )
    eq(
        "同一批断言：两次 gate 的 expr 集合相同（返工只改结果不改条目）",
        set(e for _, e in s1_a[0]["results"]),
        set(e for _, e in s1_a[1]["results"]),
    )
    eq(
        "rework01 的 expr 集合 == plan 步 1 的 accept（trace.md §5.2 分母口径）",
        sorted(set(e for _, e in s1_a[1]["results"])),
        sorted(plans[A.name]["steps"][0]["accept"]),
    )
    eq(
        "rework01b 的 expr 集合 == plan 步 1 的 accept",
        sorted(set(e for _, e in s1_b[0]["results"])),
        sorted(plans[B.name]["steps"][0]["accept"]),
    )
    eq("两份夹具的步 2 gate 次数都是 1", (len(s2_a), len(s2_b)), (1, 1))
    eq(
        "两份夹具的步 2 gate 完全相同（§4.2：只有两处不同）",
        [(v, e) for v, e in s2_a[0]["results"]],
        [(v, e) for v, e in s2_b[0]["results"]],
    )
    ok(
        "步 2 的 cmd: 断言明细里有 exit=N（trace.md §5.1 exit_code_src=gate）",
        any("exit=" in d for d in s2_a[0]["details"]),
        "details=%r" % (s2_a[0]["details"],),
    )

    print("[7] 两份夹具树只差 .plan/ledger.jsonl 与 .plan/gates.log（§4.2 可回归的前提）")
    tree_a = {
        p.relative_to(A).as_posix(): sha256_of(p)
        for p in A.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    }
    tree_b = {
        p.relative_to(B).as_posix(): sha256_of(p)
        for p in B.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    }
    eq("两份夹具的相对路径集合相同", sorted(tree_a), sorted(tree_b))
    eq(
        "内容不同的文件恰好是这两个",
        sorted(k for k in tree_a if tree_a.get(k) != tree_b.get(k)),
        [".plan/gates.log", ".plan/ledger.jsonl"],
    )

    print("")
    if FAILS:
        for line in FAILS:
            print("check: %s" % line, file=sys.stderr)
        print("check: %d/%d 条不符" % (len(FAILS), COUNT[0]), file=sys.stderr)
        return 1
    print("check: %d/%d 全绿" % (COUNT[0], COUNT[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
