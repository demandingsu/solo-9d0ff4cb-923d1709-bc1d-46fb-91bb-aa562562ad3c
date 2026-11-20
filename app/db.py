"""SQLite 持久层：现势宗地、历史版本与变更记录。

并发控制：每次提交必须携带 base_version，写入时在 IMMEDIATE 事务内
复核当前版本；版本不一致即拒绝并整体回滚，保证同一版上的两笔提交
最多一笔生效，且任何失败都不改动现势与历史数据。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from shapely.geometry import mapping
from shapely import wkt as shapely_wkt

from .geometry import (
    GeoValidationError,
    PlanValidationError,
    ensure_no_overlap,
    parse_feature_collection,
    parse_polygon,
    plan_change,
)
from . import rounding

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 宗地版本行：append-only。同一 parcel_id 每个生效版本一行；
-- status 表示该宗地在该版本结束时的状态（current / retired），旧行永不 UPDATE。
CREATE TABLE IF NOT EXISTS parcel_versions (
    parcel_id      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('current', 'retired')),
    geom_wkt       TEXT NOT NULL,
    area           REAL NOT NULL,
    created_txn_id INTEGER NOT NULL,
    retired_txn_id INTEGER,
    PRIMARY KEY (parcel_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pv_status ON parcel_versions(status);
CREATE TABLE IF NOT EXISTS txns (
    txn_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('import', 'change')),
    base_version INTEGER NOT NULL,
    new_version  INTEGER NOT NULL,
    reason       TEXT,
    report_json  TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS txn_items (
    txn_id    INTEGER NOT NULL REFERENCES txns(txn_id),
    role      TEXT NOT NULL CHECK (role IN ('retired', 'new')),
    parcel_id TEXT NOT NULL,
    geom_wkt  TEXT NOT NULL,
    area      REAL NOT NULL
);
-- 分项面积整数表：每个地籍版本最多一张（version 唯一）。
-- 行 = 宗地（含锁定标志），格 = 宗地 × 地类（缺项按 0 也落库），
-- 列合计 = 资料员录入值。勘误整体重写明细与整数，锁定行保留原数。
CREATE TABLE IF NOT EXISTS round_tables (
    table_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    version    INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS round_rows (
    table_id        INTEGER NOT NULL REFERENCES round_tables(table_id),
    parcel_id       TEXT NOT NULL,
    registered_area REAL NOT NULL,
    row_total       INTEGER NOT NULL,
    locked          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (table_id, parcel_id)
);
CREATE TABLE IF NOT EXISTS round_cells (
    table_id   INTEGER NOT NULL REFERENCES round_tables(table_id),
    parcel_id  TEXT NOT NULL,
    land_class TEXT NOT NULL,
    orig_area  REAL NOT NULL,
    int_area   INTEGER NOT NULL,
    PRIMARY KEY (table_id, parcel_id, land_class)
);
CREATE TABLE IF NOT EXISTS round_totals (
    table_id   INTEGER NOT NULL REFERENCES round_tables(table_id),
    land_class TEXT NOT NULL,
    total      INTEGER NOT NULL,
    PRIMARY KEY (table_id, land_class)
);
"""


class VersionConflictError(Exception):
    """base_version 与当前版本不一致（竞争提交的失败者）。"""


class RoundNotFoundError(Exception):
    """该地籍版本尚未建立整数表。"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'current_version'").fetchone()
    return int(row["value"]) if row else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parcel_to_geojson(row: sqlite3.Row) -> dict[str, Any]:
    geom = shapely_wkt.loads(row["geom_wkt"])
    return {
        "parcel_id": row["parcel_id"],
        "version": row["version"],
        "status": row["status"],
        "area": row["area"],
        "geometry": mapping(geom),
        "created_txn_id": row["created_txn_id"],
        "retired_txn_id": row["retired_txn_id"],
    }


def list_current(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pv.* FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        ORDER BY pv.parcel_id
        """
    ).fetchall()
    return [_parcel_to_geojson(r) for r in rows]


def _current_geoms(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT pv.parcel_id, pv.geom_wkt FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        """
    ).fetchall()
    return {r["parcel_id"]: shapely_wkt.loads(r["geom_wkt"]) for r in rows}


def parcel_history(conn: sqlite3.Connection, parcel_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM parcel_versions WHERE parcel_id = ? ORDER BY version",
        (parcel_id,),
    ).fetchall()
    if not rows:
        raise PlanValidationError(f"宗地号 {parcel_id} 无任何历史记录")
    return {
        "parcel_id": parcel_id,
        "versions": [_parcel_to_geojson(r) for r in rows],
    }


def snapshot_at(conn: sqlite3.Connection, version: int) -> list[dict]:
    """截至某版本的现势快照：每宗地取 <= version 的最新行，排除当时已退休者。"""
    rows = conn.execute(
        """
        SELECT pv.* FROM parcel_versions pv
        JOIN (SELECT parcel_id, MAX(version) AS mv
              FROM parcel_versions WHERE version <= ?
              GROUP BY parcel_id) latest
          ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
        WHERE pv.status = 'current'
        ORDER BY pv.parcel_id
        """,
        (version,),
    ).fetchall()
    return [_parcel_to_geojson(r) for r in rows]


def list_txns(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT txn_id, kind, base_version, new_version, reason, created_at "
        "FROM txns ORDER BY txn_id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_txn(conn: sqlite3.Connection, txn_id: int) -> dict:
    row = conn.execute("SELECT * FROM txns WHERE txn_id = ?", (txn_id,)).fetchone()
    if row is None:
        raise PlanValidationError(f"变更记录 {txn_id} 不存在")
    items = conn.execute(
        "SELECT role, parcel_id, area, geom_wkt FROM txn_items WHERE txn_id = ? ORDER BY role, parcel_id",
        (txn_id,),
    ).fetchall()
    return {
        **dict(row),
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
        "items": [
            {
                "role": it["role"],
                "parcel_id": it["parcel_id"],
                "area": it["area"],
                "geometry": mapping(shapely_wkt.loads(it["geom_wkt"])),
            }
            for it in items
        ],
    }


def import_initial(conn: sqlite3.Connection, payload: Any) -> dict:
    """初始导入：要求库内尚无现势宗地，整批校验通过才写入。"""
    if current_version(conn) != 0 or _current_geoms(conn):
        raise VersionConflictError("数据库已存在现势宗地，不能重复初始导入")

    parsed = parse_feature_collection(payload)
    overlaps = ensure_no_overlap(
        [(pid, geom) for pid, geom, _ in parsed], label="初始宗地"
    )
    if overlaps:
        raise GeoValidationError(
            "初始宗地之间存在面积重叠："
            + "；".join(o["message"] for o in overlaps)
        )

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO txns (kind, base_version, new_version, reason, report_json, created_at) "
            "VALUES ('import', 0, 1, ?, NULL, ?)",
            ("初始导入", _now()),
        )
        txn_id = cur.lastrowid
        for pid, geom, _ in parsed:
            conn.execute(
                "INSERT INTO parcel_versions (parcel_id, version, status, geom_wkt, area, created_txn_id) "
                "VALUES (?, 1, 'current', ?, ?, ?)",
                (pid, geom.wkt, geom.area, txn_id),
            )
            conn.execute(
                "INSERT INTO txn_items (txn_id, role, parcel_id, geom_wkt, area) "
                "VALUES (?, 'new', ?, ?, ?)",
                (txn_id, pid, geom.wkt, geom.area),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('current_version', '1')"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"txn_id": txn_id, "new_version": 1, "imported": len(parsed)}


def _parse_change_request(req: dict) -> tuple[list[str], list[tuple[str, Any]]]:
    if not isinstance(req, dict):
        raise GeoValidationError("请求体必须是 JSON 对象")
    retired_ids = req.get("retired_ids")
    new_parcels = req.get("new_parcels")
    if not isinstance(retired_ids, list) or not retired_ids:
        raise GeoValidationError("retired_ids 必须是非空数组（退出的宗地号）")
    if not isinstance(new_parcels, list) or not new_parcels:
        raise GeoValidationError("new_parcels 必须是非空数组（接替的新宗地）")

    retired_ids = [str(x) for x in retired_ids]
    if len(set(retired_ids)) != len(retired_ids):
        raise GeoValidationError("retired_ids 存在重复宗地号")

    new_items: list[tuple[str, Any]] = []
    new_ids: set[str] = set()
    for i, item in enumerate(new_parcels):
        if not isinstance(item, dict) or "parcel_id" not in item or "geometry" not in item:
            raise GeoValidationError(
                f"new_parcels[{i}] 必须包含 parcel_id 与 geometry"
            )
        nid = str(item["parcel_id"])
        if not nid.strip():
            raise GeoValidationError(f"new_parcels[{i}] 宗地号为空")
        if nid in new_ids:
            raise GeoValidationError(f"新宗地号重复：{nid}")
        new_ids.add(nid)
        geom = parse_polygon(item["geometry"], label=f"新宗地 {nid}")
        new_items.append((nid, geom))
    return retired_ids, new_items


def plan(conn: sqlite3.Connection, req: dict) -> dict:
    """预核验：不落库，返回缺口/侵占/越界/重叠明细供网页预览。"""
    retired_ids, new_items = _parse_change_request(req)
    current = _current_geoms(conn)

    missing = [pid for pid in retired_ids if pid not in current]
    if missing:
        raise PlanValidationError(f"退出宗地号在现势库中不存在：{', '.join(missing)}")
    collision = [nid for nid, _ in new_items if nid in current]
    if collision:
        raise PlanValidationError(
            f"新宗地号与现有宗地号冲突：{', '.join(collision)}"
        )

    retired = [(pid, current[pid]) for pid in retired_ids]
    surviving = [(pid, g) for pid, g in current.items() if pid not in set(retired_ids)]
    report = plan_change(retired, new_items, surviving)
    return {
        "base_version": current_version(conn),
        "retired_ids": retired_ids,
        "new_ids": [nid for nid, _ in new_items],
        **report.to_dict(),
    }


def commit_change(conn: sqlite3.Connection, req: dict) -> dict:
    """确认提交：版本检查 + 事务内复核 + 原子写入。"""
    base_version = req.get("base_version")
    if not isinstance(base_version, int) or isinstance(base_version, bool):
        raise GeoValidationError("base_version 必须是整数")
    retired_ids, new_items = _parse_change_request(req)
    reason = str(req.get("reason") or "").strip() or None

    try:
        conn.execute("BEGIN IMMEDIATE")
        ver = current_version(conn)
        if ver != base_version:
            raise VersionConflictError(
                f"版本冲突：请求基于 v{base_version}，当前已是 v{ver}，"
                "该笔提交未生效，请刷新现势数据后重新核验"
            )

        current = _current_geoms(conn)
        missing = [pid for pid in retired_ids if pid not in current]
        if missing:
            raise PlanValidationError(
                f"退出宗地号在现势库中不存在：{', '.join(missing)}"
            )
        collision = [nid for nid, _ in new_items if nid in current]
        if collision:
            raise PlanValidationError(
                f"新宗地号与现有宗地号冲突：{', '.join(collision)}"
            )

        retired_set = set(retired_ids)
        retired = [(pid, current[pid]) for pid in retired_ids]
        surviving = [(pid, g) for pid, g in current.items() if pid not in retired_set]
        report = plan_change(retired, new_items, surviving)
        if not report.valid:
            raise GeoValidationError("核验未通过：" + "；".join(report.errors))

        new_version = ver + 1
        cur = conn.execute(
            "INSERT INTO txns (kind, base_version, new_version, reason, report_json, created_at) "
            "VALUES ('change', ?, ?, ?, ?, ?)",
            (ver, new_version, reason, json.dumps(report.to_dict()), _now()),
        )
        txn_id = cur.lastrowid

        # 退出宗地：追加一条 retired 版本行（旧行原样保留作为历史依据）
        for pid in retired_ids:
            old = conn.execute(
                """
                SELECT pv.* FROM parcel_versions pv
                JOIN (SELECT parcel_id, MAX(version) AS mv FROM parcel_versions
                      WHERE parcel_id = ? GROUP BY parcel_id) latest
                  ON latest.parcel_id = pv.parcel_id AND latest.mv = pv.version
                """,
                (pid,),
            ).fetchone()
            conn.execute(
                "INSERT INTO parcel_versions "
                "(parcel_id, version, status, geom_wkt, area, created_txn_id, retired_txn_id) "
                "VALUES (?, ?, 'retired', ?, ?, ?, ?)",
                (pid, new_version, old["geom_wkt"], old["area"],
                 old["created_txn_id"], txn_id),
            )
            conn.execute(
                "INSERT INTO txn_items (txn_id, role, parcel_id, geom_wkt, area) "
                "VALUES (?, 'retired', ?, ?, ?)",
                (txn_id, pid, old["geom_wkt"], old["area"]),
            )
        # 新宗地：追加 current 版本行
        for nid, geom in new_items:
            conn.execute(
                "INSERT INTO parcel_versions "
                "(parcel_id, version, status, geom_wkt, area, created_txn_id) "
                "VALUES (?, ?, 'current', ?, ?, ?)",
                (nid, new_version, geom.wkt, geom.area, txn_id),
            )
            conn.execute(
                "INSERT INTO txn_items (txn_id, role, parcel_id, geom_wkt, area) "
                "VALUES (?, 'new', ?, ?, ?)",
                (txn_id, nid, geom.wkt, geom.area),
            )
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'current_version'", (str(new_version),)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "txn_id": txn_id,
        "base_version": base_version,
        "new_version": new_version,
        "retired_ids": retired_ids,
        "new_ids": [nid for nid, _ in new_items],
        "report": report.to_dict(),
    }


# ---------------------------------------------------------------- 整数表

def snapshot_parcels(conn: sqlite3.Connection, version: int) -> list[dict]:
    """某版登记面积清单（宗地号 + 面积），按宗地号排序。"""
    return [
        {"parcel_id": p["parcel_id"], "area": p["area"]}
        for p in snapshot_at(conn, version)
    ]


def check_version(conn: sqlite3.Connection, version) -> None:
    cur = current_version(conn)
    if cur < 1:
        raise PlanValidationError("现势库为空，请先导入宗地")
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= cur:
        raise PlanValidationError(f"版本号须为 1..{cur} 的整数")


def check_round_details(conn: sqlite3.Connection, version, csv_text):
    """预校验明细 CSV（不落库）：版本 + 宗地集合 + 每宗分项和。"""
    check_version(conn, version)
    snap = snapshot_parcels(conn, version)
    cells, _parcels, classes = rounding.parse_area_csv(csv_text)
    rounding.validate_details(cells, snap)
    return snap, cells, classes


def _prepare_round_input(conn: sqlite3.Connection, version, csv_text, totals):
    """公共校验：版本号、CSV 明细、地类合计。返回求解所需的全部结构。"""
    snap, cells, classes = check_round_details(conn, version, csv_text)
    totals_map = rounding.validate_totals(totals, classes)
    row_targets = {p["parcel_id"]: rounding.round_half_up(p["area"]) for p in snap}
    return snap, cells, classes, totals_map, row_targets


def _write_round_rows(conn, table_id, snap, cells, classes, totals_map,
                      row_targets, result, locked_rows):
    """整表重写行/格/合计（建表与勘误共用，须在事务内调用）。"""
    for p in snap:
        pid = p["parcel_id"]
        conn.execute(
            "INSERT INTO round_rows (table_id, parcel_id, registered_area, row_total, locked) "
            "VALUES (?, ?, ?, ?, ?)",
            (table_id, pid, p["area"], row_targets[pid], 1 if pid in locked_rows else 0),
        )
        for cls in classes:
            conn.execute(
                "INSERT INTO round_cells (table_id, parcel_id, land_class, orig_area, int_area) "
                "VALUES (?, ?, ?, ?, ?)",
                (table_id, pid, cls, cells.get((pid, cls), 0.0), result[(pid, cls)]),
            )
    for cls in classes:
        conn.execute(
            "INSERT INTO round_totals (table_id, land_class, total) VALUES (?, ?, ?)",
            (table_id, cls, totals_map[cls]),
        )


def create_round_table(conn: sqlite3.Connection, version, csv_text, totals) -> dict:
    """为某版地籍建立整数表：校验 → 求解 → 原子写入。每版仅一张。"""
    snap, cells, classes, totals_map, row_targets = _prepare_round_input(
        conn, version, csv_text, totals
    )
    result = rounding.solve_integer_table(
        cells, [p["parcel_id"] for p in snap], classes, row_targets, totals_map
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM round_tables WHERE version = ?", (version,)
        ).fetchone():
            raise VersionConflictError(f"v{version} 已建立整数表，如需修正请使用勘误")
        now = _now()
        cur = conn.execute(
            "INSERT INTO round_tables (version, created_at, updated_at) VALUES (?, ?, ?)",
            (version, now, now),
        )
        _write_round_rows(
            conn, cur.lastrowid, snap, cells, classes, totals_map,
            row_targets, result, set(),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_round_table(conn, version)


def get_round_table(conn: sqlite3.Connection, version: int) -> dict:
    t = conn.execute(
        "SELECT * FROM round_tables WHERE version = ?", (version,)
    ).fetchone()
    if t is None:
        raise RoundNotFoundError(f"v{version} 尚未建立整数表")
    tid = t["table_id"]
    rows = conn.execute(
        "SELECT * FROM round_rows WHERE table_id = ? ORDER BY parcel_id", (tid,)
    ).fetchall()
    cells = conn.execute(
        "SELECT parcel_id, land_class, orig_area, int_area FROM round_cells "
        "WHERE table_id = ?",
        (tid,),
    ).fetchall()
    totals = conn.execute(
        "SELECT land_class, total FROM round_totals WHERE table_id = ? ORDER BY rowid",
        (tid,),
    ).fetchall()
    classes = [r["land_class"] for r in totals]
    cell_map = {(c["parcel_id"], c["land_class"]): c for c in cells}
    return {
        "version": version,
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        "land_classes": classes,
        "totals": {r["land_class"]: r["total"] for r in totals},
        "rows": [
            {
                "parcel_id": r["parcel_id"],
                "registered_area": r["registered_area"],
                "row_total": r["row_total"],
                "locked": bool(r["locked"]),
                "cells": {
                    cls: {
                        "orig": cell_map[(r["parcel_id"], cls)]["orig_area"],
                        "int": cell_map[(r["parcel_id"], cls)]["int_area"],
                    }
                    for cls in classes
                },
            }
            for r in rows
        ],
        "grand_total": sum(r["row_total"] for r in rows),
    }


def errata_round_table(conn: sqlite3.Connection, version: int, csv_text, totals) -> dict:
    """勘误：替换明细并重解。锁定行保留原数，其余格子改动数最少。

    整个「读旧表 → 校验 → 求解 → 重写」在 IMMEDIATE 事务内完成；
    校验失败或无解时整体回滚，原明细与整数表保持不变。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        t = conn.execute(
            "SELECT * FROM round_tables WHERE version = ?", (version,)
        ).fetchone()
        if t is None:
            raise RoundNotFoundError(f"v{version} 尚未建立整数表，请先建表")
        tid = t["table_id"]
        snap, cells, classes, totals_map, row_targets = _prepare_round_input(
            conn, version, csv_text, totals
        )
        prev: dict[tuple[str, str], int] = {}
        locked_rows: set[str] = set()
        for r in conn.execute(
            "SELECT parcel_id, locked FROM round_rows WHERE table_id = ?", (tid,)
        ).fetchall():
            if r["locked"]:
                locked_rows.add(r["parcel_id"])
        for c in conn.execute(
            "SELECT parcel_id, land_class, int_area FROM round_cells WHERE table_id = ?",
            (tid,),
        ).fetchall():
            prev[(c["parcel_id"], c["land_class"])] = c["int_area"]

        result = rounding.solve_integer_table(
            cells, [p["parcel_id"] for p in snap], classes, row_targets, totals_map,
            prev=prev, fixed_rows=locked_rows,
        )
        changed = sum(1 for k, v in result.items() if v != prev.get(k, 0))

        conn.execute("DELETE FROM round_cells WHERE table_id = ?", (tid,))
        conn.execute("DELETE FROM round_totals WHERE table_id = ?", (tid,))
        conn.execute("DELETE FROM round_rows WHERE table_id = ?", (tid,))
        _write_round_rows(
            conn, tid, snap, cells, classes, totals_map,
            row_targets, result, locked_rows,
        )
        conn.execute(
            "UPDATE round_tables SET updated_at = ? WHERE table_id = ?", (_now(), tid)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    out = get_round_table(conn, version)
    out["changed_cells"] = changed
    return out


def set_round_lock(conn: sqlite3.Connection, version: int, parcel_id, locked, expected) -> dict:
    """锁定 / 解锁一行。expected 为页面当前显示的整行整数（乐观并发）：
    与库中不一致说明该行的数字已变，拒绝并返回 409。"""
    if not isinstance(locked, bool):
        raise GeoValidationError("locked 必须是布尔值")
    if not isinstance(parcel_id, str) or not parcel_id.strip():
        raise GeoValidationError("parcel_id 不能为空")
    if not isinstance(expected, dict):
        raise GeoValidationError("expected 必须是 {地类: 整数} 对象")
    want: dict[str, int] = {}
    for k, v in expected.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer():
            raise GeoValidationError(f"expected[{k!r}] 必须是整数")
        want[str(k)] = int(v)

    try:
        conn.execute("BEGIN IMMEDIATE")
        t = conn.execute(
            "SELECT * FROM round_tables WHERE version = ?", (version,)
        ).fetchone()
        if t is None:
            raise RoundNotFoundError(f"v{version} 尚未建立整数表")
        tid = t["table_id"]
        row = conn.execute(
            "SELECT 1 FROM round_rows WHERE table_id = ? AND parcel_id = ?",
            (tid, parcel_id),
        ).fetchone()
        if row is None:
            raise PlanValidationError(f"宗地 {parcel_id} 不在 v{version} 整数表中")
        current = {
            c["land_class"]: c["int_area"]
            for c in conn.execute(
                "SELECT land_class, int_area FROM round_cells "
                "WHERE table_id = ? AND parcel_id = ?",
                (tid, parcel_id),
            ).fetchall()
        }
        if current != want:
            raise VersionConflictError(
                f"宗地 {parcel_id} 的整数已变化（他人已勘误或改动），"
                "请刷新整数表后再锁定"
            )
        conn.execute(
            "UPDATE round_rows SET locked = ? WHERE table_id = ? AND parcel_id = ?",
            (1 if locked else 0, tid, parcel_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_round_table(conn, version)


def list_round_versions(conn: sqlite3.Connection) -> list[dict]:
    cur = current_version(conn)
    have = {r["version"] for r in conn.execute("SELECT version FROM round_tables")}
    return [
        {"version": v, "parcel_count": len(snapshot_at(conn, v)), "has_table": v in have}
        for v in range(1, cur + 1)
    ]


def round_table_csv(conn: sqlite3.Connection, version: int) -> str:
    return rounding.build_table_csv(get_round_table(conn, version))
