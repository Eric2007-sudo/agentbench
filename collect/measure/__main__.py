"""`python -m collect.measure` —— 性能五标量 + 模态登记 CLI（零依赖、纯标准库）。

四种用法（简报「一、CLI」）：

① `--run <run.json> --out <measure.json>`：读 run.json + 同名目录 `.plan/ledger.jsonl` + 宿主库，落 `measure.json`；
② `--json`：stdout **恰好一个** JSON（UTF-8 无 BOM、`ensure_ascii=False`、`indent=1`、末尾一个 `\\n`），
   人话全走 stderr；出错时 stdout 仍是纯 JSON `{"ok": false, "error": {...}}` 且非 0 退出；
③ `--self-test`：内置用例覆盖每条硬规矩，一条一行 `PASS`/`FAIL`，全过 exit 0；
④ `--modalities --out <path>`：读 `~/.config/opencode/opencode.json` 的
   `providers.*.models.*.capabilities.input` 生成模态登记表；读不到就整表记
   `not_applicable` + `unknown_reasons`，不许猜（status 枚举只有 measured / not_applicable）。
"""

import sys

sys.dont_write_bytecode = True

import argparse
import json
import os

from . import base, modalities, perf

EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_INTERNAL = 1


# ---------------------------------------------------------------- 输出通道


def _err(text):
    """人话一律走 stderr（trace.md §7.1）。"""
    sys.stderr.write(text.rstrip("\n") + "\n")


def _emit(obj, stream=None):
    """唯一出口：一个 JSON + 一个 `\\n`。"""
    stream = stream or sys.stdout
    stream.write(base.dumps(obj))
    stream.flush()


def _error(code, message, hint=""):
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


# ---------------------------------------------------------------- 组装


def build_measure(run_path, db_path=None, config_path=None):
    """读 run.json → measure 段；返回 `(measure, run_doc)`。抛 `ValueError` 表示数据不可用。

    `modalities` 块每次都重读 `~/.config/opencode/opencode.json`（简报 ④：别缓存），
    读不到就照实记 `unknown_reasons`，不猜、不补默认。
    """
    run_doc = perf.load_run_doc(run_path)
    run_dir = perf.run_dir_of(run_path)
    payload = perf.measure(run_doc, run_dir=run_dir, db_path=db_path)

    registry = modalities.registry(config_path=config_path)
    problems = modalities.validate(registry)
    if problems:
        raise RuntimeError("模态登记表不合规: %s" % "; ".join(problems))
    # 每行 {modality, status, reason, evidence}；status 只有 measured / not_applicable 两种
    payload["modalities"] = registry["modalities"]
    payload["modalities_source"] = {
        "config_path": registry["config"]["path"],
        "present": registry["config"]["present"],
        "readable": registry["config"]["readable"],
        "declared_inputs": registry["declared_inputs"],
        "per_model": registry["per_model"],
        "unknown_reasons": registry["degraded_reasons"],
    }

    hits = base.forbidden_hits(payload)
    if hits:  # 自证：本模块自己也不许吐出禁用字段名
        raise RuntimeError("measure 里出现禁用字段: %s" % ", ".join(hits))
    return payload, run_doc


def build_modalities(provider=None, model=None, config_path=None):
    doc = modalities.registry(provider=provider, model=model, config_path=config_path)
    problems = modalities.validate(doc)
    if problems:
        raise RuntimeError("模态登记表不合规: %s" % "; ".join(problems))
    hits = base.forbidden_hits(doc)
    if hits:
        raise RuntimeError("模态登记表里出现禁用字段: %s" % ", ".join(hits))
    return doc


def write_json_file(path, obj, label):
    """写盘：UTF-8 无 BOM、`\\n`、末尾一个换行；目标是目录时按 `label` 补文件名。"""
    if not path:
        raise ValueError("--out 需要一个文件路径")
    target = path
    if os.path.isdir(target):
        target = os.path.join(target, label)
    parent = os.path.dirname(os.path.abspath(target))
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    base.write_text(target, base.dumps(obj))
    return target


# ---------------------------------------------------------------- 主入口


def run_measure(args):
    """`--run` 路径：返回 `(envelope, exit_code)`。"""
    try:
        payload, run_doc = build_measure(args.run, args.db, args.config)
    except ValueError as exc:
        _err("数据不可用: %s" % exc)
        return _error("data_unavailable", str(exc), "检查 --run 指向的 run.json 是否存在且合法"), 3
    except OSError as exc:
        _err("读盘失败: %s" % exc)
        return _error("io_error", str(exc), "检查文件权限"), EXIT_INTERNAL
    except RuntimeError as exc:
        _err("内部自检失败: %s" % exc)
        return _error("internal_error", str(exc), "这是工具自身的缺陷，不是数据问题"), EXIT_INTERNAL

    envelope = {"ok": True}
    if args.out:
        try:
            written = write_json_file(args.out, payload, "measure.json")
        except (OSError, ValueError) as exc:
            _err("写盘失败: %s" % exc)
            return _error("write_failed", str(exc), "检查 --out 的父目录是否可写"), EXIT_INTERNAL
        _err("已写出: %s" % written)
        envelope["out"] = written
    envelope.update(payload)
    _err(
        "run %s：wall_s=%r，turns=%r，round_gap n=%r"
        % (payload["run_id"], payload["wall_s"], payload["turns"], payload["round_gap"]["n"])
    )
    return envelope, EXIT_OK


def run_modalities(args):
    try:
        doc = build_modalities(provider=args.provider, model=args.model, config_path=args.config)
    except RuntimeError as exc:
        _err("内部自检失败: %s" % exc)
        return _error("internal_error", str(exc), ""), EXIT_INTERNAL
    envelope = {"ok": True}
    if args.out:
        try:
            written = write_json_file(args.out, doc, "modalities.json")
        except (OSError, ValueError) as exc:
            _err("写盘失败: %s" % exc)
            return _error("write_failed", str(exc), "检查 --out 的父目录是否可写"), EXIT_INTERNAL
        _err("已写出: %s" % written)
        envelope["out"] = written
    for row in doc["modalities"]:
        _err("%-6s %s%s" % (row["modality"], row["status"], "（%s）" % row["reason"] if row["reason"] else ""))
    envelope.update(doc)
    return envelope, EXIT_OK


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="python -m collect.measure",
        description="性能五标量 + 模态登记（unknown 不估算）",
    )
    parser.add_argument("--run", help="run.json 路径")
    parser.add_argument("--out", help="输出文件路径（--run 时为 measure.json；--modalities 时为登记表）")
    parser.add_argument("--json", action="store_true", help="stdout 恰好一个 JSON")
    parser.add_argument("--self-test", action="store_true", help="内置自检，一条一行 PASS/FAIL")
    parser.add_argument("--modalities", action="store_true", help="生成模态登记表")
    parser.add_argument("--db", help="宿主 opencode.db 路径（默认按 host_db.default_db_path 找）")
    parser.add_argument("--config", help="opencode.json 路径（默认 ~/.config/opencode/opencode.json）")
    parser.add_argument("--provider", help="只登记这个 provider")
    parser.add_argument("--model", help="只登记这个模型")

    args = parser.parse_args(argv)

    if args.self_test:
        from . import selftest

        return selftest.run()

    if args.modalities:
        envelope, code = run_modalities(args)
    elif args.run:
        envelope, code = run_measure(args)
    else:
        envelope = _error("bad_args", "需要 --run <run.json> 或 --modalities", "例：--run runs/x/run.json --out runs/x/measure.json")
        code = EXIT_BAD_ARGS
        _err("参数不足：%s" % envelope["error"]["hint"])

    # `--json` 或出错时：stdout 恰好一个 JSON；纯落盘模式（无 --json 且成功）不往 stdout 写。
    if args.json or code != EXIT_OK:
        _emit(envelope)
    return code


if __name__ == "__main__":
    sys.exit(main())
