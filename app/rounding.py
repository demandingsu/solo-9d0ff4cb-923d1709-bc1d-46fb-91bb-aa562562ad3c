"""分项面积整数化：CSV 解析、校验与「上下取整 + 行列合计」求解。

业务规则：
- 每格只能对原值向上或向下取整（floor / ceil）；
- 行合计 = 该版登记面积四舍五入（半进）后的整数；
- 列合计 = 资料员录入的各地类整数合计；
- 缺项按零：矩阵中未出现的（宗地 × 地类）格子原值视为 0；
- 勘误时锁定行保留原数，其余格子以「改动数最少」为目标。

求解模型：扣除 floor 基线后，每个小数格是一个 0/1 变量，
行/列剩余量构成二分图 b-匹配；以「与旧整数不同」为费用，
用最小费用最大流（SSP + 势函数）一次求出可行且改动最少的解。
"""
from __future__ import annotations

import csv
import io
import math
from heapq import heappop, heappush


class RoundValidationError(ValueError):
    """分项面积明细（CSV）或地类合计不合法。"""


class RoundInfeasibleError(ValueError):
    """取整无解：行合计与列合计无法同时满足。"""


PARCEL_HEADERS = {"宗地号", "宗地编号", "parcel_id"}
CLASS_HEADERS = {"地类", "地类名称", "land_class"}
AREA_HEADERS = {"面积", "面积(平方米)", "面积（平方米）", "area"}

EPS = 1e-9        # 判断小数部分是否为 0
SUM_EPS = 1e-6    # 分项和与登记面积比对的浮点容差


def round_half_up(x: float) -> int:
    """四舍五入（半进）。Python 内置 round 是银行家舍入，不能直接用。"""
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------- CSV 解析

def parse_area_csv(text: str):
    """解析分项面积 CSV。

    需含表头：宗地号 / 地类 / 面积（支持常见别名，容忍 UTF-8 BOM）。
    返回 (cells, parcels, land_classes)：
    cells 为 {(宗地号, 地类): 面积}，parcels 与 land_classes 按出现顺序去重。
    """
    if not isinstance(text, str) or not text.strip():
        raise RoundValidationError("CSV 内容为空")
    text = text.lstrip("\ufeff")
    try:
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    except csv.Error as e:  # pragma: no cover - csv 模块极少抛错
        raise RoundValidationError(f"CSV 解析失败：{e}") from e
    if not rows:
        raise RoundValidationError("CSV 内容为空")

    header = [h.strip() for h in rows[0]]

    def find_col(aliases: set[str], label: str) -> int:
        for i, h in enumerate(header):
            if h in aliases:
                return i
        raise RoundValidationError(
            f"CSV 表头缺少「{label}」列（实际表头：{'、'.join(header) or '空'}）"
        )

    ci_pid = find_col(PARCEL_HEADERS, "宗地号")
    ci_cls = find_col(CLASS_HEADERS, "地类")
    ci_area = find_col(AREA_HEADERS, "面积")
    need = max(ci_pid, ci_cls, ci_area)

    cells: dict[tuple[str, str], float] = {}
    parcels: list[str] = []
    classes: list[str] = []
    seen_pid: set[str] = set()
    seen_cls: set[str] = set()
    for ln, row in enumerate(rows[1:], start=2):
        if len(row) <= need:
            raise RoundValidationError(f"第 {ln} 行列数不足（至少需要 {need + 1} 列）")
        pid = row[ci_pid].strip()
        cls = row[ci_cls].strip()
        raw = row[ci_area].strip()
        if not pid:
            raise RoundValidationError(f"第 {ln} 行宗地号为空")
        if not cls:
            raise RoundValidationError(f"第 {ln} 行地类为空")
        try:
            area = float(raw)
        except ValueError:
            raise RoundValidationError(f"第 {ln} 行面积不是数字：{raw!r}") from None
        if not math.isfinite(area) or area < 0:
            raise RoundValidationError(f"第 {ln} 行面积必须是非负数字：{raw!r}")
        key = (pid, cls)
        if key in cells:
            raise RoundValidationError(f"第 {ln} 行与前面重复：宗地 {pid} / 地类 {cls}")
        cells[key] = area
        if pid not in seen_pid:
            seen_pid.add(pid)
            parcels.append(pid)
        if cls not in seen_cls:
            seen_cls.add(cls)
            classes.append(cls)
    return cells, parcels, classes


def validate_details(cells: dict[tuple[str, str], float], snapshot: list[dict]) -> None:
    """校验 CSV 明细与某版登记快照一致：宗地集合相同，且每宗分项和 = 登记面积。"""
    snap_ids = [p["parcel_id"] for p in snapshot]
    snap_set = set(snap_ids)
    sums: dict[str, float] = {}
    for (pid, _), v in cells.items():
        sums[pid] = sums.get(pid, 0.0) + v

    extra = sorted(pid for pid in sums if pid not in snap_set)
    if extra:
        raise RoundValidationError("CSV 中该版不存在的宗地号：" + "、".join(extra))
    missing = [pid for pid in snap_ids if pid not in sums]
    if missing:
        raise RoundValidationError("CSV 缺少该版宗地：" + "、".join(missing))

    bad = [
        f"{p['parcel_id']}（分项和 {sums[p['parcel_id']]:g} ≠ 登记面积 {p['area']:g}）"
        for p in snapshot
        if abs(sums[p["parcel_id"]] - p["area"]) > SUM_EPS
    ]
    if bad:
        raise RoundValidationError(
            "每宗原始分项之和须等于该版登记面积：" + "；".join(bad)
        )


def validate_totals(totals, land_classes: list[str]) -> dict[str, int]:
    """校验录入的各地类整数合计：地类集合须与 CSV 完全一致，值为非负整数。"""
    if not isinstance(totals, dict):
        raise RoundValidationError("totals 必须是 {地类: 整数合计} 对象")
    norm: dict[str, int] = {}
    for k, v in totals.items():
        name = str(k).strip()
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not float(v).is_integer():
            raise RoundValidationError(f"地类「{name}」的合计必须是整数")
        v = int(v)
        if v < 0:
            raise RoundValidationError(f"地类「{name}」的合计不能为负")
        norm[name] = v
    want, got = set(land_classes), set(norm)
    if got != want:
        parts = []
        if want - got:
            parts.append("缺少：" + "、".join(sorted(want - got)))
        if got - want:
            parts.append("多余：" + "、".join(sorted(got - want)))
        raise RoundValidationError("合计的地类须与 CSV 地类完全一致（" + "；".join(parts) + "）")
    return norm


# ---------------------------------------------------------------- 求解

def _min_cost_flow(n_nodes: int, edges: list[tuple], s: int, t: int, limit: int):
    """最小费用最大流（SSP + Johnson 势函数）。

    edges: (u, v, cap, cost)，初始费用须非负。
    返回 (流量, forward)，forward[i] 是 edges[i] 正向边的可变引用，
    实际流量 = 原容量 - forward[i][2]。实现确定性：堆按 (距离, 点号) 弹出。
    """
    g: list[list[list]] = [[] for _ in range(n_nodes)]
    forward: list[list] = []
    for u, v, cap, cost in edges:
        fwd = [v, len(g[v]), cap, cost]
        g[u].append(fwd)
        g[v].append([u, len(g[u]) - 1, 0, -cost])
        forward.append(fwd)
    inf = float("inf")
    potential = [0] * n_nodes
    flow = 0
    while flow < limit:
        dist = [inf] * n_nodes
        dist[s] = 0
        prev_v = [-1] * n_nodes
        prev_e = [-1] * n_nodes
        pq = [(0, s)]
        while pq:
            d, v = heappop(pq)
            if d > dist[v]:
                continue
            for i, e in enumerate(g[v]):
                if e[2] <= 0:
                    continue
                nd = d + e[3] + potential[v] - potential[e[0]]
                if nd < dist[e[0]]:
                    dist[e[0]] = nd
                    prev_v[e[0]] = v
                    prev_e[e[0]] = i
                    heappush(pq, (nd, e[0]))
        if dist[t] == inf:
            break
        for v in range(n_nodes):
            if dist[v] < inf:
                potential[v] += dist[v]
        d = limit - flow
        v = t
        while v != s:
            d = min(d, g[prev_v[v]][prev_e[v]][2])
            v = prev_v[v]
        flow += d
        v = t
        while v != s:
            e = g[prev_v[v]][prev_e[v]]
            e[2] -= d
            g[v][e[1]][2] += d
            v = prev_v[v]
    return flow, forward


def solve_integer_table(
    cells: dict[tuple[str, str], float],
    parcels: list[str],
    land_classes: list[str],
    row_targets: dict[str, int],
    col_targets: dict[str, int],
    prev: dict[tuple[str, str], int] | None = None,
    fixed_rows: set[str] | None = None,
) -> dict[tuple[str, str], int]:
    """求整数表。返回覆盖全部 宗地 × 地类 的 {(宗地号, 地类): 整数}。

    prev：上一版整数（勘误时用于「改动最少」目标），缺省按 0；
    fixed_rows：锁定行宗地号，这些行必须沿用 prev 原数。
    无解时抛 RoundInfeasibleError。
    """
    prev = prev or {}
    fixed_rows = set(fixed_rows or ())

    result: dict[tuple[str, str], int] = {}

    # 锁定行：整行沿用旧整数；旧行合计必须仍等于行目标（登记面积不变，恒成立）
    for pid in parcels:
        if pid not in fixed_rows:
            continue
        total = 0
        for cls in land_classes:
            v = int(prev.get((pid, cls), 0))
            result[(pid, cls)] = v
            total += v
        if total != row_targets[pid]:
            raise RoundInfeasibleError(
                f"锁定行 {pid} 的原数合计 {total} 与行合计 {row_targets[pid]} 不一致"
            )

    # 自由行：floor 基线 + 小数格 0/1 调整量
    base: dict[tuple[str, str], int] = {}
    frac_cells: list[tuple[str, str]] = []
    row_need: dict[str, int] = {}
    free_parcels: list[str] = []
    for pid in parcels:
        if pid in fixed_rows:
            continue
        free_parcels.append(pid)
        need = row_targets[pid]
        for cls in land_classes:
            v = cells.get((pid, cls), 0.0)
            f = math.floor(v)
            base[(pid, cls)] = f
            need -= f
            if v - f > EPS:
                frac_cells.append((pid, cls))
        row_need[pid] = need

    col_need: dict[str, int] = {}
    for cls in land_classes:
        n = col_targets[cls]
        for pid in parcels:
            if pid in fixed_rows:
                n -= result[(pid, cls)]
            else:
                n -= base[(pid, cls)]
        col_need[cls] = n

    # 单行列快速可行性检查（给出更具体的报错；整体可行性由最大流最终判定）
    for pid in free_parcels:
        cap = sum(1 for p, _ in frac_cells if p == pid)
        if not 0 <= row_need[pid] <= cap:
            raise RoundInfeasibleError(
                f"宗地 {pid} 的行合计 {row_targets[pid]} 无法由分项上下取整凑出"
            )
    for cls in land_classes:
        cap = sum(1 for p, c in frac_cells if c == cls)
        if not 0 <= col_need[cls] <= cap:
            lo = col_targets[cls] - col_need[cls]
            raise RoundInfeasibleError(
                f"地类「{cls}」的合计 {col_targets[cls]} 超出可行范围 "
                f"[{lo}, {lo + cap}]，请核对录入的合计"
            )

    total_need = sum(row_need.values())
    if total_need != sum(col_need.values()):
        raise RoundInfeasibleError(
            f"各地类合计之和（{sum(col_need.values())}）与"
            f"各行合计之和（{sum(row_targets.values())}）不一致"
        )

    # 最小费用流：源→行（剩余量）→列（每小数格容量 1）→汇
    n_r, n_c = len(free_parcels), len(land_classes)
    src, sink = 0, 1 + n_r + n_c
    row_node = {pid: 1 + i for i, pid in enumerate(free_parcels)}
    col_node = {cls: 1 + n_r + j for j, cls in enumerate(land_classes)}
    edges: list[tuple] = []
    for pid in free_parcels:
        if row_need[pid] > 0:
            edges.append((src, row_node[pid], row_need[pid], 0))
    for cls in land_classes:
        if col_need[cls] > 0:
            edges.append((col_node[cls], sink, col_need[cls], 0))
    # 行→列：选 ceil 的费用 = 与旧值是否不同（0/1），整体 +1 平移为非负；
    # 每单位流量恰过一条行→列边，平移量恒定，不影响最优性。
    frac_edge_idx: dict[tuple[str, str], int] = {}
    for pid, cls in frac_cells:
        f = base[(pid, cls)]
        pv = int(prev.get((pid, cls), 0))
        cost_ceil = 0 if f + 1 == pv else 1
        cost_floor = 0 if f == pv else 1
        frac_edge_idx[(pid, cls)] = len(edges)
        edges.append((row_node[pid], col_node[cls], 1, cost_ceil - cost_floor + 1))

    flow, forward = _min_cost_flow(2 + n_r + n_c, edges, src, sink, total_need)
    if flow != total_need:
        raise RoundInfeasibleError(
            "取整无解：各行合计与各地类合计无法同时满足，请核对录入的地类合计"
        )

    # 汇总：自由行 = 基线 + 流量调整；整数格与小数未调整格即基线
    for pid in free_parcels:
        for cls in land_classes:
            result[(pid, cls)] = base[(pid, cls)]
    for (pid, cls), ei in frac_edge_idx.items():
        if forward[ei][2] == 0:  # 容量 1 用尽 → 该格向上取整
            result[(pid, cls)] = base[(pid, cls)] + 1
    return result


# ---------------------------------------------------------------- 导出

def build_table_csv(table: dict) -> str:
    """把整数表渲染为矩阵 CSV：首行表头，末行合计，缺项为 0。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    classes = table["land_classes"]
    w.writerow(["宗地号"] + list(classes) + ["合计"])
    for row in table["rows"]:
        w.writerow(
            [row["parcel_id"]]
            + [row["cells"][c]["int"] for c in classes]
            + [row["row_total"]]
        )
    w.writerow(["合计"] + [table["totals"][c] for c in classes] + [table["grand_total"]])
    return buf.getvalue()
