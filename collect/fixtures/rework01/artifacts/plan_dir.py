"""plan_dir.py（夹具示意产物 · 步 2）

只作为「步 2 的产物」被采集器量 exists / bytes / sha256。
它不会被 import，也不会被执行 —— gates.log 里的 exit=0 是手写的示意值。
"""


def normalize_plan_dir(root):
    """示意桩：真实实现在步 3 的 collect/ingest/plan_dir.py。"""
    return {"root": str(root), "steps": [], "degraded_reasons": []}
