"""来源对齐：现场抄表 / 自动采集同一读数点合并、偏差超精度与同源矛盾拦截。"""

from __future__ import annotations

from datetime import date

from app import alignment, readings, settings


def row(meter: str, day: str, value: float, source: str = "manual", precision: int = 1, kind: str = "total"):
    return {
        "meter_id": meter,
        "read_at": date.fromisoformat(day),
        "reading_kwh": value,
        "kind": kind,
        "source": source,
        "precision": precision,
    }


def test_same_point_different_day_and_precision_merges_once():
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual", 1),
        row("EM-01", "2026-03-02", 100.00, "auto", 2),   # 同一次抄见，日期差 1 天、精度 2 位
        row("EM-01", "2026-03-31", 160.0, "manual", 1),
        row("EM-01", "2026-04-01", 160.00, "auto", 2),
    ]
    result = alignment.align(rows)
    assert result["summary"]["conflicts"] == 0
    series = result["series"][("EM-01", "total")]
    assert len(series) == 2  # 不是 4 个点
    assert series[0]["read_at"] == date(2026, 3, 1)   # 日期以现场抄表为准
    assert series[1]["read_at"] == date(2026, 3, 31)
    merged = [point for point in result["points"] if point["merged"]]
    assert len(merged) == 2
    # 保留取舍依据：两路原始值与理由
    assert len(merged[0]["sources"]) == 2
    assert "精度更细" in merged[0]["rationale"]
    assert merged[0]["date_span_days"] == 1


def test_deviation_beyond_precision_blocks_publish():
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual"),
        row("EM-01", "2026-03-01", 100.4, "auto", 1),   # 偏差 0.4 > 0.1
        row("EM-01", "2026-03-02", 130.0, "manual"),
    ]
    result = alignment.align(rows)
    assert result["summary"]["conflicts"] == 1
    conflict = result["conflicts"][0]
    assert conflict["type"] == "cross_source_deviation"
    assert conflict["detail"]["diff"] == 0.4
    assert conflict["detail"]["tolerance"] == 0.1
    # 仍保留主来源数值供人工核对，但批次被标记不可发布
    merged = [point for point in result["points"] if point["merged"]][0]
    assert merged["conflict"] is True
    assert merged["value"] == 100.0


def test_precision_tolerance_allows_last_digit_rounding():
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual", 1),
        row("EM-01", "2026-03-01", 100.1, "auto", 2),   # 末位差 1 格，容差内
        row("EM-01", "2026-03-02", 130.0, "manual", 1),
    ]
    result = alignment.align(rows)
    assert result["summary"]["conflicts"] == 0


def test_same_source_conflicting_readings_are_flagged():
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual"),
        row("EM-01", "2026-03-01", 101.0, "manual"),
        row("EM-01", "2026-03-02", 130.0, "manual"),
    ]
    result = alignment.align(rows)
    assert result["summary"]["same_source_conflict"] == 1
    assert result["conflicts"][0]["type"] == "same_source_conflict"
    # 矛盾点只保留第一条，避免同日两个读数被当成两个区间
    assert len(result["series"][("EM-01", "total")]) == 2


def test_unmatched_secondary_point_is_kept():
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual"),
        row("EM-01", "2026-03-02", 130.0, "auto", 2),   # 现场抄表漏了这一天
        row("EM-01", "2026-03-03", 145.0, "manual"),
    ]
    result = alignment.align(rows)
    series = result["series"][("EM-01", "total")]
    assert [point["read_at"] for point in series] == [
        date(2026, 3, 1),
        date(2026, 3, 2),
        date(2026, 3, 3),
    ]
    filled = [point for point in result["points"] if "补齐" in point["rationale"]]
    assert len(filled) == 1
    assert filled[0]["source"] == "auto"


def test_aligned_usage_equals_single_source_usage(sample):
    """两路来源合并后做差，用量必须与只用现场抄表完全一致（不重复跨段、不丢点）。"""
    from app import pipeline, seed

    manual_only = pipeline.analyze(
        sample["rows"], sample["meters"], sample["changes"], month="2026-03"
    )
    dual = seed.load_sample_files(include_auto=True)
    dual_result = pipeline.analyze(
        dual["rows"], dual["meters"], dual["changes"], month="2026-03"
    )
    assert dual_result["alignment"]["summary"]["merged_points"] == dual["stats"]["auto_rows"]
    assert dual_result["alignment"]["summary"]["conflicts"] == 0
    assert (
        dual_result["focus"]["totals"]["usage_total"]
        == manual_only["focus"]["totals"]["usage_total"]
        == 2225.82
    )
    # 每个月每块表的用量也逐项相同
    for month, block in dual_result["months"].items():
        base = {row["meter_id"]: row for row in manual_only["months"][month]["rows"]}
        for row in block["rows"]:
            assert row["total"] == base[row["meter_id"]]["total"]
            assert row["peak"] == base[row["meter_id"]]["peak"]
            assert row["valley"] == base[row["meter_id"]]["valley"]


def test_same_day_points_never_leave_two_points_for_one_date():
    """同一读数日只能留一个点：精度内合并、超精度记矛盾并以现场抄表为准。"""
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual"),
        row("EM-01", "2026-03-01", 100.4, "auto"),   # 同日超精度
        row("EM-01", "2026-03-02", 130.0, "manual"),
    ]
    result = alignment.align(rows)
    series = result["series"][("EM-01", "total")]
    assert [point["read_at"] for point in series] == [date(2026, 3, 1), date(2026, 3, 2)]
    assert series[0]["value"] == 100.0  # 以现场抄表为准
    assert result["summary"]["conflicts"] == 1
    assert result["conflicts"][0]["type"] == "cross_source_deviation"


def test_align_days_zero_does_not_merge_shifted_points():
    rows = [
        row("EM-01", "2026-03-01", 100.0, "manual"),
        row("EM-01", "2026-03-02", 100.0, "auto", 2),
        row("EM-01", "2026-03-03", 130.0, "manual"),
    ]
    result = alignment.align(rows, align_days=0)
    assert len(result["series"][("EM-01", "total")]) == 3


def test_parse_csv_reads_optional_source_column():
    text = (
        "meter_id,read_at,reading_kwh,kind,source\n"
        "EM-01,2026-03-01,100.0,total,manual\n"
        "EM-01,2026-03-01,100.00,total,auto\n"
        "EM-01,2026-03-02,130.0,total\n"   # 缺省 manual
    )
    parsed = readings.parse_csv(text)
    assert parsed["stats"]["source_counts"] == {"manual": 2, "auto": 1}
    assert parsed["stats"]["sources"] == ["auto", "manual"]
    assert parsed["rows"][1]["precision"] == 2


def test_parse_csv_rejects_unknown_source():
    text = "meter_id,read_at,reading_kwh,kind,source\nEM-01,2026-03-01,100.0,total,ocr\n"
    with __import__("pytest").raises(readings.ReadingError) as exc:
        readings.parse_csv(text)
    assert "未知读数来源" in exc.value.problems[0]["message"]


def test_default_align_thresholds_come_from_settings():
    assert settings.SOURCE_ALIGN_DAYS == 1
    assert settings.READING_PRECISION == 0.1
