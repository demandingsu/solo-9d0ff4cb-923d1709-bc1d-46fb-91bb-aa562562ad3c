"""分项面积整数表：建表校验、行列合计、锁定冲突、勘误最小改动、失败回滚。"""
import os
import tempfile

import pytest

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ.setdefault("CADASTRE_DB", _db_path)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app import db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例独立数据库文件，互不污染（也不影响 test_api 的库）。"""
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "round.db"))
    with TestClient(main.app) as c:
        yield c


P1_RING = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]          # 面积 100
P2_RING = [[10, 0], [20, 0], [20, 10], [10, 10], [10, 0]]       # 面积 100
TRI_RING = [[20, 0], [21, 0], [20, 1], [20, 0]]                 # 面积 0.5


def import_parcels(client, rings=("P1", "P2")):
    feats = []
    for pid in rings:
        ring = {"P1": P1_RING, "P2": P2_RING, "P3": TRI_RING}[pid]
        feats.append({
            "type": "Feature",
            "properties": {"parcel_id": pid},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    r = client.post("/api/import", json={"type": "FeatureCollection", "features": feats})
    assert r.status_code == 200, r.text
    return r.json()["new_version"]


def csv_text(rows):
    return "宗地号,地类,面积\n" + "\n".join(f"{p},{c},{a}" for p, c, a in rows)


# P1: 耕地33.5/园地66.5，P2: 耕地33/园地67 → 唯一解 P1 34/66，P2 33/67
BASE_ROWS = [("P1", "耕地", 33.5), ("P1", "园地", 66.5),
             ("P2", "耕地", 33.0), ("P2", "园地", 67.0)]
BASE_TOTALS = {"耕地": 67, "园地": 133}


def create_base_table(client):
    import_parcels(client)
    r = client.post("/api/round/create",
                    json={"version": 1, "csv": csv_text(BASE_ROWS), "totals": BASE_TOTALS})
    assert r.status_code == 200, r.text
    return r.json()


def row_of(table, pid):
    return next(r for r in table["rows"] if r["parcel_id"] == pid)


def ints_of(table, pid):
    r = row_of(table, pid)
    return {c: r["cells"][c]["int"] for c in table["land_classes"]}


# ---------------------------------------------------------------- 建表校验

def test_versions_and_preview(client):
    import_parcels(client)
    vs = client.get("/api/round/versions").json()["versions"]
    assert vs == [{"version": 1, "parcel_count": 2, "has_table": False}]
    pv = client.get("/api/round/preview/1").json()
    assert {p["parcel_id"]: p["area"] for p in pv["parcels"]} == {"P1": 100.0, "P2": 100.0}
    assert client.get("/api/round/preview/9").status_code == 422
    assert client.get("/api/round/1").status_code == 404


def test_create_ok_constraints_and_zero_fill(client):
    t = create_base_table(client)
    assert t["land_classes"] == ["耕地", "园地"]
    assert t["totals"] == BASE_TOTALS and t["grand_total"] == 200
    for row in t["rows"]:
        assert row["row_total"] == 100
        assert sum(row["cells"][c]["int"] for c in t["land_classes"]) == 100
        for c in t["land_classes"]:
            cell = row["cells"][c]
            assert cell["int"] in (int(cell["orig"]), int(cell["orig"]) + 1)
    assert ints_of(t, "P1") == {"耕地": 34, "园地": 66}
    assert ints_of(t, "P2") == {"耕地": 33, "园地": 67}
    # 缺项按零：P1 没有「林地」行时该格为 0（换一份含林地的 CSV 验证）
    # 重复建表 → 409
    r = client.post("/api/round/create",
                    json={"version": 1, "csv": csv_text(BASE_ROWS), "totals": BASE_TOTALS})
    assert r.status_code == 409


def test_create_zero_fill_column(client):
    """缺项按零：P2 只有耕地，园地格为 0。"""
    import_parcels(client)
    rows = [("P1", "耕地", 40.5), ("P1", "园地", 59.5), ("P2", "耕地", 100.0)]
    r = client.post("/api/round/create",
                    json={"version": 1, "csv": csv_text(rows),
                          "totals": {"耕地": 141, "园地": 59}})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["land_classes"] == ["耕地", "园地"]
    p2 = row_of(t, "P2")
    assert p2["cells"]["园地"]["orig"] == 0.0 and p2["cells"]["园地"]["int"] == 0
    assert ints_of(t, "P1") == {"耕地": 41, "园地": 59}


def test_create_detail_validation_errors(client):
    import_parcels(client)
    bad_cases = [
        csv_text([("P1", "耕地", 100.0)]),                                  # 缺 P2
        csv_text(BASE_ROWS + [("P9", "耕地", 1.0)]),                        # 多出 P9
        csv_text([("P1", "耕地", 60.0), ("P1", "园地", 39.0),               # 分项和≠登记
                  ("P2", "耕地", 33.0), ("P2", "园地", 67.0)]),
        csv_text(BASE_ROWS + [("P1", "耕地", 0.5)]),                        # 重复行
        csv_text([("P1", "耕地", -1.0), ("P1", "园地", 101.0),
                  ("P2", "耕地", 33.0), ("P2", "园地", 67.0)]),             # 负面积
        "宗地号,地类\nP1,耕地",                                             # 缺面积列
    ]
    for bad in bad_cases:
        r = client.post("/api/round/create",
                        json={"version": 1, "csv": bad, "totals": BASE_TOTALS})
        assert r.status_code == 422, bad
    # 全部失败，整数表未建立
    assert client.get("/api/round/1").status_code == 404


def test_create_totals_validation_errors(client):
    import_parcels(client)
    cases = [
        {"耕地": 67},                            # 缺地类
        {"耕地": 67, "园地": 133, "林地": 0},    # 多地类
        {"耕地": 67.5, "园地": 133},             # 非整数
        {"耕地": 68, "园地": 133},               # 行列总和不等（201≠200）
        {"耕地": 90, "园地": 110},               # 列合计超出可行范围
    ]
    for totals in cases:
        r = client.post("/api/round/create",
                        json={"version": 1, "csv": csv_text(BASE_ROWS), "totals": totals})
        assert r.status_code == 422, totals
    assert client.get("/api/round/1").status_code == 404


def test_check_endpoint(client):
    import_parcels(client)
    r = client.post("/api/round/check", json={"version": 1, "csv": csv_text(BASE_ROWS)})
    assert r.status_code == 200
    body = r.json()
    assert body["land_classes"] == ["耕地", "园地"]
    assert {p["parcel_id"]: p["row_total"] for p in body["parcels"]} == {"P1": 100, "P2": 100}
    assert client.post("/api/round/check",
                       json={"version": 1, "csv": "宗地号,地类,面积\nP1,耕地,1"}).status_code == 422


def test_round_half_up_row_total(client):
    """登记面积 0.5 的宗地：行合计四舍五入（半进）为 1。"""
    import_parcels(client, rings=("P3",))
    r = client.post("/api/round/create",
                    json={"version": 1, "csv": csv_text([("P3", "耕地", 0.5)]),
                          "totals": {"耕地": 1}})
    assert r.status_code == 200, r.text
    t = r.json()
    row = row_of(t, "P3")
    assert row["registered_area"] == 0.5 and row["row_total"] == 1
    assert row["cells"]["耕地"]["int"] == 1


def test_download_csv(client):
    create_base_table(client)
    r = client.get("/api/round/1/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0] == "宗地号,耕地,园地,合计"
    assert lines[1] == "P1,34,66,100"
    assert lines[2] == "P2,33,67,100"
    assert lines[3] == "合计,67,133,200"


# ---------------------------------------------------------------- 锁定与冲突

def test_lock_and_unlock(client):
    t = create_base_table(client)
    expected = ints_of(t, "P1")
    r = client.post("/api/round/1/lock",
                    json={"parcel_id": "P1", "locked": True, "expected": expected})
    assert r.status_code == 200 and row_of(r.json(), "P1")["locked"] is True
    # 解锁
    r2 = client.post("/api/round/1/lock",
                     json={"parcel_id": "P1", "locked": False, "expected": expected})
    assert r2.status_code == 200 and row_of(r2.json(), "P1")["locked"] is False


def test_lock_conflict_stale_page(client):
    """旧页面显示的数字已变 → 不能锁定（409）。"""
    t = create_base_table(client)
    stale = ints_of(t, "P1")          # 旧页面看到的 P1：耕地34 园地66
    # 他人勘误：列合计改为 耕地65/园地135，P1 被迫变为 耕地33 园地67
    rows2 = [("P1", "耕地", 33.5), ("P1", "园地", 66.5),
             ("P2", "耕地", 32.5), ("P2", "园地", 67.5)]
    r = client.post("/api/round/1/errata",
                    json={"csv": csv_text(rows2), "totals": {"耕地": 65, "园地": 135}})
    assert r.status_code == 200, r.text
    assert ints_of(r.json(), "P1") == {"耕地": 33, "园地": 67}
    # 旧页面用旧数字锁定 → 409
    r2 = client.post("/api/round/1/lock",
                     json={"parcel_id": "P1", "locked": True, "expected": stale})
    assert r2.status_code == 409
    # 用最新数字才能锁定
    fresh = ints_of(client.get("/api/round/1").json(), "P1")
    r3 = client.post("/api/round/1/lock",
                     json={"parcel_id": "P1", "locked": True, "expected": fresh})
    assert r3.status_code == 200


def test_lock_bad_requests(client):
    t = create_base_table(client)
    expected = ints_of(t, "P1")
    wrong = dict(expected, 耕地=expected["耕地"] + 1)
    assert client.post("/api/round/1/lock",
                       json={"parcel_id": "P1", "locked": True, "expected": wrong}).status_code == 409
    assert client.post("/api/round/1/lock",
                       json={"parcel_id": "P9", "locked": True, "expected": {}}).status_code == 422
    assert client.post("/api/round/1/lock",
                       json={"parcel_id": "P1", "locked": True,
                             "expected": {"耕地": 1.5, "园地": 66}}).status_code == 422
    assert client.post("/api/round/9/lock",
                       json={"parcel_id": "P1", "locked": True, "expected": expected}).status_code == 404


# ---------------------------------------------------------------- 勘误

def test_errata_locked_row_kept_minimal_changes(client):
    t = create_base_table(client)
    # 锁定 P1（耕地34 园地66）
    r = client.post("/api/round/1/lock",
                    json={"parcel_id": "P1", "locked": True, "expected": ints_of(t, "P1")})
    assert r.status_code == 200
    # 勘误：P2 明细改为 33.5/66.5，列合计改为 耕地68/园地132
    rows2 = [("P1", "耕地", 33.5), ("P1", "园地", 66.5),
             ("P2", "耕地", 33.5), ("P2", "园地", 66.5)]
    r2 = client.post("/api/round/1/errata",
                     json={"csv": csv_text(rows2), "totals": {"耕地": 68, "园地": 132}})
    assert r2.status_code == 200, r2.text
    t2 = r2.json()
    assert t2["changed_cells"] == 2                      # 仅 P2 两格被迫改动
    assert ints_of(t2, "P1") == {"耕地": 34, "园地": 66}  # 锁定行保留原数
    assert ints_of(t2, "P2") == {"耕地": 34, "园地": 66}
    assert row_of(t2, "P1")["locked"] is True            # 锁定状态保留
    # 明细已替换为新 CSV 的原始值
    assert row_of(t2, "P2")["cells"]["耕地"]["orig"] == 33.5


def test_errata_minimizes_changes_without_lock(client):
    """无锁定时也取改动最少：两种可行解中只动一格的方案胜出。"""
    t = create_base_table(client)   # P1 34/66，P2 33/67
    # 列合计 耕地66/园地134：P1 耕地 34→33 即可（1 格），或 P2 耕地 33→?（不可行，33.0 只能取 33）
    rows2 = [("P1", "耕地", 33.5), ("P1", "园地", 66.5),
             ("P2", "耕地", 33.0), ("P2", "园地", 67.0)]
    r = client.post("/api/round/1/errata",
                    json={"csv": csv_text(rows2), "totals": {"耕地": 66, "园地": 134}})
    assert r.status_code == 200, r.text
    t2 = r.json()
    assert t2["changed_cells"] == 2   # P1 耕地 34→33、园地 66→67（行合计约束连带）
    assert ints_of(t2, "P1") == {"耕地": 33, "园地": 67}
    assert ints_of(t2, "P2") == {"耕地": 33, "园地": 67}  # 未动


def test_errata_failure_keeps_original(client):
    t = create_base_table(client)
    before = client.get("/api/round/1").json()
    # 明细校验失败（分项和≠登记面积）
    bad_csv = csv_text([("P1", "耕地", 50.0), ("P1", "园地", 40.0),
                        ("P2", "耕地", 33.0), ("P2", "园地", 67.0)])
    r = client.post("/api/round/1/errata",
                    json={"csv": bad_csv, "totals": BASE_TOTALS})
    assert r.status_code == 422
    # 无解（列合计超出可行范围）
    r2 = client.post("/api/round/1/errata",
                     json={"csv": csv_text(BASE_ROWS), "totals": {"耕地": 999, "园地": -799}})
    assert r2.status_code == 422
    # 不存在的版本
    r3 = client.post("/api/round/9/errata",
                     json={"csv": csv_text(BASE_ROWS), "totals": BASE_TOTALS})
    assert r3.status_code == 404
    # 原明细与整数表保持不变
    assert client.get("/api/round/1").json() == before


def test_errata_locked_row_details_replaced_but_ints_kept(client):
    """锁定行的明细被勘误替换：原始值更新为新 CSV，整数保留原数。"""
    t = create_base_table(client)
    client.post("/api/round/1/lock",
                json={"parcel_id": "P1", "locked": True, "expected": ints_of(t, "P1")})
    # P1 的明细改为 50/50（和仍等于登记面积 100），P2 不变，合计不变
    rows2 = [("P1", "耕地", 50.0), ("P1", "园地", 50.0),
             ("P2", "耕地", 33.0), ("P2", "园地", 67.0)]
    r = client.post("/api/round/1/errata",
                    json={"csv": csv_text(rows2), "totals": BASE_TOTALS})
    assert r.status_code == 200, r.text
    t2 = r.json()
    p1 = row_of(t2, "P1")
    assert p1["cells"]["耕地"]["orig"] == 50.0     # 明细已替换
    assert p1["cells"]["园地"]["orig"] == 50.0
    assert ints_of(t2, "P1") == {"耕地": 34, "园地": 66}  # 整数保留原数
    assert t2["changed_cells"] == 0


def test_errata_new_land_class_with_lock(client):
    """勘误新增地类列：锁定行该列按 0（缺项），行合计仍成立。"""
    t = create_base_table(client)
    client.post("/api/round/1/lock",
                json={"parcel_id": "P1", "locked": True, "expected": ints_of(t, "P1")})
    rows2 = [("P1", "耕地", 33.5), ("P1", "园地", 66.5),
             ("P2", "耕地", 30.0), ("P2", "园地", 60.0), ("P2", "林地", 10.0)]
    r = client.post("/api/round/1/errata",
                    json={"csv": csv_text(rows2),
                          "totals": {"耕地": 64, "园地": 126, "林地": 10}})
    assert r.status_code == 200, r.text
    t2 = r.json()
    assert t2["land_classes"] == ["耕地", "园地", "林地"]
    assert ints_of(t2, "P1") == {"耕地": 34, "园地": 66, "林地": 0}  # 锁定行原数+新列 0
    assert ints_of(t2, "P2") == {"耕地": 30, "园地": 60, "林地": 10}
    assert t2["totals"] == {"耕地": 64, "园地": 126, "林地": 10}


def test_concurrent_errata_serialized(client):
    """两笔并发勘误：IMMEDIATE 事务串行化，都成功且表始终一致。"""
    import threading
    create_base_table(client)
    rows_a = csv_text([("P1", "耕地", 33.5), ("P1", "园地", 66.5),
                       ("P2", "耕地", 33.0), ("P2", "园地", 67.0)])
    rows_b = csv_text([("P1", "耕地", 33.5), ("P1", "园地", 66.5),
                       ("P2", "耕地", 32.5), ("P2", "园地", 67.5)])
    results = {}
    barrier = threading.Barrier(2)

    def worker(key, csv_body, totals):
        barrier.wait()
        from fastapi.testclient import TestClient as TC
        with TC(main.app) as local:
            results[key] = local.post("/api/round/1/errata",
                                      json={"csv": csv_body, "totals": totals}).status_code

    t1 = threading.Thread(target=worker, args=("a", rows_a, {"耕地": 67, "园地": 133}))
    t2 = threading.Thread(target=worker, args=("b", rows_b, {"耕地": 66, "园地": 134}))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results.values()) == [200, 200]
    # 最终表内部一致：行合计、列合计都满足
    t = client.get("/api/round/1").json()
    for row in t["rows"]:
        assert sum(row["cells"][c]["int"] for c in t["land_classes"]) == row["row_total"]
    for c in t["land_classes"]:
        assert sum(row["cells"][c]["int"] for row in t["rows"]) == t["totals"][c]
