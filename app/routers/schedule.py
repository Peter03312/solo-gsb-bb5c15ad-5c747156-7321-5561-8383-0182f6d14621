"""排班规划查询接口。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from ..errors import blocked
from ..models import as_utc
from ..repository import release_windows
from ..transactions import run_tx

router = APIRouter(prefix="/schedule", tags=["schedule"])


@router.get("/release-windows")
def get_release_windows(
    instrument: list[str] = Query(
        ..., description="仪器标识列表（重复传参，如 ?instrument=A&instrument=B）"),
    from_: datetime = Query(..., alias="from", description="查询起点（含），带时区"),
    to: datetime = Query(..., description="查询终点（不含），带时区"),
):
    """查询一组仪器在 [from, to) 内何时全部具备报告放行条件。

    按与报告发布相同的规则（未撤销禁用区间优先，再看当前校准版本覆盖且通过）
    对查询范围切分，返回按时间排序的左闭右开时段；每段列出全组是否可放行、
    各仪器原因码及所据禁用事件或校准版本。仅当全组结论与各仪器依据均相同时
    合并相邻时段；开放终点截在查询终点。

    整次查询运行在同一 SERIALIZABLE 只读事务内，依据同一已提交快照，
    不与并发的更正/禁用登记/撤销混用前后状态。结果仅供排班规划，
    不预占未来放行资格——正式报告仍以提交时刻的判定为准。

    - 仪器列表为空或含空标识 -> 422 EMPTY_INSTRUMENT_LIST
    - 仪器标识重复 -> 422 DUPLICATE_INSTRUMENT
    - from >= to -> 422 INVALID_INTERVAL
    """
    instruments = list(instrument)
    if not instruments or any(not i.strip() for i in instruments):
        raise blocked("EMPTY_INSTRUMENT_LIST",
                      "仪器标识列表不能为空，且标识不能为空字符串")
    if len(set(instruments)) != len(instruments):
        duplicates = sorted({i for i in instruments if instruments.count(i) > 1})
        raise blocked("DUPLICATE_INSTRUMENT",
                      f"仪器标识列表存在重复: {', '.join(duplicates)}",
                      duplicates=duplicates)
    start, end = as_utc(from_), as_utc(to)
    if end <= start:
        raise blocked("INVALID_INTERVAL", "查询范围必须满足 from < to（左闭右开）")

    segments = run_tx(
        lambda conn: release_windows(conn, instruments, start, end),
        readonly=True,
    )
    return {
        "instruments": instruments,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "segments": segments,
        "count": len(segments),
    }
