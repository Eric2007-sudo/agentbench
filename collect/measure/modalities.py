"""多模态登记表（`contract/dimensions.md` §4）。

判定依据只有一处：`~/.config/opencode/opencode.json` 的
`providers.*.models.*.capabilities.input` —— 把**读到的原值**写进 `evidence`。

三条硬规矩：

1. `modality` 只能取 `text`/`image`/`audio`/`video`/`pdf`；
   `status` 只能取 `measured` 或 `not_applicable` —— **没有第三种值**（§4.2）。
2. 读不到 `capabilities.input` 时**不许猜**：整张表照出，status 照填（text 记 `measured`
   属猜测，故整张表都按「未声明」处理），并 `degraded_reasons += "modal.capabilities_unknown"`，
   单独说明，不污染 status 枚举（§4.2）。
3. `not_applicable` 不是 0 分、不是「不支持」：它只表示「这次的结构里没有这个模态」（§4.3）。
"""

import sys

sys.dont_write_bytecode = True

import json
import os

from . import base

MODALITIES = ("text", "image", "audio", "video", "pdf")
STATUSES = ("measured", "not_applicable")

# 未声明时的 reason（§4.2：拿不到 capabilities.input 时的固定说法）
NO_CAPS_REASON = "当前 provider 的模型没有声明 capabilities.input，无从判定"
NOT_IN_CAPS_REASON = "当前 provider 的模型声明 capabilities.input 不含该模态"

# §4.4：换什么才能测 —— 三者缺一不可
TO_MEASURE = [
    "模型：capabilities.input 含该模态（例如 [\"text\",\"image\"]），并经同一 provider 实际可调",
    "夹具：一份带该模态输入的固定样例（图片 + 题面 + 期望产物），放 fixtures/ 下，只读",
    "判分方式：机械判据（产物 hash + 断言）或明确的 jev 概率阈值口径",
]

CONFIG_REL = os.path.join(".config", "opencode", "opencode.json")


def default_config_path():
    """`~/.config/opencode/opencode.json`；不存在就返回路径本身（由读取处报 present:false）。"""
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, CONFIG_REL)


def _capabilities_of(model):
    """`models.*.capabilities.input` 的原值；拿不到 → `None`（不猜、不补默认）。"""
    if not isinstance(model, dict):
        return None
    caps = model.get("capabilities")
    if not isinstance(caps, dict):
        return None
    inputs = caps.get("input")
    if not isinstance(inputs, list):
        return None
    if not all(isinstance(item, str) for item in inputs):
        return None
    return inputs


def read_config(path=None):
    """读 provider 配置：返回 `(doc, present, readable, reason)`。"""
    path = path or default_config_path()
    if not os.path.isfile(path):
        return None, False, False, "config.missing:%s" % path
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            doc = json.load(handle)
    except OSError as exc:
        return None, True, False, "config.unreadable:%s" % exc
    except ValueError as exc:
        return None, True, False, "config.bad_json:%s" % exc
    if not isinstance(doc, dict):
        return None, True, False, "config.not_object"
    return doc, True, True, None


def providers_of(doc):
    """`providers`（契约写法）优先，兼容 `provider`（opencode 常见写法）。不猜别的键。"""
    if not isinstance(doc, dict):
        return {}
    for key in ("providers", "provider"):
        value = doc.get(key)
        if isinstance(value, dict):
            return {name: entry for name, entry in value.items() if isinstance(entry, dict)}
    return {}


def declared_inputs(doc, provider=None, model=None):
    """汇总声明的输入模态集合。

    只采 `provider` / `model` 指定到的对象；没指定就采全部 provider 的全部模型。
    返回 `(declared_set, evidence_by_model, provider_count, model_count)`；
    一个模型都没能读到 `capabilities.input` 时 `declared_set` 为 `None`。
    """
    providers = providers_of(doc)
    if provider and provider in providers:
        providers = {provider: providers[provider]}
    evidence = {}
    count = 0
    for pname in sorted(providers):
        models = providers[pname].get("models")
        if not isinstance(models, dict):
            continue
        for mname in sorted(models):
            if model and mname != model:
                continue
            inputs = _capabilities_of(models[mname])
            if inputs is None:
                continue
            evidence["%s/%s" % (pname, mname)] = inputs
            count += 1
    if not evidence:
        return None, evidence, len(providers), 0
    declared = set()
    for inputs in evidence.values():
        declared.update(inputs)
    return declared, evidence, len(providers), count


def registry(doc=None, provider=None, model=None, config_path=None):
    """生成模态登记表（逐模态一行，顺序固定 `text`→`image`→`audio`→`video`→`pdf`）。"""
    reasons = []
    if doc is None:
        doc, present, readable, reason = read_config(config_path)
        if reason:
            reasons.append("modal.capabilities_unknown")
    else:
        present = readable = True

    declared, evidence, provider_count, model_count = declared_inputs(doc, provider, model)
    if declared is None:
        if "modal.capabilities_unknown" not in reasons:
            reasons.append("modal.capabilities_unknown")

    if declared is not None:
        evidence_text = "capabilities.input = " + json.dumps(
            sorted(declared), ensure_ascii=False, separators=(",", ":")
        )
    else:
        evidence_text = "capabilities.input 读不到"

    rows = []
    for modality in MODALITIES:
        if declared is None:
            # 读不到就没有任何模态被声明 —— 不许把 text 顺手记成 measured（那是猜）
            status = "not_applicable"
            why = NO_CAPS_REASON
        elif modality in declared:
            status = "measured"
            why = None
        else:
            status = "not_applicable"
            why = NOT_IN_CAPS_REASON
        assert status in STATUSES  # §4.2：没有第三种值
        rows.append(
            {
                "modality": modality,
                "status": status,
                "reason": why,
                "evidence": evidence_text,
            }
        )

    return {
        "schema": 1,
        "config": {
            "path": config_path or default_config_path(),
            "present": bool(present),
            "readable": bool(readable),
        },
        "providers_seen": provider_count,
        "models_counted": model_count,
        "declared_inputs": sorted(declared) if declared is not None else base.UNKNOWN,
        "per_model": {name: evidence[name] for name in sorted(evidence)},
        "modalities": rows,
        "to_measure": list(TO_MEASURE),
        "note": "not_applicable = 这次的结构里没有这个模态，不是「测了得 0 分」，也不是「不支持」",
        "degraded_reasons": reasons,
    }


def validate(registry_doc):
    """校验枚举与逐模态覆盖；返回问题列表（空 = 合规）。"""
    problems = []
    rows = registry_doc.get("modalities")
    if not isinstance(rows, list):
        return ["modalities 不是数组"]
    seen = [row.get("modality") for row in rows if isinstance(row, dict)]
    if sorted(seen) != sorted(MODALITIES):
        problems.append("modal 覆盖不全或重复: %r" % (seen,))
    for row in rows:
        if not isinstance(row, dict):
            problems.append("行不是对象")
            continue
        if row.get("status") not in STATUSES:
            problems.append("非法 status: %r" % (row.get("status"),))
        if row.get("status") == "not_applicable" and not row.get("reason"):
            problems.append("not_applicable 缺 reason: %s" % row.get("modality"))
        if base.is_unknown(row.get("status")) or row.get("status") is base.UNKNOWN:
            problems.append("status 不许是 unknown: %s" % row.get("modality"))
    return problems
