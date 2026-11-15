"""台账发布：核对通过才发布、同批重试不叠加、失败/回滚恢复上一版。"""

from __future__ import annotations

import csv
import io
import tempfile
from pathlib import Path

import pytest

from app import main, publish, settings, store


DUAL_CSV = """meter_id,read_at,reading_kwh,kind,source
DV-01,2026-01-01,100.0,total,manual
DV-01,2026-01-01,100.00,total,auto
DV-01,2026-01-01,62.0,peak,manual
DV-01,2026-01-01,62.00,peak,auto
DV-01,2026-01-01,38.0,valley,manual
DV-01,2026-01-01,38.00,valley,auto
DV-01,2026-02-01,160.0,total,manual
DV-01,2026-02-01,160.00,total,auto
DV-01,2026-02-01,99.2,peak,manual
DV-01,2026-02-01,99.20,peak,auto
DV-01,2026-02-01,60.8,valley,manual
DV-01,2026-02-01,60.80,valley,auto
"""

CLEAN_CSV = """meter_id,read_at,reading_kwh,kind,source
DV-04,2026-01-01,200.0,total,manual
DV-04,2026-01-01,200.00,total,auto
DV-04,2026-01-01,120.0,peak,manual
DV-04,2026-01-01,120.00,peak,auto
DV-04,2026-01-01,80.0,valley,manual
DV-04,2026-01-01,80.00,valley,auto
DV-04,2026-02-01,260.0,total,manual
DV-04,2026-02-01,260.00,total,auto
DV-04,2026-02-01,156.0,peak,manual
DV-04,2026-02-01,156.00,peak,auto
DV-04,2026-02-01,104.0,valley,manual
DV-04,2026-02-01,104.00,valley,auto
"""

CONFLICT_CSV = """meter_id,read_at,reading_kwh,kind,source
DV-02,2026-01-01,500.0,total,manual
DV-02,2026-01-01,512.5,total,auto
DV-02,2026-01-01,310.0,peak,manual
DV-02,2026-01-01,320.0,peak,auto
DV-02,2026-01-01,190.0,valley,manual
DV-02,2026-01-01,192.5,valley,auto
DV-02,2026-02-01,600.0,total,manual
DV-02,2026-02-01,612.5,total,auto
DV-02,2026-02-01,372.0,peak,manual
DV-02,2026-02-01,382.0,peak,auto
DV-02,2026-02-01,228.0,valley,manual
DV-02,2026-02-01,230.5,valley,auto
"""


@pytest.fixture()
def fresh_db(monkeypatch):
    """每个用例一个独立 SQLite 文件 + 清空还原缓存，避免批次/版本互相污染。"""
    path = Path(tempfile.mkdtemp(prefix="metercalc-publish-")) / "db.sqlite3"
    monkeypatch.setattr(settings, "DB_PATH", path)
    monkeypatch.setattr(main, "_CACHE", {"key": None, "value": None})
    yield path
    monkeypatch.undo()
    main._CACHE["key"] = None
    main._CACHE["value"] = None


def upload(client, text: str, name: str = "dual.csv"):
    response = client.post("/api/import", files={"file": (name, text.encode("utf-8"), "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()


def restore(client, month: str, meter: str, **extra):
    return client.post("/api/restore", json={"month": month, "meter_ids": [meter], **extra})


def test_dual_source_restore_merges_and_publishes(client, fresh_db):
    upload(client, DUAL_CSV)
    response = restore(client, "2026-01", "DV-01")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["published"] is True
    assert body["alignment"]["summary"]["merged_points"] == 6
    assert body["alignment"]["summary"]["conflicts"] == 0
    assert body["rows"][0]["total"] == 60.0
    assert body["ledger_version"].startswith("lv-")
    assert body["active_version"]["version_id"] == body["ledger_version"]

    # 对齐依据：每个合并点都写明两路原始值与取舍理由
    merged = [point for point in body["alignment"]["points"] if point["merged"]]
    assert merged and all(len(point["sources"]) == 2 for point in merged)
    assert all(point["rationale"] for point in merged)

    active = client.get("/api/ledger/active").json()["active"]
    assert active["version_id"] == body["ledger_version"]
    assert active["alignment"]["merged_points"] == 6

    versions = client.get("/api/ledger/versions").json()
    assert versions["active_version"] == body["ledger_version"]
    assert versions["versions"][0]["active"] is True


def test_retry_same_batch_does_not_double_count(client, fresh_db):
    upload(client, DUAL_CSV)
    first = restore(client, "2026-01", "DV-01").json()
    second = restore(client, "2026-01", "DV-01").json()
    third = restore(client, "2026-01", "DV-01").json()

    assert first["batch_key"] == second["batch_key"] == third["batch_key"]
    assert first["ledger_version"] == second["ledger_version"] == third["ledger_version"]
    assert second["ledger_reused"] is True
    assert third["ledger_reused"] is True
    assert first["run_id"] == third["run_id"]  # 同批复用批次号，不产生第二个批次

    versions = client.get("/api/ledger/versions").json()["versions"]
    assert sum(1 for item in versions if item["batch_key"] == first["batch_key"]) == 1

    runs = client.get("/api/runs").json()["runs"]
    assert sum(1 for item in runs if item["batch_key"] == first["batch_key"]) == 1

    # 导出只有一份用量，重试没有叠加
    ledger = list(csv.reader(io.StringIO(client.get("/api/ledger.csv").text)))
    dv_rows = [row for row in ledger[1:] if row[1] == "DV-01" and row[0] == "2026-01"]
    assert len(dv_rows) == 1
    assert dv_rows[0][3] == "60.00"


def test_cross_source_deviation_blocks_publish(client, fresh_db):
    upload(client, DUAL_CSV)
    good = restore(client, "2026-01", "DV-01").json()

    upload(client, CONFLICT_CSV)
    blocked = restore(client, "2026-01", "DV-02")
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["ok"] is False
    assert body["published"] is False
    assert "不发布新台账" in body["error"]
    assert {item["type"] for item in body["conflicts"]} == {"cross_source_deviation"}
    total_conflict = next(item for item in body["conflicts"] if item["kind"] == "total")
    assert total_conflict["detail"]["diff"] == 12.5
    # 活动台账仍指向上一版，没有被半成品覆盖
    assert body["active_version"]["version_id"] == good["ledger_version"]
    active = client.get("/api/ledger/active").json()["active"]
    assert active["version_id"] == good["ledger_version"]
    # 被拒绝的批次没有写入任何版本
    versions = client.get("/api/ledger/versions").json()["versions"]
    assert len(versions) == 1

    # 仅供核对、不发布时仍返回分析数值
    preview = restore(client, "2026-01", "DV-02", allow_publish=False)
    assert preview.status_code == 201
    assert preview.json()["alignment"]["publish_blocked"] is True
    assert preview.json()["published"] is False


def test_rollback_restores_previous_version(client, fresh_db):
    upload(client, DUAL_CSV)
    first = restore(client, "2026-01", "DV-01").json()

    upload(client, CLEAN_CSV)
    second = restore(client, "2026-01", "DV-04").json()
    assert second["ledger_version"] != first["ledger_version"]

    rolled = client.post("/api/ledger/rollback", json={})
    assert rolled.status_code == 200
    assert rolled.json()["rolled_back_to"] == first["ledger_version"]
    active = client.get("/api/ledger/active").json()["active"]
    assert active["version_id"] == first["ledger_version"]

    # 显式回到指定版本
    again = client.post("/api/ledger/rollback", json={"version_id": second["ledger_version"]})
    assert again.status_code == 200
    assert client.get("/api/ledger/active").json()["active"]["version_id"] == second["ledger_version"]

    missing = client.post("/api/ledger/rollback", json={"version_id": "lv-nope"})
    assert missing.status_code == 404


def test_publish_failure_restores_previous_version(client, fresh_db):
    upload(client, DUAL_CSV)
    first = restore(client, "2026-01", "DV-01").json()

    def boom(*args, **kwargs):
        raise RuntimeError("模拟切换失败")

    # 写版本与切指针在同一事务：写版本失败 → 整批回滚，活动指针保持上一版
    patches = pytest.MonkeyPatch()
    patches.setattr(publish.store, "save_ledger_version", boom)
    with pytest.raises(RuntimeError):
        client.post("/api/restore", json={"month": "2026-01", "meter_ids": ["DV-01"], "max_gap_days": 30})
    patches.undo()

    active = client.get("/api/ledger/active").json()["active"]
    assert active["version_id"] == first["ledger_version"]
    # 半成品版本没有留下
    versions = client.get("/api/ledger/versions").json()["versions"]
    assert [item["version_id"] for item in versions] == [first["ledger_version"]]
    with store.session() as conn:
        kinds = [row["kind"] for row in store.list_events(conn, limit=5)]
    assert "rollback" in kinds


def test_failure_mid_switch_keeps_previous_pointer(client, fresh_db):
    """指针已切到新版本后出错：事务回滚必须把指针带回上一版。"""
    upload(client, DUAL_CSV)
    first = restore(client, "2026-01", "DV-01").json()

    original = publish.store.activate_ledger

    def activate_then_fail(conn, version_id):
        original(conn, version_id)          # 指针已切到新版本
        raise RuntimeError("切换后写事件失败")

    patches = pytest.MonkeyPatch()
    patches.setattr(publish.store, "activate_ledger", activate_then_fail)
    with pytest.raises(RuntimeError):
        client.post("/api/restore", json={"month": "2026-01", "meter_ids": ["DV-01"], "max_gap_days": 30})
    patches.undo()

    active = client.get("/api/ledger/active").json()["active"]
    assert active["version_id"] == first["ledger_version"]
    versions = client.get("/api/ledger/versions").json()["versions"]
    assert [item["version_id"] for item in versions] == [first["ledger_version"]]


def test_retry_after_failure_does_not_accumulate(client, fresh_db):
    upload(client, DUAL_CSV)
    calls = {"n": 0}
    original = publish.store.save_ledger_version

    def flaky(conn, version):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次发布失败")
        return original(conn, version)

    patches = pytest.MonkeyPatch()
    patches.setattr(publish.store, "save_ledger_version", flaky)
    with pytest.raises(RuntimeError):
        client.post("/api/restore", json={"month": "2026-01", "meter_ids": ["DV-01"]})
    patches.undo()
    retry = client.post("/api/restore", json={"month": "2026-01", "meter_ids": ["DV-01"]})
    assert retry.status_code == 201

    versions = client.get("/api/ledger/versions").json()["versions"]
    assert len(versions) == 1
    ledger = list(csv.reader(io.StringIO(client.get("/api/ledger.csv").text)))
    dv_rows = [row for row in ledger[1:] if row[1] == "DV-01"]
    assert len(dv_rows) == 1
    assert dv_rows[0][3] == "60.00"


def test_same_source_conflict_blocks_publish(fresh_db):
    """同一来源同一天两个不同读数（表计时钟错误/重复导出）→ 不发布。"""
    from datetime import date

    from app import pipeline

    rows = [
        {"meter_id": "DV-05", "read_at": date(2026, 1, 1), "reading_kwh": 100.0, "kind": "total",
         "source": "manual", "precision": 1},
        {"meter_id": "DV-05", "read_at": date(2026, 1, 1), "reading_kwh": 101.0, "kind": "total",
         "source": "manual", "precision": 1},
        {"meter_id": "DV-05", "read_at": date(2026, 2, 1), "reading_kwh": 160.0, "kind": "total",
         "source": "manual", "precision": 1},
    ]
    result = pipeline.analyze(rows, [], [], month="2026-01")
    assert result["alignment"]["publish_blocked"] is True
    assert result["alignment"]["conflicts"][0]["type"] == "same_source_conflict"
    assert "来源前后矛盾" in result["alignment"]["blocked_reason"]


def test_import_stats_report_sources(client, fresh_db):
    body = upload(client, DUAL_CSV)
    assert body["stats"]["source_counts"] == {"manual": 6, "auto": 6}
    assert body["stats"]["sources"] == ["auto", "manual"]

    dual_sample = client.post("/api/import/sample", params={"include_auto": "true"}).json()
    assert dual_sample["sources"] == {"manual": 4140, "auto": 303}
    assert dual_sample["counts"]["readings"] == 4443
