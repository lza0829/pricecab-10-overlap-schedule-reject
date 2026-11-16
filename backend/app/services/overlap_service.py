"""同商品 scheduled 单的重叠检测与切断账本（全系统唯一的切断策略）。

策略（互斥二选一，本系统只实现这一种）：**允许创建并切断**。

- 建单/改约时，对新单涉及的每个 sku，扫描同商品未作废（status='scheduled'）
  且仍开放的明细行（有效区间 [effective_at, cut_at)，cut_at IS NULL 即右端开放）。
- 重叠意图判定（开放区间无截止，两单必然在某一时刻同时有效）：
  * 前序单 effective_at <= 新单 effective_at -> 重叠，前序行在新单生效时刻被切断；
  * 同时刻（effective_at 相同）-> 重叠且无先后可分，后建单切断先建单，
    先建单有效区间退化为空区间 [t,t)，任何业务钟下都不会再写价签；
  * 后序单 effective_at > 新单 effective_at -> 改约场景下同样重叠，
    新单被该后序单在其生效时刻切断（建单时不存在更晚的新单，但保留对称处理）。
- 切断标记 cut_at / cut_by_ticket_id 只在本模块写入；应用（apply）路径只读不写。
- 切断当下不改工单状态：行级区间是唯一账本，工单是否还能应用由应用扫描在具体
  业务钟下判定（clock >= cut_at 的行禁止写价签），窗口完全流逝后才转 superseded。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from app.repositories import tickets as ticket_repo


def normalize_effective_at(raw: str) -> str:
    """校验并归一化生效时刻为 UTC 的 ISO 字符串（Z 结尾），字典序与时间序一致。"""
    text = (raw or "").strip()
    if not text:
        raise HTTPException(400, "生效时刻必填")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"生效时刻格式非法: {text}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def apply_overlap_policy(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    effective_at: str,
    sku_codes: list[str],
    exclude_ticket_id: int | None = None,
) -> dict[str, list[str]]:
    """在同一事务内对新单/改约单执行切断策略。

    返回每个 sku 被切断的前序行所在单号，供服务层生成提示。
    语义冲突（同商品在本单内重复出现）在此以 409 拒绝——一行新价无法在同一时刻
    持有两个互相矛盾的切断意图。
    """
    if exclude_ticket_id is None:
        exclude_ticket_id = ticket_id

    seen: set[str] = set()
    for code in sku_codes:
        if code in seen:
            raise HTTPException(409, f"同一改价单内商品 {code} 重复，存在自重叠冲突")
        seen.add(code)

    cut_ticket_nos: set[str] = set()
    for sku_code in sku_codes:
        open_lines = ticket_repo.open_lines_for_sku(
            conn, sku_code, exclude_ticket_id=exclude_ticket_id
        )
        for line in open_lines:
            other_at = line["ticket_effective_at"]
            if other_at <= effective_at:
                # 前序（含同时刻）：在新单生效时刻切断它。
                ticket_repo.cut_line(
                    conn,
                    line["id"],
                    cut_at=effective_at,
                    cut_by_ticket_id=ticket_id,
                )
                cut_ticket_nos.add(line["ticket_no"])
            else:
                # 后序（仅改约可能遇到）：新单的开放区间被后序单在其生效时刻切断。
                ticket_repo.cut_ticket_lines_for_sku(
                    conn,
                    ticket_id=ticket_id,
                    sku_code=sku_code,
                    cut_at=other_at,
                    cut_by_ticket_id=line["ticket_id"],
                )

    # 不在此处改工单状态：终态（superseded）只能由应用扫描在确认整单有效区间
    # 已完全流逝后写入，保证前序单在 cut_at 之前的合法窗口内仍可正常生效。
    return {"cut_tickets": sorted(cut_ticket_nos)}
