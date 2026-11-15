"""来源对齐：同表 × 同电量类型，把现场抄表（manual）与自动采集（auto）的同一读数点合并。

同一块表、同一个电量类型，两路来源各自累计读数。两路的抄表日期可能差 1 天、
末位精度可能不同（现场抄表通常 1 位小数，自动采集可能 2 位）。如果不先对齐，
月度对账会把两条当成连续读数，区间用量会重复跨段或漏算。

对齐规则：

1. 按（表号, 电量类型）分组，再按来源拆成多条序列，各来源内部先按「同一读数日只保留一条」收敛。
2. 选主来源：`SOURCE_PREFERENCE` 依次优先（现场抄表优先，作为结算依据）。
3. 跨来源配对按「读数是否吻合」优先、日期差其次：
   - 读数差 ≤ 该点精度容差且日期相差 ≤ `align_days` → 视为同一读数点，只保留一条
     （数值取末位精度更细的一路，并列时取主来源），日期取主来源日期，依据里写明两路原始值与取舍理由；
   - 同一天但读数差 > 精度容差 → `cross_source_deviation`，以现场抄表数值展示分析结果，
     但**禁止发布新台账**；
   - 日期错开且读数不吻合 → 不合并，各自保留（避免把另一路的点错配到相邻另一天）。
4. 其它来源里没有匹配上的点（例如自动采集有、现场抄表漏抄）→ 补齐为独立读数点。
5. 同一来源在同一天给出两个不同读数 → `same_source_conflict`（同一来源前后矛盾），
   同样禁止发布。
6. 兜底：任何情况下同一读数日不能留下两个读数点，否则下游会把同日两次上报当成两个区间。

对齐只合并「同一个读数点」，不改变读数本身；合并后每个读数点只有一条，
因此下游做差不会把同一天的两次上报当成两个区间，重试也不会叠加用量。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from . import readings, settings

SOURCE_PREFERENCE = ("manual", "auto")


def _precision_tolerance(left: dict, right: dict, floor: float | None = None) -> float:
    """两路来源的精度容差：取两边较粗的末位精度半格，且不低于配置的抄表精度。"""
    coarser = min(int(left.get("precision", 1)), int(right.get("precision", 1)))
    half_unit = 0.5 * (10 ** (-coarser))
    base = settings.READING_PRECISION if floor is None else float(floor)
    return round(max(base, half_unit), 4)


def _preference_rank(source: str) -> int:
    """来源优先级：数值越小越优先；未登记的来源排在最后。"""
    return SOURCE_PREFERENCE.index(source) if source in SOURCE_PREFERENCE else len(SOURCE_PREFERENCE)


def _point(row: dict) -> dict:
    return {
        "read_at": row["read_at"],
        "value": float(row["reading_kwh"]),
        "source": row.get("source") or readings.DEFAULT_SOURCE,
        "precision": int(row.get("precision", 1)),
        "line": row.get("line"),
    }


def _raw_view(point: dict) -> dict:
    return {
        "source": point["source"],
        "source_label": readings.SOURCE_LABELS.get(point["source"], point["source"]),
        "read_at": point["read_at"].isoformat(),
        "value": round(point["value"], 3),
        "precision": point["precision"],
        "line": point.get("line"),
    }


def _choose(group: list[dict], primary_source: str, *, conflicting: bool) -> tuple[dict, str]:
    """同一读数点里选一条，并给出取舍理由。

    两路一致时取末位精度更细的一路（并列时取主来源）；两路矛盾时以主来源（现场抄表）
    为准，另一路只作为偏差证据保留，且整个批次不发布。
    """
    if conflicting:
        for item in group:
            if item["source"] == primary_source:
                return item, f"两路偏差超过抄表精度，以{readings.SOURCE_LABELS[primary_source]}为准，待人工复核"
        return group[0], "两路偏差超过抄表精度，待人工复核"
    chosen = max(
        group,
        key=lambda item: (item["precision"], -_preference_rank(item["source"])),
    )
    if len(group) > 1 and chosen["precision"] > min(item["precision"] for item in group):
        reason = (
            f"两路读数在精度内一致，取{readings.SOURCE_LABELS[chosen['source']]}"
            f"（末位精度更细，{chosen['precision']} 位小数）"
        )
    elif len(group) > 1:
        reason = f"两路读数在精度内一致，取{readings.SOURCE_LABELS[chosen['source']]}（结算主来源）"
    else:
        reason = f"仅{readings.SOURCE_LABELS.get(chosen['source'], chosen['source'])}有该读数点"
    return chosen, reason


def align(
    rows: list[dict],
    *,
    align_days: int | None = None,
    precision: float | None = None,
) -> dict:
    """把两路来源的读数点对齐成每表每类型一条有序序列。

    返回 {series, points, conflicts, summary}：
    - series：{(meter_id, kind): [point, ...]}，下游可直接按现有规则做差；
    - points：每个对齐后读数点的依据（两路原始值、日期差、取舍理由）；
    - conflicts：偏差超精度 / 同源前后矛盾的清单（非空时禁止发布）。
    """
    align_days = settings.SOURCE_ALIGN_DAYS if align_days is None else int(align_days)
    tolerance_floor = settings.READING_PRECISION if precision is None else float(precision)

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_key[(row["meter_id"], row["kind"])].append(_point(row))

    series: dict[tuple[str, str], list[dict]] = {}
    point_evidence: list[dict] = []
    conflicts: list[dict] = []

    for (meter_id, kind), batch in sorted(by_key.items()):
        by_source: dict[str, list[dict]] = defaultdict(list)
        for point in batch:
            by_source[point["source"]].append(point)

        # 同一来源同一天两个不同读数：来源本身前后矛盾
        for source, points in by_source.items():
            seen: dict[date, dict] = {}
            deduped: list[dict] = []
            for point in sorted(points, key=lambda item: item["read_at"]):
                previous = seen.get(point["read_at"])
                if previous is not None and abs(previous["value"] - point["value"]) > 10 ** (-min(
                    previous["precision"], point["precision"]
                )):
                    conflicts.append(
                        {
                            "type": "same_source_conflict",
                            "meter_id": meter_id,
                            "kind": kind,
                            "date": point["read_at"].isoformat(),
                            "message": (
                                f"{meter_id} {readings.KIND_LABELS.get(kind, kind)}："
                                f"{readings.SOURCE_LABELS.get(source, source)}在 {point['read_at']} "
                                f"同时给出 {previous['value']:g} 与 {point['value']:g} 两个读数，来源前后矛盾"
                            ),
                            "detail": {
                                "source": source,
                                "values": [round(previous["value"], 3), round(point["value"], 3)],
                                "lines": [previous.get("line"), point.get("line")],
                            },
                        }
                    )
                    continue
                if previous is None:
                    seen[point["read_at"]] = point
                    deduped.append(point)
            by_source[source] = deduped

        available = [source for source in SOURCE_PREFERENCE if by_source.get(source)]
        available += sorted(source for source in by_source if source not in available)
        if not available:
            continue
        primary_source = available[0]
        primary = sorted(by_source[primary_source], key=lambda item: item["read_at"])
        others = [source for source in available if source != primary_source]

        # 跨来源配对规则：
        # 1) 先配「读数落在精度内」的点——同一个读数点的读数必须吻合，日期只是辅助；
        #    同类候选按读数差、日期差排序，避免把某一路的点错配到相邻另一天的读数上。
        # 2) 读数不吻合、但日期相同（同一天）——同一天的两次上报视为同一读数点但矛盾，
        #    记为偏差超精度，整批不发布。
        # 3) 其它情况（日期错开且读数不吻合）不合并，各自保留为独立读数点。
        used_secondary: dict[str, set[int]] = {source: set() for source in others}
        used_primary: dict[str, set[int]] = {source: set() for source in others}
        matches: list[tuple[int, float, int, int, str, int]] = []
        for source in others:
            for index, candidate in enumerate(by_source[source]):
                for primary_index, point in enumerate(primary):
                    delta = abs((candidate["read_at"] - point["read_at"]).days)
                    if delta > align_days:
                        continue
                    value_diff = abs(candidate["value"] - point["value"])
                    tolerance = max(_precision_tolerance(point, candidate), tolerance_floor)
                    same_day = delta == 0
                    within = value_diff <= tolerance + 1e-9
                    if not within and not same_day:
                        continue
                    # 排序键：读数一致优先（0），同日矛盾次之（1）；再按读数差、日期差
                    rank = 0 if within else 1
                    matches.append((rank, value_diff, delta, primary_index, source, index))
        matches.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        match_of_primary: dict[int, list[tuple[dict, str]]] = defaultdict(list)
        unmatched_secondary: list[tuple[str, int]] = []
        for _rank, _diff, _delta, primary_index, source, index in matches:
            if index in used_secondary[source] or primary_index in used_primary[source]:
                continue
            used_secondary[source].add(index)
            used_primary[source].add(primary_index)
            match_of_primary[primary_index].append((by_source[source][index], source))
        for source in others:
            for index in range(len(by_source[source])):
                if index not in used_secondary[source]:
                    unmatched_secondary.append((source, index))

        canonical: list[dict] = []

        for primary_index, point in enumerate(primary):
            matched = match_of_primary.get(primary_index, [])
            group = [point] + [item[0] for item in matched]
            diff = None
            tolerance = None
            span = 0
            conflicting = False
            if matched:
                diff = round(max(abs(item["value"] - point["value"]) for item in group), 3)
                tolerance = round(
                    max(
                        max(_precision_tolerance(point, item[0]) for item in matched),
                        tolerance_floor,
                    ),
                    4,
                )
                span = max(
                    abs((item[0]["read_at"] - point["read_at"]).days) for item in matched
                )
                conflicting = diff > tolerance
            chosen, reason = _choose(group, primary_source, conflicting=conflicting)
            # 对齐后读数点的日期以主来源（现场抄表）为准，避免取另一路晚一天的日期后
            # 与主来源当天的另一个点撞成同一天两个读数；数值可取精度更细的一路。
            if span:
                reason += f"；日期以现场抄表 {point['read_at']} 为准（另一路为 {chosen['read_at']}）"
            canonical_date = point["read_at"]
            if matched and conflicting:
                conflicts.append(
                    {
                        "type": "cross_source_deviation",
                        "meter_id": meter_id,
                        "kind": kind,
                        "date": point["read_at"].isoformat(),
                        "message": (
                            f"{meter_id} {readings.KIND_LABELS.get(kind, kind)} "
                            f"{point['read_at']}：现场抄表 {point['value']:g} 与自动采集 "
                            f"{matched[0][0]['value']:g} 偏差 {diff:g} kWh > 精度 {tolerance:g}，"
                            f"不发布新台账"
                        ),
                        "detail": {
                            "sources": [_raw_view(item) for item in group],
                            "date_span_days": span,
                            "diff": diff,
                            "tolerance": tolerance,
                        },
                    }
                )
            evidence = {
                "meter_id": meter_id,
                "kind": kind,
                "kind_label": readings.KIND_LABELS.get(kind, kind),
                "read_at": canonical_date.isoformat(),
                "value": round(chosen["value"], 3),
                "precision": chosen["precision"],
                "source": chosen["source"],
                "source_label": readings.SOURCE_LABELS.get(chosen["source"], chosen["source"]),
                "merged": bool(matched),
                "date_span_days": span,
                "diff": diff,
                "tolerance": tolerance,
                "sources": [_raw_view(item) for item in group],
                "rationale": reason,
                "conflict": conflicting,
            }
            canonical.append(
                {
                    "read_at": canonical_date,
                    "value": chosen["value"],
                    "source": chosen["source"],
                    "precision": chosen["precision"],
                    "evidence": evidence,
                }
            )
            point_evidence.append(evidence)

        # 其它来源里没配对上的点：补齐为独立读数点（不能因为另一路缺抄就丢读数）
        for source, index in unmatched_secondary:
            point = by_source[source][index]
            evidence = {
                "meter_id": meter_id,
                "kind": kind,
                "kind_label": readings.KIND_LABELS.get(kind, kind),
                "read_at": point["read_at"].isoformat(),
                "value": round(point["value"], 3),
                "precision": point["precision"],
                "source": point["source"],
                "source_label": readings.SOURCE_LABELS.get(point["source"], point["source"]),
                "merged": False,
                "date_span_days": 0,
                "diff": None,
                "tolerance": None,
                "sources": [_raw_view(point)],
                "rationale": (
                    f"现场抄表缺该点，取{readings.SOURCE_LABELS.get(point['source'], point['source'])}补齐"
                ),
                "conflict": False,
            }
            canonical.append(
                {
                    "read_at": point["read_at"],
                    "value": point["value"],
                    "source": point["source"],
                    "precision": point["precision"],
                    "evidence": evidence,
                }
            )
            point_evidence.append(evidence)

        # 兜底：同一读数日只能有一个读数点，否则下游做差会把同日两次上报当成两个区间。
        # 同日同源超精度 → 来源前后矛盾；同日跨源超精度 → 两路来源偏差；精度内 → 合并。
        by_day: dict[date, dict] = {}
        consolidated: list[dict] = []
        for item in canonical:
            previous = by_day.get(item["read_at"])
            if previous is None:
                by_day[item["read_at"]] = item
                consolidated.append(item)
                continue
            tolerance = round(
                max(_precision_tolerance(previous, item), tolerance_floor), 4
            )
            diff = round(abs(previous["value"] - item["value"]), 3)
            if diff > tolerance:
                same_source = previous["source"] == item["source"]
                if same_source:
                    label = readings.SOURCE_LABELS.get(item["source"], item["source"])
                    conflicts.append(
                        {
                            "type": "same_source_conflict",
                            "meter_id": meter_id,
                            "kind": kind,
                            "date": item["read_at"].isoformat(),
                            "message": (
                                f"{meter_id} {readings.KIND_LABELS.get(kind, kind)}："
                                f"{label}在 {item['read_at']} 同时给出 "
                                f"{previous['value']:g} 与 {item['value']:g} 两个读数，来源前后矛盾"
                            ),
                            "detail": {
                                "source": item["source"],
                                "values": [round(previous["value"], 3), round(item["value"], 3)],
                                "diff": diff,
                                "tolerance": tolerance,
                            },
                        }
                    )
                else:
                    conflicts.append(
                        {
                            "type": "cross_source_deviation",
                            "meter_id": meter_id,
                            "kind": kind,
                            "date": item["read_at"].isoformat(),
                            "message": (
                                f"{meter_id} {readings.KIND_LABELS.get(kind, kind)} {item['read_at']}："
                                f"现场抄表与自动采集同日读数偏差 {diff:g} kWh > 精度 {tolerance:g}，"
                                f"不发布新台账"
                            ),
                            "detail": {
                                "sources": [_raw_view(previous), _raw_view(item)],
                                "date_span_days": 0,
                                "diff": diff,
                                "tolerance": tolerance,
                            },
                        }
                    )
                # 矛盾时以主来源（现场抄表）为准
                if item["source"] == primary_source and previous["source"] != primary_source:
                    by_day[item["read_at"]] = item
                    consolidated[consolidated.index(previous)] = item
            # 精度内一致：保留精度更细的一路
            elif item["precision"] > previous["precision"]:
                by_day[item["read_at"]] = item
                consolidated[consolidated.index(previous)] = item
        canonical = consolidated

        canonical.sort(key=lambda item: item["read_at"])
        series[(meter_id, kind)] = [
            {"read_at": item["read_at"], "value": item["value"]} for item in canonical
        ]

    all_sources = sorted({row.get("source") or readings.DEFAULT_SOURCE for row in rows})
    merged = sum(1 for item in point_evidence if item["merged"])
    return {
        "series": series,
        "points": point_evidence,
        "conflicts": conflicts,
        "summary": {
            "raw_rows": len(rows),
            "aligned_points": len(point_evidence),
            "merged_points": merged,
            "manual_points": sum(1 for item in point_evidence if item["source"] == "manual"),
            "auto_points": sum(1 for item in point_evidence if item["source"] == "auto"),
            "sources": all_sources,
            "conflicts": len(conflicts),
            "cross_source_deviation": sum(
                1 for item in conflicts if item["type"] == "cross_source_deviation"
            ),
            "same_source_conflict": sum(
                1 for item in conflicts if item["type"] == "same_source_conflict"
            ),
            "align_days": align_days,
            "precision": tolerance_floor,
        },
    }
