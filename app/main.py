"""地籍变更 Web 应用：导入、预核验、确认提交、现势/历史/变更记录查询。"""
from __future__ import annotations

import os
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, rounding
from .geometry import GeoValidationError, PlanValidationError

DB_PATH = os.environ.get("CADASTRE_DB", "/data/cadastre.db")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def get_conn() -> db.sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    return conn


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = get_conn()
    conn.close()
    yield


app = FastAPI(title="地籍变更系统", version="1.0", lifespan=lifespan)


@app.exception_handler(GeoValidationError)
def _geo_error_handler(_: Request, exc: GeoValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "数据校验失败", "detail": str(exc)})


@app.exception_handler(PlanValidationError)
def _plan_error_handler(_: Request, exc: PlanValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "变更前提不成立", "detail": str(exc)})


@app.exception_handler(db.VersionConflictError)
def _conflict_handler(_: Request, exc: db.VersionConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "版本冲突", "detail": str(exc)})


@app.exception_handler(db.RoundNotFoundError)
def _round_not_found_handler(_: Request, exc: db.RoundNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "整数表不存在", "detail": str(exc)})


@app.exception_handler(rounding.RoundValidationError)
def _round_validation_handler(_: Request, exc: rounding.RoundValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "明细校验失败", "detail": str(exc)})


@app.exception_handler(rounding.RoundInfeasibleError)
def _round_infeasible_handler(_: Request, exc: rounding.RoundInfeasibleError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "取整无解", "detail": str(exc)})


@app.get("/api/state")
def state() -> dict:
    conn = get_conn()
    try:
        parcels = db.list_current(conn)
        return {
            "current_version": db.current_version(conn),
            "parcel_count": len(parcels),
            "parcels": parcels,
        }
    finally:
        conn.close()


@app.post("/api/import")
async def import_geojson(request: Request) -> dict:
    payload = await request.json()
    conn = get_conn()
    try:
        return db.import_initial(conn, payload)
    finally:
        conn.close()


@app.post("/api/changes/plan")
async def plan_change(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.plan(conn, req)
    finally:
        conn.close()


@app.post("/api/changes/commit")
async def commit_change(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.commit_change(conn, req)
    finally:
        conn.close()


@app.get("/api/history/parcel/{parcel_id}")
def history_parcel(parcel_id: str) -> dict:
    conn = get_conn()
    try:
        return db.parcel_history(conn, parcel_id)
    finally:
        conn.close()


@app.get("/api/history/version/{version}")
def history_version(version: int) -> dict:
    if version < 1:
        raise PlanValidationError("版本号必须 >= 1")
    conn = get_conn()
    try:
        return {
            "version": version,
            "parcels": db.snapshot_at(conn, version),
        }
    finally:
        conn.close()


@app.get("/api/txns")
def txns() -> dict:
    conn = get_conn()
    try:
        return {"txns": db.list_txns(conn)}
    finally:
        conn.close()


@app.get("/api/txns/{txn_id}")
def txn_detail(txn_id: int) -> dict:
    conn = get_conn()
    try:
        return db.get_txn(conn, txn_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- 整数表

@app.get("/api/round/versions")
def round_versions() -> dict:
    """各地籍版本清单（含是否已建整数表）。"""
    conn = get_conn()
    try:
        return {"versions": db.list_round_versions(conn)}
    finally:
        conn.close()


@app.get("/api/round/preview/{version}")
def round_preview(version: int) -> dict:
    """某版登记面积清单，供准备 CSV 用。"""
    conn = get_conn()
    try:
        db.check_version(conn, version)
        return {"version": version, "parcels": db.snapshot_parcels(conn, version)}
    finally:
        conn.close()


@app.post("/api/round/check")
async def round_check(request: Request) -> dict:
    """预校验 CSV 明细（不落库），返回地类清单供录入合计。"""
    req = await request.json()
    conn = get_conn()
    try:
        snap, cells, classes = db.check_round_details(conn, req.get("version"), req.get("csv"))
        return {
            "version": req.get("version"),
            "land_classes": classes,
            "parcels": [
                {
                    "parcel_id": p["parcel_id"],
                    "registered_area": p["area"],
                    "row_total": rounding.round_half_up(p["area"]),
                    "csv_sum": round(
                        sum(v for (pid, _), v in cells.items() if pid == p["parcel_id"]), 6
                    ),
                }
                for p in snap
            ],
        }
    finally:
        conn.close()


@app.post("/api/round/create")
async def round_create(request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.create_round_table(conn, req.get("version"), req.get("csv"), req.get("totals"))
    finally:
        conn.close()


@app.get("/api/round/{version}")
def round_get(version: int) -> dict:
    conn = get_conn()
    try:
        return db.get_round_table(conn, version)
    finally:
        conn.close()


@app.post("/api/round/{version}/errata")
async def round_errata(version: int, request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.errata_round_table(conn, version, req.get("csv"), req.get("totals"))
    finally:
        conn.close()


@app.post("/api/round/{version}/lock")
async def round_lock(version: int, request: Request) -> dict:
    req = await request.json()
    conn = get_conn()
    try:
        return db.set_round_lock(
            conn, version, req.get("parcel_id"), req.get("locked"), req.get("expected")
        )
    finally:
        conn.close()


@app.get("/api/round/{version}/download")
def round_download(version: int) -> Response:
    conn = get_conn()
    try:
        text = db.round_table_csv(conn, version)
    finally:
        conn.close()
    return Response(
        content="\ufeff" + text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="round_v{version}.csv"'
        },
    )


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
