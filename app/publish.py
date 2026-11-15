"""台账发布：核对通过后一次性切换版本，失败恢复上一版，同批重试不叠加用量。

设计要点：

- **批量指纹（batch_key）**：由「月份 + 参与表 + 参数 + 对齐后读数 + 换表记录 + 容差」
  共同哈希得到。同一批数据重试得到同一个 batch_key，不会产生第二个版本，也不会把
  同一段用量重复计入。
- **原子切换**：新版本写入 ledger_versions 与 meta 活动指针在同一事务内提交；
  中途失败整体回滚，活动指针仍指向上一个已核对版本。
- **失败恢复**：事务回滚后另开事务确认活动指针仍是发布前版本，并记一条 rollback 事件。
- **回滚**：任意历史版本可一键重新激活，活动指针切换与版本数据相互独立。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from . import store


def _stamp() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _hash(payload: object) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def batch_key(
    *,
    month: str,
    selected: list[str],
    full_scale: float,
    max_gap_days: int,
    tou_tolerance: float,
    rows: list[dict],
    changes: list[dict],
    align_days: int,
    reading_precision: float,
) -> str:
    """同一批数据重试必须得到同一个 key（幂等的基础）。"""
    fingerprint = {
        "month": month,
        "selected": sorted(selected),
        "full_scale": round(float(full_scale), 4),
        "max_gap_days": max_gap_days,
        "tou_tolerance": round(float(tou_tolerance), 6),
        "align_days": align_days,
        "reading_precision": round(float(reading_precision), 4),
        "readings": sorted(
            [
                (
                    row["meter_id"],
                    row["read_at"].isoformat(),
                    row["kind"],
                    row.get("source") or "manual",
                    round(float(row["reading_kwh"]), 3),
                    int(row.get("precision", 1)),
                )
                for row in rows
                if row["meter_id"] in selected
            ]
        ),
        "changes": [
            change
            for change in changes
            if str(change.get("meter_id")) in set(selected)
        ],
    }
    return "batch-" + _hash(fingerprint)[:24]


def ledger_key(payload: dict) -> str:
    return "ledger-" + _hash(payload)[:24]


def publish(
    conn,
    *,
    batch_key_value: str,
    run_id: str,
    month: str,
    month_order: list[str],
    payload: dict,
) -> dict:
    """在调用方的事务里写入新版本并切换活动指针，**不自行提交**。

    版本数据与 meta 活动指针由同一个事务一起提交，因此对外只有「旧版」或「新版」
    两种可见状态；任何一步失败由调用方回滚整个事务，活动指针自然保持上一版。

    返回 {version_id, reused, previous_version_id}。
    """
    previous = store.active_ledger(conn)
    existing = store.ledger_by_batch(conn, batch_key_value)
    if existing is not None:
        # 同批重试：复用已有版本（不新增版本、不重复计用量）
        if not previous or previous["version_id"] != existing["version_id"]:
            store.activate_ledger(conn, existing["version_id"])
        return {
            "version_id": existing["version_id"],
            "reused": True,
            "previous_version_id": previous["version_id"] if previous else None,
        }

    version_id = f"lv-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    version = {
        "version_id": version_id,
        "batch_key": batch_key_value,
        "ledger_key": ledger_key(payload),
        "created_at": _stamp(),
        "run_id": run_id,
        "month": month,
        "month_order": month_order,
        "payload": payload,
    }
    store.save_ledger_version(conn, version)
    store.activate_ledger(conn, version_id)
    return {
        "version_id": version_id,
        "reused": False,
        "previous_version_id": previous["version_id"] if previous else None,
    }


def record_failure(conn, previous_version_id: str | None, error: Exception) -> None:
    """发布失败后另开事务：确认活动指针仍指向上一版，并记一条 rollback 事件。"""
    active = store.active_ledger(conn)
    active_id = active["version_id"] if active else None
    if previous_version_id and active_id != previous_version_id:
        store.activate_ledger(conn, previous_version_id)
        active_id = previous_version_id
    store.add_event(
        conn,
        "rollback",
        f"台账发布失败（{error}），已恢复上一版 {active_id or '（无历史版本）'}",
        {"restored_version": active_id, "error": str(error)},
        _stamp(),
    )
    conn.commit()


def rollback(conn, version_id: str | None = None) -> dict:
    """切回指定历史版本；不指定时回退到活动版本之前最近的一版。"""
    active = store.active_ledger(conn)
    if version_id:
        target = store.load_ledger_version(conn, version_id)
        if target is None:
            raise KeyError(f"版本 {version_id} 不存在")
    else:
        target = _previous_version(conn, active["version_id"] if active else None)
        if target is None:
            raise KeyError("没有可回退的历史版本")
    store.activate_ledger(conn, target["version_id"])
    store.add_event(
        conn,
        "rollback",
        f"台账回滚到 {target['version_id']}",
        {
            "from_version": active["version_id"] if active else None,
            "to_version": target["version_id"],
        },
        _stamp(),
    )
    return target


def _previous_version(conn, active_version_id: str | None) -> dict | None:
    history = store.ledger_history(conn, limit=50)
    for index, item in enumerate(history):
        if item["version_id"] == active_version_id:
            if index + 1 < len(history):
                return store.load_ledger_version(conn, history[index + 1]["version_id"])
            return None
    return store.load_ledger_version(conn, history[1]["version_id"]) if len(history) > 1 else None
