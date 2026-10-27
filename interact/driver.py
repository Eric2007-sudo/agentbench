#!/usr/bin/env python3
"""interact/driver.py — 脚本化用户的多轮交互驱动。

一个 turn = 一次 `opencode run`：第 1 轮开新会话，后续轮用 `-s <sid>` 续**同一个**会话。
台词全部来自 `interact/scenarios/*.json`（写死，不由模型生成、不由模型扮演用户）。

账本单独记在 `interact/.plan/ledger.jsonl`，一行一个 turn；cost 取宿主库
`session_v2.cost` 的**增量**（真实值，不按均价乘 token）。本文件不写 bench 主账本
`.plan/ledger.jsonl`，也不调 `tg sync`。

`--model` 必须显式传 `deepseek/deepseek-flash`（本机 kouxian 中转站整组不可用，
不传模型会直接失败），默认值即该模型。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

# ---------------------------------------------------------------- 常量

ROOT = Path(__file__).resolve().parent.parent      # bench/
INTERACT = ROOT / "interact"
SCEN_DIR = INTERACT / "scenarios"
LEDGER = INTERACT / ".plan" / "ledger.jsonl"
OUT_DIR = INTERACT / "out"
RUN_SUMMARY = ROOT / "runs" / "fx-rework01" / "interact.json"

DEFAULT_MODEL = "deepseek/deepseek-flash"
PROVIDER_CHANGED = "kouxian → deepseek (direct)"
DISCLAIMER = "台词是编的，不代表真实用户"

DEFAULT_MAX_USD = 0.05
TURN_TIMEOUT_S = 900
SCENARIO_ORDER = ("clarify", "correct", "confirm")

TOKEN_KEYS = ("input", "output", "reasoning", "cache_read", "cache_write")
UNKNOWN = "unknown"

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_DATA_UNAVAILABLE = 3


class DriverError(Exception):
    """工具自身故障（参数、环境、写盘）。"""


class ScenarioError(DriverError):
    """场景文件不合法 —— 整场景不跑，不写空串。"""


# ---------------------------------------------------------------- 小工具

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(os.path.expanduser("~/.local/share/opencode/opencode.db"))


def _exe_from_wrapper(wrapper: Path):
    """opencode.cmd 里包的是真 exe，直接取它，避免 cmd 的引号转义。"""
    try:
        text = wrapper.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    base = str(wrapper.parent)
    text = text.replace("%~dp0", base).replace("%dp0%", base).replace("%dp0", base)
    for m in re.finditer(r'"([^"]*?\.exe)"', text, re.IGNORECASE):
        cand = Path(m.group(1))
        if cand.is_file():
            return cand
    return None


def resolve_opencode(explicit: str | None = None) -> list[str]:
    if explicit:
        return [explicit]
    if os.name == "nt":
        for name in ("opencode.exe", "opencode.cmd", "opencode.bat", "opencode"):
            found = shutil.which(name)
            if not found:
                continue
            low = found.lower()
            if low.endswith((".cmd", ".bat")):
                exe = _exe_from_wrapper(Path(found))
                if exe:
                    return [str(exe)]
                return [os.environ.get("COMSPEC", "cmd.exe"), "/c", found]
            return [found]
        raise DriverError("找不到 opencode 可执行文件（PATH 里没有 opencode）")
    found = shutil.which("opencode")
    if not found:
        raise DriverError("找不到 opencode 可执行文件（PATH 里没有 opencode）")
    return [found]


# ---------------------------------------------------------------- 场景

def load_scenario(path: Path) -> dict:
    if not path.is_file():
        raise ScenarioError(f"场景文件不存在：{path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if DISCLAIMER not in raw:
        raise ScenarioError(f"{path.name}: 文件头缺少「{DISCLAIMER}」")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{path.name}: 不是合法 JSON（{exc}）") from exc
    validate_scenario(data, path.stem)
    return data


def validate_scenario(sc: dict, stem: str) -> None:
    if not isinstance(sc, dict):
        raise ScenarioError("场景必须是 JSON 对象")
    if sc.get("id") != stem:
        raise ScenarioError(f"id 必须等于文件名（{stem}），实际 {sc.get('id')!r}")
    say = sc.get("say")
    if not isinstance(say, list) or not (2 <= len(say) <= 4):
        raise ScenarioError("say 必须是 2–4 轮的数组")
    for i, line in enumerate(say, start=1):
        if not isinstance(line, dict):
            raise ScenarioError(f"say[{i}] 必须是对象")
        text = line.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ScenarioError(f"say[{i}].text 不能为空（台词文件损坏 → 整场景不跑）")
        claim = line.get("expect_claim")
        if claim is not None and (not isinstance(claim, str) or not claim):
            raise ScenarioError(f"say[{i}].expect_claim 必须是非空字符串")
    mt = sc.get("max_turns", len(say))
    if not isinstance(mt, int) or not (2 <= mt <= 4):
        raise ScenarioError("max_turns 必须是 2–4 的整数")
    after = sc.get("done_after_turn", 1)
    if not isinstance(after, int) or not (1 <= after <= len(say)):
        raise ScenarioError("done_after_turn 必须落在 1..len(say)")
    checks = sc.get("checks", [])
    if not isinstance(checks, list) or not all(isinstance(c, str) for c in checks):
        raise ScenarioError("checks 必须是字符串数组")
    if sc.get("max_usd") is not None and not isinstance(sc["max_usd"], (int, float)):
        raise ScenarioError("max_usd 必须是数字")


def asked_for(sc: dict, turn: int) -> str:
    """turn 是 1 起的序号；台词原文照搬，不加工、不生成。"""
    say = sc["say"]
    if not (1 <= turn <= len(say)):
        raise ScenarioError(f"turn {turn} 不在台词范围 1..{len(say)}")
    return say[turn - 1]["text"]


# ---------------------------------------------------------------- 命令

def build_command(sc: dict, turn: int, sid: str, model: str, exe: list[str]) -> list[str]:
    message = asked_for(sc, turn)
    cmd = list(exe) + [
        "run",
        "--format", "json",
        "--model", model,
        "--auto",
    ]
    if sid and sid != UNKNOWN:
        cmd += ["--session", sid]
    else:
        cmd += ["--title", f"interact-{sc['id']}"]
    cmd.append(message)
    return cmd


def shell_join(cmd: list[str]) -> str:
    out = []
    for arg in cmd:
        out.append(f'"{arg}"' if (not arg or re.search(r'[\s"]', arg)) else arg)
    return " ".join(out)


# ---------------------------------------------------------------- 输出解析

def parse_events(stdout: str) -> tuple[str, str, str | None]:
    """从 `--format json` 的 JSONL 里取 sessionID 与助手文本。拿不到就是 unknown。"""
    sid = UNKNOWN
    texts: list[str] = []
    err = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        ev_sid = ev.get("sessionID")
        if isinstance(ev_sid, str) and ev_sid and sid == UNKNOWN:
            sid = ev_sid
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        part_sid = part.get("sessionID")
        if isinstance(part_sid, str) and part_sid and sid == UNKNOWN:
            sid = part_sid
        if ev.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        if ev.get("type") == "error":
            err = json.dumps(ev, ensure_ascii=False)[:500]
    return sid, "\n".join(texts), err


ASK_PATTERN = re.compile(
    r"[?？]"
    r"|请(你)?(告诉|发|贴|补充|确认|提供)"
    r"|需要你(补充|确认|告诉|提供)"
    r"|告诉我|发给我|贴过来|补充一下"
)


def asked_back(text: str) -> bool:
    """机械判「有没有反问」：问号，或明确的索要信息短语。

    只记录观测值，不参与 done 判定（done 只看产物断言与 expect_claim）。
    这是关键词判据，不是语义判断 —— 报告里必须把判据原文写出来。
    """
    return bool(ASK_PATTERN.search(text or ""))


def claim_ok(text: str, claim: str | None) -> bool:
    if not claim:
        return True
    return claim in (text or "")


# ---------------------------------------------------------------- 宿主库

def session_snapshot(sid: str) -> dict | None:
    """只读宿主库，返回该会话的累计 cost / tokens。拿不到 → None。"""
    if not sid or sid == UNKNOWN:
        return None
    path = db_path()
    if not path.is_file():
        return None
    uri = "file:" + path.as_posix() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            row = con.execute(
                "select cost, tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write"
                " from session_v2 where id = ?",
                (sid,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    keys = ("cost",) + TOKEN_KEYS
    return dict(zip(keys, row))


def zero_snapshot() -> dict:
    snap = {k: 0 for k in TOKEN_KEYS}
    snap["cost"] = 0.0
    return snap


def turn_usage(prev: dict | None, cur: dict | None) -> tuple[dict, object]:
    """本轮用量 = 会话累计增量。任何一环缺 → 该字段 unknown，不估算。"""
    tokens = {}
    for key in TOKEN_KEYS:
        if prev is None or cur is None:
            tokens[key] = UNKNOWN
            continue
        a, b = prev.get(key), cur.get(key)
        if a is None or b is None:
            tokens[key] = UNKNOWN
        else:
            tokens[key] = int(b) - int(a)
    tokens["complete"] = all(isinstance(tokens[k], int) for k in TOKEN_KEYS)

    cost: object = UNKNOWN
    if prev is not None and cur is not None:
        a, b = prev.get("cost"), cur.get("cost")
        if a is not None and b is not None:
            delta = float(b) - float(a)
            if delta >= 0:
                cost = round(delta, 9)
    return tokens, cost


def numeric_spend(values) -> float:
    return round(sum(v for v in values if isinstance(v, (int, float))), 9)


# ---------------------------------------------------------------- 账本

def ledger_rows() -> list[dict]:
    if not LEDGER.is_file():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def scenario_spent(scenario_id: str) -> float:
    """本场景此前已花的钱（累计口径，含之前几次运行）。unknown 行不计入，只是下限。"""
    costs = [r.get("cost") for r in ledger_rows() if r.get("scenario") == scenario_id]
    return numeric_spend(costs)


def append_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 机械判分

def eval_check(expr: str, workdir: Path) -> tuple[bool, str]:
    if expr.startswith("file:exists:"):
        rel = expr[len("file:exists:"):]
        ok = (workdir / rel).is_file()
        return ok, "" if ok else f"不存在：{rel}"
    if expr.startswith("file:absent:"):
        rel = expr[len("file:absent:"):]
        ok = not (workdir / rel).exists()
        return ok, "" if ok else f"仍存在：{rel}"
    for prefix, want in (("grep:", True), ("nogrep:", False)):
        if expr.startswith(prefix):
            body = expr[len(prefix):]
            if "@" not in body:
                return False, "断言缺少 @FILE"
            pattern, rel = body.rsplit("@", 1)
            path = workdir / rel
            if not path.is_file():
                return False, f"文件不存在：{rel}"
            text = path.read_text(encoding="utf-8", errors="replace")
            hit = pattern in text
            ok = hit if want else not hit
            return ok, "" if ok else f"{pattern!r} {'未命中' if want else '不该出现'}"
    return False, f"无法识别的断言语法：{expr}"


def evaluate_checks(sc: dict, workdir: Path) -> list[dict]:
    items = []
    for expr in sc.get("checks", []):
        ok, detail = eval_check(expr, workdir)
        items.append({"expr": expr, "result": "pass" if ok else "fail", "detail": detail})
    return items


def done_at_turn(sc: dict, turn: int, turns: list[dict], workdir: Path) -> tuple[bool, list[dict]]:
    """turn 这一轮结束时是否算做完：产物断言全绿 + 已发各轮 expect_claim 全中 + 过了 done_after_turn。"""
    checks = evaluate_checks(sc, workdir)
    if turn < sc.get("done_after_turn", 1):
        return False, checks
    if not all(c["result"] == "pass" for c in checks):
        return False, checks
    for item in turns:
        claim = sc["say"][item["turn"] - 1].get("expect_claim")
        if not claim_ok(item.get("response", ""), claim):
            return False, checks
    return True, checks


def budget_exhausted(spent: float, max_usd: float) -> bool:
    return spent >= max_usd


# ---------------------------------------------------------------- 跑一个场景

def _reset_workdir(workdir: Path) -> None:
    """每轮开跑前清空沙箱，保证断言判的是本次运行的产物，不是上一轮的残留。"""
    workdir = workdir.resolve()
    if INTERACT.resolve() not in workdir.parents:
        raise DriverError(f"拒绝清理 interact/ 之外的目录：{workdir}")
    workdir.mkdir(parents=True, exist_ok=True)
    for child in workdir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def run_scenario(
    sc: dict,
    *,
    model: str,
    max_usd: float,
    out_path: Path,
    exe: list[str],
    write_summary: bool = True,
) -> dict:
    workdir = OUT_DIR / sc["id"] / "work"
    _reset_workdir(workdir)

    spent_before = scenario_spent(sc["id"])
    turns: list[dict] = []
    prev = zero_snapshot()
    sid = UNKNOWN
    done_turn: int | None = None
    stop_reason = "turns_exhausted"
    checks: list[dict] = []
    started_at = now_iso()

    limit = min(int(sc.get("max_turns", len(sc["say"]))), len(sc["say"]))

    for turn in range(1, limit + 1):
        spent_now = round(spent_before + numeric_spend(t["cost"] for t in turns), 9)
        if budget_exhausted(spent_now, max_usd):
            stop_reason = "budget"
            print(f"[budget] 已花 {spent_now} >= 上限 {max_usd}，不派第 {turn} 轮", file=sys.stderr)
            break
        if turn > 1 and sid == UNKNOWN:
            stop_reason = "sid_unknown"
            print("[sid] 上一轮拿不到 sid，无法续会话，停止", file=sys.stderr)
            break

        cmd = build_command(sc, turn, sid, model, exe)
        asked = asked_for(sc, turn)
        print(f"[turn {turn}] {shell_join(cmd)}", file=sys.stderr)

        t0 = time.monotonic()
        error = None
        stdout = ""
        stderr = ""
        returncode = None
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TURN_TIMEOUT_S,
                env=env,
            )
            returncode = proc.returncode
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            returncode = None
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            error = f"timeout after {TURN_TIMEOUT_S}s"
        except OSError as exc:
            error = f"spawn failed: {exc}"
        wall_s = round(time.monotonic() - t0, 6)

        parsed_sid, text, ev_err = parse_events(stdout)
        if parsed_sid != UNKNOWN:
            if sid == UNKNOWN:
                sid = parsed_sid
            elif parsed_sid != sid:
                error = (error + "; " if error else "") + f"sid 变了：{sid} -> {parsed_sid}"
        if ev_err:
            error = (error + "; " if error else "") + ev_err
        if returncode not in (0, None) and not error:
            error = f"exit={returncode}: " + (stderr.strip().splitlines()[-1] if stderr.strip() else "")

        snapshot = session_snapshot(sid)
        base = zero_snapshot() if turn == 1 else prev
        tokens, cost = turn_usage(base, snapshot)
        prev = snapshot  # 拿不到就是 None：后面一律 unknown，不许编

        record = {
            "turn": turn,
            "asked": asked,
            "wall_s": wall_s,
            "tokens": tokens,
            "cost": cost,
            "sid": sid,
            "response": text,
            "asked_back": asked_back(text),
            "error": error,
        }
        if sc["say"][turn - 1].get("clarify"):
            record["clarify"] = True
        turns.append(record)

        append_ledger({
            "ts": now_iso(),
            "scenario": sc["id"],
            "turn": turn,
            "sid": sid,
            "tokens": tokens,
            "cost": cost,
            "src": "host_db",
        })

        is_done, checks = done_at_turn(sc, turn, turns, workdir)
        print(
            f"[turn {turn}] sid={sid} wall={wall_s}s cost={cost}"
            f" asked_back={record['asked_back']}"
            + (f" error={error}" if error else ""),
            file=sys.stderr,
        )
        if is_done:
            done_turn = turn
            stop_reason = "done"
            break

    consumed = len(turns)
    clarify_turns = sum(1 for t in turns if sc["say"][t["turn"] - 1].get("clarify"))
    checks = evaluate_checks(sc, workdir)

    if sid == UNKNOWN:
        verdict = UNKNOWN
        turns_to_done: object = UNKNOWN
    elif done_turn is not None:
        verdict = "done"
        turns_to_done = done_turn
    else:
        verdict = "not_done"
        turns_to_done = UNKNOWN

    stopped_early = consumed < len(sc["say"])

    result = {
        "schema": 1,
        "scenario": sc["id"],
        "title": sc.get("title", ""),
        "model": model,
        "started_at": started_at,
        "finished_at": now_iso(),
        "max_usd": max_usd,
        "spent_usd": round(spent_before + numeric_spend(t["cost"] for t in turns), 9),
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "turns_to_done": turns_to_done,
        "clarify_turns": clarify_turns,
        "verdict": verdict,
        "checks": checks,
        "turns": turns,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[out] {out_path}", file=sys.stderr)

    if write_summary:
        entry = {
            "id": sc["id"],
            "turns_to_done": turns_to_done,
            "clarify_turns": clarify_turns,
            "stopped_early": stopped_early,
            "verdict": verdict,
            "turns": [
                {
                    "turn": t["turn"],
                    "asked": t["asked"],
                    "wall_s": t["wall_s"],
                    "cost": t["cost"],
                    "sid": t["sid"],
                }
                for t in turns
            ],
        }
        update_run_summary(entry)
        print(f"[summary] {RUN_SUMMARY}", file=sys.stderr)

    return result


# ---------------------------------------------------------------- 传送带

def _scenario_cost(entry: dict) -> object:
    costs = [t.get("cost", UNKNOWN) for t in entry.get("turns", [])]
    if not costs or any(c == UNKNOWN for c in costs):
        return UNKNOWN
    return round(numeric_spend(costs), 9)


def update_run_summary(entry: dict) -> None:
    data = {"schema": 1, "scenarios": [], "cost": 0.0, "provider_changed": PROVIDER_CHANGED}
    if RUN_SUMMARY.is_file():
        try:
            old = json.loads(RUN_SUMMARY.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("scenarios"), list):
                data = {
                    "schema": 1,
                    "scenarios": old["scenarios"],
                    "cost": old.get("cost", UNKNOWN),
                    "provider_changed": PROVIDER_CHANGED,
                }
        except (json.JSONDecodeError, OSError):
            pass

    scenarios = [s for s in data["scenarios"] if s.get("id") != entry["id"]]
    scenarios.append(entry)
    scenarios.sort(
        key=lambda s: SCENARIO_ORDER.index(s["id"]) if s.get("id") in SCENARIO_ORDER else len(SCENARIO_ORDER)
    )
    data["scenarios"] = scenarios

    totals = [_scenario_cost(s) for s in scenarios]
    data["cost"] = UNKNOWN if any(t == UNKNOWN for t in totals) else round(numeric_spend(totals), 9)
    data["provider_changed"] = PROVIDER_CHANGED

    RUN_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RUN_SUMMARY.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- dry-run

def scenario_paths(ids: list[str] | None) -> list[Path]:
    if ids:
        return [SCEN_DIR / f"{i}.json" for i in ids]
    return sorted(SCEN_DIR.glob("*.json"))


def dry_run(ids: list[str] | None, model: str, max_usd: float | None, exe: list[str]) -> int:
    paths = scenario_paths(ids)
    if not paths:
        print("没有找到任何场景文件", file=sys.stderr)
        return EXIT_TOOL_ERROR
    print(f"[dry-run] model={model}  (不调模型，只打印命令与校验结果)")
    ok = True
    for path in paths:
        try:
            sc = load_scenario(path)
        except DriverError as exc:
            print(f"[dry-run] {path.name}: FAIL - {exc}", file=sys.stderr)
            ok = False
            continue
        cap = max_usd if max_usd is not None else float(sc.get("max_usd", DEFAULT_MAX_USD))
        workdir = OUT_DIR / sc["id"] / "work"
        print(f"[dry-run] {sc['id']}: ok  ({len(sc['say'])} 轮台词, max_turns={sc.get('max_turns', len(sc['say']))},"
              f" done_after_turn={sc.get('done_after_turn', 1)}, 上限 ${cap})")
        print(f"[dry-run]   workdir={workdir.as_posix()}")
        print(f"[dry-run]   checks={sc.get('checks', [])}")
        sid_placeholder = "<第 1 轮拿到的 sid>"
        for turn in range(1, min(int(sc.get("max_turns", len(sc['say']))), len(sc["say"])) + 1):
            fake_sid = UNKNOWN if turn == 1 else sid_placeholder
            cmd = build_command(sc, turn, fake_sid, model, exe)
            print(f"[dry-run]   turn {turn}: {shell_join(cmd)}")
    return EXIT_OK if ok else EXIT_TOOL_ERROR


# ---------------------------------------------------------------- self-test

def self_test() -> int:
    cases: list[tuple[str, object]] = []

    def add(name: str, fn):
        try:
            detail = fn()
            cases.append((name, detail if detail is not None else True))
        except Exception as exc:  # noqa: BLE001 - self-test 要把失败抓出来变成 FAIL
            cases.append((name, f"{type(exc).__name__}: {exc}"))

    def case_scenarios_parse():
        paths = sorted(SCEN_DIR.glob("*.json"))
        ids = []
        for p in paths:
            sc = load_scenario(p)
            ids.append(sc["id"])
        missing = [i for i in SCENARIO_ORDER if i not in ids]
        if missing:
            raise AssertionError(f"缺少场景：{missing}")
        return f"{len(ids)} 个场景：{ids}"

    def case_empty_line_rejected():
        bad = {
            "id": "tmp",
            "say": [{"text": "x"}, {"text": "   "}],
            "max_turns": 2,
        }
        try:
            validate_scenario(bad, "tmp")
        except ScenarioError:
            return "空台词被拒绝"
        raise AssertionError("空台词没有被拒绝")

    def case_disclaimer_required():
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tmp.json"
            p.write_text(json.dumps({"id": "tmp", "say": [{"text": "a"}, {"text": "b"}]}), encoding="utf-8")
            try:
                load_scenario(p)
            except ScenarioError:
                return "缺免责声明被拒绝"
            raise AssertionError("缺免责声明没有被拒绝")

    def case_budget():
        if budget_exhausted(0.049, 0.05):
            raise AssertionError("0.049 不应触发刹车")
        if not budget_exhausted(0.05, 0.05):
            raise AssertionError("0.05 应触发刹车")
        if not budget_exhausted(0.2, 0.05):
            raise AssertionError("0.2 应触发刹车")
        return "累计口径：>= max_usd 就不再派下一轮"

    def case_sid_unknown():
        sid, text, _ = parse_events("")
        if sid != UNKNOWN:
            raise AssertionError(f"空输出应是 unknown，得到 {sid!r}")
        sid2, _, _ = parse_events('{"type":"step_start","sessionID":"ses_abc"}')
        if sid2 != "ses_abc":
            raise AssertionError("应能从事件里取 sessionID")
        # sid 拿不到 → verdict 也是 unknown，不伪造
        fake_sid = UNKNOWN
        verdict = UNKNOWN if fake_sid == UNKNOWN else "done"
        if verdict != UNKNOWN:
            raise AssertionError("sid 缺失时 verdict 必须是 unknown")
        return "sid 缺失 → unknown（不拿时间戳/会话数自造）"

    def case_asked_verbatim():
        sc = load_scenario(SCEN_DIR / "clarify.json")
        for turn, line in enumerate(sc["say"], start=1):
            if asked_for(sc, turn) != line["text"]:
                raise AssertionError(f"turn {turn} 的 asked 与台词不一致")
            if not asked_for(sc, turn).strip():
                raise AssertionError("asked 不能是空串")
        return "asked 逐字等于 scenarios/*.json 的台词"

    def case_checks():
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "a.txt").write_text("hello\n", encoding="utf-8")
            assert eval_check("file:exists:a.txt", wd)[0] is True
            assert eval_check("file:exists:b.txt", wd)[0] is False
            assert eval_check("file:absent:b.txt", wd)[0] is True
            assert eval_check("file:absent:a.txt", wd)[0] is False
            assert eval_check("grep:hello@a.txt", wd)[0] is True
            assert eval_check("grep:nope@a.txt", wd)[0] is False
            assert eval_check("nogrep:nope@a.txt", wd)[0] is True
            assert eval_check("nogrep:hello@a.txt", wd)[0] is False
            assert eval_check("wat:hello@a.txt", wd)[0] is False
        return "6 种前缀的 4 种产物断言逐条判定"

    def case_cost_unknown():
        tokens, cost = turn_usage(zero_snapshot(), None)
        if cost != UNKNOWN:
            raise AssertionError("宿主库拿不到 cost 时必须 unknown")
        if any(tokens[k] != UNKNOWN for k in TOKEN_KEYS):
            raise AssertionError("宿主库拿不到 token 时必须 unknown")
        prev = zero_snapshot()
        cur = {"cost": 0.0012, "input": 10, "output": 2, "reasoning": 0, "cache_read": 3, "cache_write": 0}
        tokens2, cost2 = turn_usage(prev, cur)
        if cost2 != 0.0012 or tokens2["input"] != 10 or tokens2["complete"] is not True:
            raise AssertionError(f"增量计算错误：{tokens2} {cost2}")
        tokens3, cost3 = turn_usage(None, cur)
        if cost3 != UNKNOWN or tokens3["input"] != UNKNOWN:
            raise AssertionError("上一轮累计缺失时本轮必须是 unknown，不许拿累计当增量")
        return "cost 缺失 → unknown；有宿主值 → 增量（不按均价乘 token）"

    def case_no_force():
        parser = build_parser()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
                parser.parse_args(["--scenario", "clarify", "--force"])
        except SystemExit:
            return "没有 --force（预算刹车不可绕过）"
        raise AssertionError("解析器接受了 --force")

    def case_command_has_model():
        sc = load_scenario(SCEN_DIR / "clarify.json")
        cmd = build_command(sc, 1, UNKNOWN, DEFAULT_MODEL, ["opencode"])
        if "--model" not in cmd or cmd[cmd.index("--model") + 1] != DEFAULT_MODEL:
            raise AssertionError(f"命令缺少显式模型：{cmd}")
        if "--session" in cmd:
            raise AssertionError("第 1 轮不应带 --session")
        cmd2 = build_command(sc, 2, "ses_abc", DEFAULT_MODEL, ["opencode"])
        if cmd2[cmd2.index("--session") + 1] != "ses_abc":
            raise AssertionError("第 2 轮应 -s 续同一会话")
        return "第 1 轮新会话、后续轮 --session <sid>、显式 --model"

    def case_summary_shape():
        with tempfile.TemporaryDirectory() as td:
            global RUN_SUMMARY
            saved, RUN_SUMMARY = RUN_SUMMARY, Path(td) / "interact.json"
            try:
                update_run_summary({
                    "id": "clarify",
                    "turns_to_done": 3,
                    "clarify_turns": 1,
                    "stopped_early": True,
                    "verdict": "done",
                    "turns": [{"turn": 1, "asked": "x", "wall_s": 1.0, "cost": 0.001, "sid": "ses_a"}],
                })
                data = json.loads(RUN_SUMMARY.read_text(encoding="utf-8"))
            finally:
                RUN_SUMMARY = saved
        if data["schema"] != 1 or "scenarios" not in data or data["cost"] != 0.001:
            raise AssertionError(f"汇总形状不对：{data}")
        keys = set(data["scenarios"][0])
        expect = {"id", "turns_to_done", "clarify_turns", "stopped_early", "verdict", "turns"}
        if not expect <= keys:
            raise AssertionError(f"缺字段：{expect - keys}")
        return "传送带形状：schema/scenarios/cost/provider_changed"

    add("scenarios.parse", case_scenarios_parse)
    add("scenario.empty_line_rejected", case_empty_line_rejected)
    add("scenario.disclaimer_required", case_disclaimer_required)
    add("budget.cumulative", case_budget)
    add("sid.missing_is_unknown", case_sid_unknown)
    add("asked.verbatim", case_asked_verbatim)
    add("checks.prefixes", case_checks)
    add("cost.unknown_not_estimated", case_cost_unknown)
    add("cli.no_force", case_no_force)
    add("cmd.model_and_session", case_command_has_model)
    add("summary.shape", case_summary_shape)

    failed = 0
    for name, detail in cases:
        ok = detail is True or isinstance(detail, str)
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\nself-test: {len(cases) - failed}/{len(cases)} pass")
    return EXIT_OK if failed == 0 else EXIT_TOOL_ERROR


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m interact.driver",
        description="脚本化用户的多轮交互驱动（一个 turn = 一次 opencode run）",
    )
    p.add_argument("--scenario", help="场景 id（interact/scenarios/<id>.json）")
    p.add_argument("--all", action="store_true", help="按 clarify/correct/confirm 顺序跑全部场景")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"显式传给 opencode run（默认 {DEFAULT_MODEL}）")
    p.add_argument("--max-usd", type=float, default=None, help="累计预算上限（含本场景此前已花的）")
    p.add_argument("--out", help="场景明细落盘路径（默认 interact/out/<id>.json）")
    p.add_argument("--dry-run", action="store_true", help="不调模型，只打印命令与场景校验结果")
    p.add_argument("--self-test", action="store_true", help="跑内置用例")
    p.add_argument("--no-summary", action="store_true", help="不写 runs/fx-rework01/interact.json")
    p.add_argument("--opencode", help="opencode 可执行文件路径（默认从 PATH 找）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.self_test:
        return self_test()

    try:
        exe = resolve_opencode(args.opencode)
    except DriverError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    if args.dry_run:
        ids = [args.scenario] if args.scenario else None
        return dry_run(ids, args.model, args.max_usd, exe)

    if not args.scenario and not args.all:
        print("[error] 需要 --scenario <id> 或 --all（或用 --dry-run / --self-test）", file=sys.stderr)
        return EXIT_TOOL_ERROR

    ids = SCENARIO_ORDER if args.all else [args.scenario]
    worst = EXIT_OK
    for scenario_id in ids:
        try:
            sc = load_scenario(SCEN_DIR / f"{scenario_id}.json")
        except DriverError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            worst = EXIT_TOOL_ERROR
            continue
        cap = args.max_usd if args.max_usd is not None else float(sc.get("max_usd", DEFAULT_MAX_USD))
        if args.out and len(ids) == 1:
            out_path = Path(args.out)
        else:
            out_path = OUT_DIR / f"{scenario_id}.json"
        try:
            result = run_scenario(
                sc,
                model=args.model,
                max_usd=cap,
                out_path=out_path,
                exe=exe,
                write_summary=not args.no_summary,
            )
        except DriverError as exc:
            print(f"[error] {scenario_id}: {exc}", file=sys.stderr)
            worst = EXIT_TOOL_ERROR
            continue
        print(f"[{scenario_id}] verdict={result['verdict']} turns_to_done={result['turns_to_done']}"
              f" clarify_turns={result['clarify_turns']} stopped_early={result['stopped_early']}"
              f" spent=${result['spent_usd']}")
        if result["verdict"] == UNKNOWN and worst == EXIT_OK:
            worst = EXIT_DATA_UNAVAILABLE
    return worst


if __name__ == "__main__":
    sys.exit(main())
