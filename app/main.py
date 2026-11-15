"""FastAPI 路由（薄）：页面渲染 + JSON API。规则全在 readings/restore/proration/validation/summary。"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import pipeline, proration, publish, readings, seed, settings, store, summary

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


def _fmt(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


templates.env.filters["num"] = lambda value: _fmt(value, 2)
templates.env.filters["num1"] = lambda value: _fmt(value, 1)
templates.env.filters["pct"] = lambda value: "—" if value is None else f"{float(value) * 100:+.2f}%"
templates.env.filters["signed"] = lambda value: "—" if value is None else f"{float(value):+,.2f}"

_CACHE: dict[str, Any] = {"key": None, "value": None}


@asynccontextmanager
async def lifespan(application: FastAPI):
    with store.session() as conn:
        application.state.seed_result = seed.seed(conn)
    yield


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def load_data() -> tuple[list[dict], list[dict], list[dict]]:
    with store.session() as conn:
        store.init_schema(conn)
        rows = store.all_readings(conn)
        meters = store.all_meters(conn)
        changes = store.all_changes(conn)
    return rows, meters, changes


def cache_key(rows: list[dict], meters: list[dict], changes: list[dict]) -> str:
    span = f"{rows[0]['read_at']}~{rows[-1]['read_at']}" if rows else "empty"
    digest = hashlib.sha256(
        json.dumps(
            [
                [
                    row["meter_id"],
                    row["read_at"].isoformat(),
                    row["kind"],
                    row.get("source"),
                    round(float(row["reading_kwh"]), 3),
                ]
                for row in rows
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{len(rows)}|{len(meters)}|{len(changes)}|{span}|{digest}|{settings.SEED_VERSION}"


def analyze_all(force: bool = False) -> dict:
    """全量还原一次并缓存（导入后 force=True 失效缓存）。"""
    rows, meters, changes = load_data()
    key = cache_key(rows, meters, changes)
    if not force and _CACHE["key"] == key and _CACHE["value"] is not None:
        return _CACHE["value"]
    result = pipeline.analyze(rows, meters, changes)
    _CACHE["key"] = key
    _CACHE["value"] = result
    return result


def base_context(request: Request, **extra: Any) -> dict:
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "page": "",
        "active": "",
        "error": None,
        "notice": None,
        "seed_result": getattr(app.state, "seed_result", None),
        "full_scale": settings.DEFAULT_FULL_SCALE,
        "max_gap_days": settings.MAX_GAP_DAYS,
        "tou_tolerance": settings.TOU_TOLERANCE,
        "align_days": settings.SOURCE_ALIGN_DAYS,
        "reading_precision": settings.READING_PRECISION,
    }
    context.update(extra)
    return context


def _source_counts(rows: list[dict]) -> dict:
    counts = {"manual": 0, "auto": 0}
    for row in rows:
        source = row.get("source") or readings.DEFAULT_SOURCE
        counts[source] = counts.get(source, 0) + 1
    return counts


def pipeline_error(exc: pipeline.PipelineError) -> HTTPException:
    detail = exc.message
    if exc.details:
        detail = f"{detail}（{exc.details[0]['message']}）"
    return HTTPException(status_code=exc.status, detail=detail)


def _ledger_sources(rows: list[dict], selected: list[str]) -> list[dict]:
    """台账口径里的来源构成（现场抄表 / 自动采集各多少行）。"""
    counts: dict[str, int] = {}
    for row in rows:
        if row["meter_id"] not in selected:
            continue
        source = row.get("source") or readings.DEFAULT_SOURCE
        counts[source] = counts.get(source, 0) + 1
    return [
        {
            "source": source,
            "label": readings.SOURCE_LABELS.get(source, source),
            "rows": count,
        }
        for source, count in sorted(counts.items())
    ]


def _active_version_view() -> dict | None:
    with store.session() as conn:
        store.init_schema(conn)
        active = store.active_ledger(conn)
        history = store.ledger_history(conn, limit=10)
    if not active:
        return None
    return {
        "version_id": active["version_id"],
        "batch_key": active["batch_key"],
        "created_at": active["created_at"],
        "month": active["month"],
        "run_id": active["run_id"],
        "month_order": active["month_order"],
        "history": len(history),
    }


# --------------------------------------------------------------------------- 请求模型


class RestoreRequest(BaseModel):
    month: str = Field(..., examples=["2026-03"])
    meter_ids: list[str] = Field(default_factory=list)
    full_scale: float | None = None
    max_gap_days: int | None = None
    align_days: int | None = Field(
        default=None, ge=0, le=7, description="现场抄表/自动采集同一读数点的最大日期偏差（天）"
    )
    reading_precision: float | None = Field(
        default=None, gt=0, description="两路来源对齐后的读数精度容差（kWh）"
    )
    allow_publish: bool = Field(
        default=True, description="核对通过后是否发布新台账；来源偏差超精度时发布会被拒绝"
    )


# --------------------------------------------------------------------------- 页面


@app.get("/health")
def health() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        readings_count = store.count_readings(conn)
        meters_count = len(store.all_meters(conn))
        changes_count = store.count_changes(conn)
        span = store.reading_span(conn)
    return {
        "status": "ok",
        "app": settings.APP_SHORT,
        "readings": readings_count,
        "meters": meters_count,
        "changes": changes_count,
        "readings_from": span["date_from"],
        "readings_to": span["date_to"],
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    rows, meters, changes = load_data()
    ledger = None
    error = None
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        error = exc.message
    stats = ledger["stats"] if ledger else {}
    months = ledger["month_order"] if ledger else []
    with store.session() as conn:
        runs = store.list_runs(conn, limit=6)
    context = base_context(
        request,
        page="index",
        active="index",
        runs=runs,
        latest_run=runs[0] if runs else None,
        error=error,
        stats=stats,
        months=months,
        month_count=len(months),
        monitored=ledger["monitored"] if ledger else [],
        latest_month=months[-1] if months else "",
        incidents=[
            {"module": "换表/回绕识别", "count": stats.get("meter_changes", 0), "unit": "次换表分段",
             "detail": "旧表拆表读数 + 新表装表读数两段相加"},
            {"module": "换表/回绕识别", "count": stats.get("rollovers", 0), "unit": "次满量程回绕",
             "detail": "量程 − 前读数 + 后读数"},
            {"module": "反复核清单", "count": stats.get("gaps", 0), "unit": "段抄表缺口",
             "detail": "间隔 > 31 天，只报区间与建议抄表日，不插值"},
            {"module": "用量还原", "count": stats.get("isolated_anomalies", 0), "unit": "条无法判定异常",
             "detail": "读数回退且无换表记录 → 隔离，不参与汇总"},
            {"module": "分时校验", "count": stats.get("review_p1", 0), "unit": "条 P1 复核项",
             "detail": "峰+谷 与总量偏差 > 0.5% 即记异常"},
        ],
        readings_rows=len(rows),
    )
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request) -> HTMLResponse:
    rows, meters, changes = load_data()
    ledger = None
    error = None
    try:
        ledger = analyze_all()
    except pipeline.PipelineError as exc:
        error = exc.message
    months = ledger["month_order"] if ledger else []
    with store.session() as conn:
        runs = store.list_runs(conn, limit=8)
        events = store.list_events(conn, limit=12)
    context = base_context(
        request,
        page="workbench",
        active="workbench",
        error=error,
        meters=meters,
        readings_rows=len(rows),
        changes_count=len(changes),
        months=months,
        default_month=months[-1] if months else "2026-03",
        runs=runs,
        events=events,
        ledger_stats=ledger["stats"] if ledger else {},
        monitored=ledger["monitored"] if ledger else [],
        source_counts=_source_counts(rows),
        active_version=_active_version_view(),
    )
    return templates.TemplateResponse(request, "workbench.html", context)


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result_page(request: Request, run_id: str) -> HTMLResponse:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    context = base_context(
        request,
        page="result",
        active="result",
        run=run,
        run_id=run_id,
        error=None if run else f"还原批次 {run_id} 不存在，请回到工作台重新执行",
    )
    return templates.TemplateResponse(request, "result.html", context)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, month: str | None = None) -> HTMLResponse:
    rows, meters, changes = load_data()
    ledger = None
    error = None
    source = "live"
    try:
        ledger, source = ledger_for_export()
        if source == "live":
            # 还没有发布版本时，复核页退化为实时还原结果（与导出保持一致）
            ledger = analyze_all()
    except pipeline.PipelineError as exc:
        error = exc.message
    months = ledger["month_order"] if ledger else []
    focus_month = month if month in months else (months[-1] if months else "")
    with store.session() as conn:
        runs = store.list_runs(conn, limit=10)
        events = store.list_events(conn, limit=20)
        versions = store.ledger_history(conn, limit=10)
    context = base_context(
        request,
        page="review",
        active="review",
        error=error,
        ledger=ledger,
        months=months,
        focus_month=focus_month,
        review_items=ledger["review"] if ledger else [],
        runs=runs,
        events=events,
        readings_rows=len(rows),
        changes_count=len(changes),
        source_counts=_source_counts(rows),
        active_version=_active_version_view(),
        versions=versions,
        ledger_source=source,
    )
    return templates.TemplateResponse(request, "review.html", context)


# --------------------------------------------------------------------------- 导入


@app.post("/api/import/sample")
def api_import_sample(include_auto: bool = False) -> JSONResponse:
    with store.session() as conn:
        store.init_schema(conn)
        sample = seed.load_sample_files(include_auto=include_auto)
        result = seed.seed(conn, sample=sample, force=True)
        counts = {
            "readings": store.count_readings(conn),
            "meters": len(store.all_meters(conn)),
            "changes": store.count_changes(conn),
        }
        sources = {}
        for row in store.all_readings(conn):
            sources[row["source"]] = sources.get(row["source"], 0) + 1
    analyze_all(force=True)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "seeded": result["seeded"],
            "reason": result["reason"],
            "stats": sample["stats"],
            "counts": counts,
            "sources": sources,
            "include_auto": include_auto,
            "source": settings.SEED_SOURCE,
        },
    )


@app.post("/api/import")
async def api_import(file: UploadFile = File(...)) -> JSONResponse:
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="读数文件编码无法识别，请另存为 UTF-8 或 GBK") from exc
    try:
        parsed = readings.parse_csv(text)
    except readings.ReadingError as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": exc.message, "problems": exc.problems, "filename": file.filename},
        )

    now = now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        replaced = store.replace_readings(conn, parsed["rows"], now)
        known = {meter["meter_id"] for meter in store.all_meters(conn)}
        new_meters = [
            {
                "meter_id": meter_id,
                "name": f"表 {meter_id}",
                "location": "导入登记",
                "full_scale": settings.DEFAULT_FULL_SCALE,
                "ct_ratio": 1.0,
            }
            for meter_id in parsed["stats"]["meters"]
            if meter_id not in known
        ]
        if new_meters:
            store.upsert_meters(conn, new_meters)
        store.add_event(
            conn,
            "import",
            f"导入读数 {replaced} 行（{file.filename or '上传文件'}）",
            parsed["stats"],
            now,
        )
    analyze_all(force=True)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "filename": file.filename,
            "replaced_rows": replaced,
            "stats": parsed["stats"],
            "new_meters": [item["meter_id"] for item in new_meters],
        },
    )


# --------------------------------------------------------------------------- 还原


@app.post("/api/restore")
def api_restore(payload: RestoreRequest = Body(...)) -> JSONResponse:
    try:
        proration.parse_month(payload.month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows, meters, changes = load_data()
    if not rows:
        raise HTTPException(status_code=400, detail="读数库为空，请先导入读数 CSV 或 /api/import/sample")
    full_scale = payload.full_scale or settings.DEFAULT_FULL_SCALE
    if full_scale <= 0:
        raise HTTPException(status_code=400, detail=f"量程必须为正数，实际 {full_scale}")
    max_gap_days = payload.max_gap_days or settings.MAX_GAP_DAYS
    if max_gap_days < 2:
        raise HTTPException(status_code=400, detail="缺口阈值至少 2 天")

    try:
        result = pipeline.analyze(
            rows,
            meters,
            changes,
            month=payload.month,
            meter_ids=payload.meter_ids,
            full_scale=full_scale,
            max_gap_days=max_gap_days,
            align_days=payload.align_days,
            reading_precision=payload.reading_precision,
        )
    except pipeline.PipelineError as exc:
        raise pipeline_error(exc) from exc

    focus = result["focus"]
    alignment = result["alignment"]
    if alignment["publish_blocked"] and payload.allow_publish:
        # 核对未通过：不写入任何新台账，活动台账保持上一版
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "published": False,
                "month": payload.month,
                "error": alignment["blocked_reason"],
                "conflicts": alignment["conflicts"],
                "alignment": alignment["summary"],
                "active_version": _active_version_view(),
                "totals": focus["totals"],
            },
        )
    if alignment["publish_blocked"]:
        # 显式只核对不发布：返回分析数值，但不产生台账版本
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "published": False,
                "month": payload.month,
                "meters": result["selected"],
                "totals": focus["totals"],
                "alignment": {
                    "summary": alignment["summary"],
                    "points": alignment["points"],
                    "conflicts": alignment["conflicts"],
                    "publish_blocked": True,
                    "blocked_reason": alignment["blocked_reason"],
                },
                "rows": [
                    {
                        "meter_id": row["meter_id"],
                        "meter_name": row["meter_name"],
                        "total": row["total"],
                        "peak": row["peak"],
                        "valley": row["valley"],
                        "gap_days": row["gap_days"],
                        "anomaly_count": row["anomaly_count"],
                        "status": row["status"],
                        "status_label": row["status_label"],
                    }
                    for row in focus["rows"]
                ],
                "review": focus["review"],
                "active_version": _active_version_view(),
            },
        )

    run_id = f"run-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    created_at = now_iso()
    batch_key_value = publish.batch_key(
        month=payload.month,
        selected=result["selected"],
        full_scale=full_scale,
        max_gap_days=max_gap_days,
        tou_tolerance=result["tou_tolerance"],
        rows=rows,
        changes=changes,
        align_days=alignment["summary"]["align_days"],
        reading_precision=alignment["summary"]["precision"],
    )
    run_payload = {
        "run_id": run_id,
        "created_at": created_at,
        "month": payload.month,
        "month_start": focus["month_start"],
        "month_end": focus["month_end"],
        "selected": result["selected"],
        "available": result["available"],
        "full_scale": result["full_scale"],
        "max_gap_days": result["max_gap_days"],
        "tou_tolerance": result["tou_tolerance"],
        "focus": focus,
        "review": focus["review"],
        "ledger": {month: result["months"][month]["rows"] for month in result["month_order"]},
        "month_order": result["month_order"],
        "stats": result["stats"],
        "monitored": result["monitored"],
        "alignment": alignment,
        "batch_key": batch_key_value,
    }
    ledger_payload = {
        "month_order": result["month_order"],
        "months": {
            month: result["months"][month]["rows"] for month in result["month_order"]
        },
        "stats": result["stats"],
        "review": result["review"],
        "selected": result["selected"],
        "monitored": result["monitored"],
        "alignment": alignment,
        "run_id": run_id,
        "month": payload.month,
        "batches": _ledger_sources(rows, result["selected"]),
    }
    with store.session() as conn:
        store.init_schema(conn)
        active_before = store.active_ledger(conn)
        previous_version_id = active_before["version_id"] if active_before else None
    try:
        with store.session() as conn:
            store.init_schema(conn)
            previous_run = store.find_run_by_batch(conn, batch_key_value)
            if previous_run:
                run_payload["run_id"] = previous_run["run_id"]
                run_id = previous_run["run_id"]
                ledger_payload["run_id"] = run_id
            store.save_run(conn, run_payload)
            info = publish.publish(
                conn,
                batch_key_value=batch_key_value,
                run_id=run_id,
                month=payload.month,
                month_order=result["month_order"],
                payload=ledger_payload,
            )
            run_payload["ledger_version"] = info["version_id"]
            run_payload["previous_version"] = info["previous_version_id"]
            run_payload["ledger_reused"] = info["reused"]
            store.save_run(conn, run_payload)
            store.add_event(
                conn,
                "restore",
                (
                    f"还原 {payload.month}：{len(focus['rows'])} 块表，总用量 {focus['totals']['usage_total']:g} kWh；"
                    f"台账版本 {info['version_id']}"
                    + ("（同批重试，复用已有版本，不重复计量）" if info["reused"] else " 已发布")
                ),
                {
                    "run_id": run_id,
                    "batch_key": batch_key_value,
                    "month": payload.month,
                    "meters": result["selected"],
                    "totals": focus["totals"],
                    "ledger_version": info["version_id"],
                    "reused_version": info["reused"],
                },
                created_at,
            )
    except Exception as exc:
        # 版本写入/指针切换/批次/事件在同一事务里，失败已整体回滚；这里只补记恢复事件
        with store.session() as conn:
            store.init_schema(conn)
            publish.record_failure(conn, previous_version_id, exc)
        raise

    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "published": True,
            "run_id": run_id,
            "batch_key": batch_key_value,
            "ledger_version": info["version_id"],
            "ledger_reused": info["reused"],
            "previous_version": info["previous_version_id"],
            "active_version": _active_version_view(),
            "month": payload.month,
            "meters": result["selected"],
            "totals": focus["totals"],
            "alignment": {
                "summary": alignment["summary"],
                "points": alignment["points"],
                "conflicts": alignment["conflicts"],
                "publish_blocked": alignment["publish_blocked"],
            },
            "rows": [
                {
                    "meter_id": row["meter_id"],
                    "meter_name": row["meter_name"],
                    "total": row["total"],
                    "peak": row["peak"],
                    "valley": row["valley"],
                    "gap_days": row["gap_days"],
                    "anomaly_count": row["anomaly_count"],
                    "status": row["status"],
                    "status_label": row["status_label"],
                }
                for row in focus["rows"]
            ],
            "anomalies": focus["anomalies"],
            "gaps": focus["gaps"],
            "changes": [
                {
                    "meter_id": item["meter_id"],
                    "start": item["start"],
                    "end": item["end"],
                    "usage": item["usage"],
                    "formula": item["formula"],
                }
                for item in focus["changes"]
            ],
            "rollovers": [
                {
                    "meter_id": item["meter_id"],
                    "start": item["start"],
                    "end": item["end"],
                    "usage": item["usage"],
                    "formula": item["formula"],
                }
                for item in focus["rollovers"]
            ],
            "review": focus["review"],
            "result_url": f"/result/{run_id}",
            "export_url": f"/api/runs/{run_id}/ledger.csv",
        },
    )


@app.get("/api/runs")
def api_runs() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        runs = store.list_runs(conn, limit=30)
    return {"ok": True, "runs": runs}


@app.get("/api/runs/{run_id}")
def api_run(run_id: str) -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"还原批次 {run_id} 不存在")
    return {"ok": True, "run": run}


# --------------------------------------------------------------------------- 导出


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/runs/{run_id}/ledger.csv")
def api_run_ledger(run_id: str) -> Response:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"还原批次 {run_id} 不存在")
    month = run["month"]
    return _csv_response(
        summary.ledger_csv({month: run["focus"]}),
        f"metercalc-ledger-{month}.csv",
    )


# --------------------------------------------------------------------------- 台账版本（发布 / 回滚）


@app.get("/api/ledger/versions")
def api_ledger_versions() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        active = store.active_ledger(conn)
        history = store.ledger_history(conn, limit=30)
    return {
        "ok": True,
        "active_version": active["version_id"] if active else None,
        "versions": [
            {**item, "active": bool(active and item["version_id"] == active["version_id"])}
            for item in history
        ],
    }


@app.get("/api/ledger/active")
def api_ledger_active() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        active = store.active_ledger(conn)
    if not active:
        return {"ok": True, "active": None}
    return {
        "ok": True,
        "active": {
            "version_id": active["version_id"],
            "batch_key": active["batch_key"],
            "created_at": active["created_at"],
            "month": active["month"],
            "run_id": active["run_id"],
            "month_order": active["month_order"],
            "stats": (active["payload"] or {}).get("stats", {}),
            "alignment": (active["payload"] or {}).get("alignment", {}).get("summary", {}),
        },
    }


@app.post("/api/ledger/rollback")
def api_ledger_rollback(payload: dict = Body(default=None)) -> JSONResponse:
    version_id = (payload or {}).get("version_id")
    with store.session() as conn:
        store.init_schema(conn)
        try:
            target = publish.rollback(conn, version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc).strip("'\"")) from exc
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "rolled_back_to": target["version_id"],
            "run_id": target["run_id"],
            "month": target["month"],
            "month_order": target["month_order"],
            "active_version": _active_version_view(),
        },
    )


@app.get("/api/runs/{run_id}/review.csv")
def api_run_review(run_id: str) -> Response:
    with store.session() as conn:
        store.init_schema(conn)
        run = store.load_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"还原批次 {run_id} 不存在")
    return _csv_response(summary.review_csv(run["review"]), f"metercalc-review-{run['month']}.csv")


def ledger_for_export() -> tuple[dict, str]:
    """导出/复核用台账：优先取已发布的活动版本；没有发布版本时才用实时还原结果。"""
    with store.session() as conn:
        store.init_schema(conn)
        active = store.active_ledger(conn)
    if active:
        payload = active["payload"] or {}
        month_order = payload.get("month_order") or sorted((payload.get("months") or {}).keys())
        month_rows = payload.get("months") or {}
        return (
            {
                "months": {month: {"rows": month_rows.get(month, [])} for month in month_order},
                "month_order": month_order,
                "review": payload.get("review") or [],
                "stats": payload.get("stats") or {},
                "monitored": payload.get("monitored") or [],
                "selected": payload.get("selected") or [],
            },
            f"active:{active['version_id']}",
        )
    ledger = analyze_all()
    return ledger, "live"


@app.get("/api/ledger.csv")
def api_ledger() -> Response:
    try:
        ledger, _src = ledger_for_export()
    except pipeline.PipelineError as exc:
        raise pipeline_error(exc) from exc
    return _csv_response(summary.ledger_csv(ledger["months"]), "metercalc-ledger-all.csv")


@app.get("/api/review.csv")
def api_review() -> Response:
    try:
        ledger, _src = ledger_for_export()
    except pipeline.PipelineError as exc:
        raise pipeline_error(exc) from exc
    return _csv_response(summary.review_csv(ledger["review"]), "metercalc-review-all.csv")
