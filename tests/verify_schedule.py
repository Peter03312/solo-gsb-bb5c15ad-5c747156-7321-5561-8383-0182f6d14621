"""排班放行窗口查询的正确性与一致性验证（直连数据库，不走 HTTP）。

覆盖：
1. 基本切分：禁用优先、全组结论与各仪器依据（禁用事件/校准版本）
2. 边界精确：查询首尾、左闭右开端点、开放终点截在查询终点
3. 合并规则：仅全组结论与各仪器依据均相同才合并；依据不同（相接版本）不合并
4. 无当前校准仪器显示 NO_CALIBRATION，不被其他仪器的可放行掩盖
5. CALIBRATION_NOT_PASSED / OUTSIDE_VALID_PERIOD 原因码
6. 入参校验：空列表、重复标识、from>=to
7. 一致性：同一 SERIALIZABLE 只读事务内两次查询结果相同，
   不受并发提交的更正/禁用登记影响（不混用前后状态）
"""
import os

os.environ.setdefault("DB_POOL_MAX_SIZE", "10")
os.environ.setdefault("DB_POOL_MIN_SIZE", "2")

import sys
from datetime import datetime, timezone

from fastapi import HTTPException
from psycopg import IsolationLevel

import app.db as db
from app.models import CalibrationCreate, DisableCreate
from app.repository import (
    create_calibration,
    register_disable,
    release_windows,
    revoke_disable,
)
from app.models import DisableRevoke
from app.routers.schedule import get_release_windows
from app.transactions import run_tx

failures = []

db.open_pool()


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


def reset():
    with db.pool.connection() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.init_db()


def T(y, mo=1, d=1, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def cal(instrument, frm, to, passed=True):
    return run_tx(lambda conn: create_calibration(conn, CalibrationCreate(
        instrument=instrument, valid_from=frm, valid_to=to, passed=passed)))


def disable(event_no, instrument, frm, to, reason="故障"):
    return run_tx(lambda conn: register_disable(conn, DisableCreate(
        event_no=event_no, instrument=instrument,
        disabled_from=frm, disabled_to=to, reason=reason)))


def windows(instruments, start, end):
    return run_tx(lambda conn: release_windows(conn, instruments, start, end),
                  readonly=True)


def check_shape(name, segments, start, end):
    """通用结构断言：按时间排序、左闭右开、首尾相接、恰好覆盖查询范围。"""
    ok = bool(segments)
    ok = ok and segments[0]["from"] == start.isoformat()
    ok = ok and segments[-1]["to"] == end.isoformat()
    for prev, nxt in zip(segments, segments[1:]):
        ok = ok and prev["to"] == nxt["from"] and prev["from"] < prev["to"]
    check(f"{name}：时段有序、相接且完整覆盖查询范围", ok, str(segments))


# ---------- 1. 基本切分：禁用优先 + 各仪器依据 ----------
reset()
cal_a = cal("S-A", T(2026, 1, 1), T(2026, 7, 1))
cal_b = cal("S-B", T(2026, 1, 1), T(2027, 1, 1))
disable("E-SA-1", "S-A", T(2026, 3, 1), T(2026, 4, 1), "泵故障")

segs = windows(["S-A", "S-B"], T(2026, 2, 1), T(2026, 5, 1))
check_shape("基本切分", segs, T(2026, 2, 1), T(2026, 5, 1))
check("基本切分：禁用区间切出 3 段", len(segs) == 3, str(len(segs)))
s1, s2, s3 = segs
check("首段全组可放行，依据各自校准版本",
      s1["releasable"] is True
      and s1["from"] == T(2026, 2, 1).isoformat() and s1["to"] == T(2026, 3, 1).isoformat()
      and s1["instruments"]["S-A"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE"
      and s1["instruments"]["S-A"]["basis_calibration"]["id"] == cal_a["id"]
      and s1["instruments"]["S-B"]["basis_calibration"]["id"] == cal_b["id"], str(s1))
check("中段 S-A 命中禁用（禁用优先于校准），S-B 仍可放行，全组不可放行",
      s2["releasable"] is False
      and s2["from"] == T(2026, 3, 1).isoformat() and s2["to"] == T(2026, 4, 1).isoformat()
      and s2["instruments"]["S-A"]["reason_code"] == "INSTRUMENT_DISABLED"
      and s2["instruments"]["S-A"]["disable"]["event_no"] == "E-SA-1"
      and s2["instruments"]["S-A"]["disable"]["reason"] == "泵故障"
      and s2["instruments"]["S-A"]["basis_calibration"] is None
      and s2["instruments"]["S-B"]["releasable"] is True, str(s2))
check("末段恢复全组可放行",
      s3["releasable"] is True
      and s3["from"] == T(2026, 4, 1).isoformat() and s3["to"] == T(2026, 5, 1).isoformat(), str(s3))

# ---------- 2. 边界精确 ----------
# 查询范围恰为禁用区间 [3/1, 4/1)：左闭命中，右开截断
segs = windows(["S-A"], T(2026, 3, 1), T(2026, 4, 1))
check("查询范围恰为禁用区间：单段且命中禁用（左闭）",
      len(segs) == 1 and segs[0]["instruments"]["S-A"]["reason_code"] == "INSTRUMENT_DISABLED"
      and segs[0]["to"] == T(2026, 4, 1).isoformat(), str(segs))

# 查询起点恰为禁用终点（右开，不命中）
segs = windows(["S-A"], T(2026, 4, 1), T(2026, 5, 1))
check("查询起点=禁用终点（右开）不命中禁用",
      len(segs) == 1 and segs[0]["releasable"] is True, str(segs))

# 开放禁用区间：disabled_to=NULL，段尾截在查询终点
disable("E-SA-2", "S-A", T(2026, 6, 1), None, "持续禁用")
segs = windows(["S-A"], T(2026, 5, 1), T(2026, 9, 1))
check_shape("开放禁用", segs, T(2026, 5, 1), T(2026, 9, 1))
check("开放禁用区间截在查询终点",
      len(segs) == 2
      and segs[1]["from"] == T(2026, 6, 1).isoformat()
      and segs[1]["to"] == T(2026, 9, 1).isoformat()
      and segs[1]["instruments"]["S-A"]["disable"]["event_no"] == "E-SA-2"
      and segs[1]["instruments"]["S-A"]["disable"]["disabled_to"] is None, str(segs))

# 校准有效期末端：valid_to 之后为 OUTSIDE_VALID_PERIOD（恰等于 valid_to 不覆盖）
segs = windows(["S-A"], T(2026, 6, 30), T(2026, 7, 2))
# 注意：6/1 起开放禁用仍生效，先撤销再测校准边界
run_tx(lambda conn: revoke_disable(conn, "E-SA-2", DisableRevoke(reason="修复")))
segs = windows(["S-A"], T(2026, 6, 30), T(2026, 7, 2))
check_shape("校准期末端", segs, T(2026, 6, 30), T(2026, 7, 2))
check("valid_to 当天起不再覆盖 -> OUTSIDE_VALID_PERIOD（左闭右开）",
      len(segs) == 2
      and segs[0]["to"] == T(2026, 7, 1).isoformat()
      and segs[0]["instruments"]["S-A"]["reason_code"] == "PASSED_CALIBRATION_COVERAGE"
      and segs[1]["instruments"]["S-A"]["reason_code"] == "OUTSIDE_VALID_PERIOD"
      and segs[1]["instruments"]["S-A"]["basis_calibration"] is None, str(segs))

# ---------- 3. 合并规则 ----------
reset()
# 两条相接的通过校准（不同版本链）+ 覆盖版本边界的禁用区间
cal("S-C", T(2026, 1, 1), T(2026, 3, 15))
cal_c2 = cal("S-C", T(2026, 3, 15), T(2026, 7, 1))
disable("E-SC-1", "S-C", T(2026, 3, 1), T(2026, 4, 1), "跨版本边界的禁用")

segs = windows(["S-C"], T(2026, 2, 1), T(2026, 5, 1))
check_shape("合并", segs, T(2026, 2, 1), T(2026, 5, 1))
check("禁用掩盖校准版本边界：禁用段合并为一段",
      len(segs) == 3
      and segs[1]["from"] == T(2026, 3, 1).isoformat()
      and segs[1]["to"] == T(2026, 4, 1).isoformat()
      and segs[1]["instruments"]["S-C"]["reason_code"] == "INSTRUMENT_DISABLED", str(segs))

# 无禁用：相接的两条通过校准不合并（依据版本不同）
segs = windows(["S-C"], T(2026, 5, 1), T(2026, 7, 1))
check("查询范围落在单条校准内：单段",
      len(segs) == 1 and segs[0]["releasable"] is True, str(segs))
segs = windows(["S-C"], T(2026, 1, 1), T(2026, 3, 1))
check("版本边界前单段", len(segs) == 1, str(segs))
run_tx(lambda conn: revoke_disable(conn, "E-SC-1", DisableRevoke()))
segs = windows(["S-C"], T(2026, 2, 1), T(2026, 5, 1))
check("撤销禁用后：相接校准 v1/v2 不合并（依据不同），两段均可放行",
      len(segs) == 2
      and all(s["releasable"] for s in segs)
      and segs[0]["instruments"]["S-C"]["basis_calibration"]["id"] != segs[1]["instruments"]["S-C"]["basis_calibration"]["id"]
      and segs[1]["instruments"]["S-C"]["basis_calibration"]["id"] == cal_c2["id"], str(segs))

# ---------- 4. 缺失校准不被掩盖 ----------
reset()
cal("S-OK", T(2026, 1, 1), T(2027, 1, 1))
segs = windows(["S-OK", "S-NONE"], T(2026, 2, 1), T(2026, 3, 1))
check("无校准仪器显示 NO_CALIBRATION，全组不可放行（不被可放行仪器掩盖）",
      len(segs) == 1
      and segs[0]["releasable"] is False
      and segs[0]["instruments"]["S-NONE"]["reason_code"] == "NO_CALIBRATION"
      and segs[0]["instruments"]["S-NONE"]["releasable"] is False
      and segs[0]["instruments"]["S-OK"]["releasable"] is True, str(segs))

# ---------- 5. 未通过校准 ----------
cal("S-F", T(2026, 1, 1), T(2026, 3, 1), passed=False)
segs = windows(["S-F"], T(2026, 2, 1), T(2026, 4, 1))
check_shape("未通过校准", segs, T(2026, 2, 1), T(2026, 4, 1))
check("覆盖段 CALIBRATION_NOT_PASSED 且给出所据版本；期外 OUTSIDE_VALID_PERIOD",
      len(segs) == 2
      and segs[0]["instruments"]["S-F"]["reason_code"] == "CALIBRATION_NOT_PASSED"
      and segs[0]["instruments"]["S-F"]["basis_calibration"]["passed"] is False
      and segs[1]["instruments"]["S-F"]["reason_code"] == "OUTSIDE_VALID_PERIOD", str(segs))

# ---------- 6. 入参校验（直接调用路由函数） ----------
def route_call(**kwargs):
    try:
        return 200, get_release_windows(**kwargs)
    except HTTPException as e:
        return e.status_code, e.detail


status, detail = route_call(instrument=[], from_=T(2026, 1, 1), to=T(2026, 2, 1))
check("空仪器列表 -> 422 EMPTY_INSTRUMENT_LIST",
      status == 422 and detail["code"] == "EMPTY_INSTRUMENT_LIST", str(detail))

status, detail = route_call(instrument=["A", ""], from_=T(2026, 1, 1), to=T(2026, 2, 1))
check("空白标识 -> 422 EMPTY_INSTRUMENT_LIST",
      status == 422 and detail["code"] == "EMPTY_INSTRUMENT_LIST", str(detail))

status, detail = route_call(instrument=["A", "B", "A"], from_=T(2026, 1, 1), to=T(2026, 2, 1))
check("重复标识 -> 422 DUPLICATE_INSTRUMENT 且列出重复项",
      status == 422 and detail["code"] == "DUPLICATE_INSTRUMENT"
      and detail["duplicates"] == ["A"], str(detail))

status, detail = route_call(instrument=["A"], from_=T(2026, 2, 1), to=T(2026, 2, 1))
check("from == to -> 422 INVALID_INTERVAL",
      status == 422 and detail["code"] == "INVALID_INTERVAL", str(detail))

status, detail = route_call(instrument=["A"], from_=T(2026, 3, 1), to=T(2026, 2, 1))
check("from > to -> 422 INVALID_INTERVAL",
      status == 422 and detail["code"] == "INVALID_INTERVAL", str(detail))

# 带时区偏移的起止时间被归一化为 UTC
status, body = route_call(
    instrument=["S-OK"],
    from_=datetime(2026, 2, 1, 8, 0, tzinfo=timezone.utc),
    to=T(2026, 2, 2))
check("路由成功返回并回显归一化范围",
      status == 200 and body["from"].startswith("2026-02-01T08:00:00")
      and body["count"] == len(body["segments"]) == 1, str(status))

# ---------- 7. 一致性：同一事务快照，不混用前后状态 ----------
reset()
cal_v1 = cal("S-X", T(2026, 1, 1), T(2027, 1, 1))
conn = db.pool.getconn()
try:
    conn.read_only = True
    conn.isolation_level = IsolationLevel.SERIALIZABLE
    with conn.transaction():
        before = release_windows(conn, ["S-X"], T(2026, 2, 1), T(2026, 3, 1))
        # 另一连接在两次查询之间提交更正与禁用登记
        run_tx(lambda c: __import__("app.repository", fromlist=["correct_calibration"])
               .correct_calibration(c, __import__("app.models", fromlist=["CalibrationCorrect"])
                                    .CalibrationCorrect(
                                        corrects_id=cal_v1["id"], version=2,
                                        valid_from=T(2026, 1, 1), valid_to=T(2027, 1, 1),
                                        passed=False)))
        run_tx(lambda c: register_disable(c, DisableCreate(
            event_no="E-SX-1", instrument="S-X",
            disabled_from=T(2026, 2, 1), disabled_to=T(2026, 3, 1), reason="并发登记")))
        after = release_windows(conn, ["S-X"], T(2026, 2, 1), T(2026, 3, 1))
    check("同一 SERIALIZABLE 只读事务内两次查询结果一致（不受并发提交影响）",
          before == after and before[0]["releasable"] is True, str(after))
finally:
    conn.read_only = False
    conn.isolation_level = None
    db.pool.putconn(conn)

# 事务外新查询立即看到已提交状态（更正为未通过 + 禁用生效）
segs = windows(["S-X"], T(2026, 2, 1), T(2026, 3, 1))
check("新事务看到并发提交后的状态（禁用优先）",
      len(segs) == 1
      and segs[0]["instruments"]["S-X"]["reason_code"] == "INSTRUMENT_DISABLED", str(segs))
run_tx(lambda c: revoke_disable(c, "E-SX-1", DisableRevoke()))
segs = windows(["S-X"], T(2026, 2, 1), T(2026, 3, 1))
check("撤销禁用后看到更正结果 CALIBRATION_NOT_PASSED",
      segs[0]["instruments"]["S-X"]["reason_code"] == "CALIBRATION_NOT_PASSED", str(segs))

print()
if failures:
    print(f"{len(failures)} 项失败:", failures)
    sys.exit(1)
print("全部排班窗口验证通过")
