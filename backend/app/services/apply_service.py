from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import tickets as ticket_repo
from app.repositories import prices as price_repo
from app.repositories import batches as batch_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_clock() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "business_clock": settings_repo.get(conn, "business_clock"),
            "clock_label": settings_repo.get(conn, "clock_label"),
        }
    finally:
        conn.close()


def set_clock(clock_at: str) -> dict[str, Any]:
    clock_at = (clock_at or "").strip()
    if not clock_at:
        raise HTTPException(400, "业务钟不能为空")
    conn = connect(immediate=True)
    try:
        settings_repo.set_value(conn, "business_clock", clock_at)
        conn.commit()
        return get_clock()
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_batches(conn)
    finally:
        conn.close()


def apply_due() -> dict[str, Any]:
    """Apply all scheduled tickets whose effective_at <= business clock."""
    conn = connect(immediate=True)
    try:
        clock = settings_repo.get(conn, "business_clock")
        if not clock:
            raise HTTPException(400, "业务钟未设置")
        due = ticket_repo.due_tickets(conn, clock)
        # 到期单里可能全部已过有效区间（将在下面转 superseded），真正会产生写入的
        # 单按同一套行级存活口径预判；一个都没有就不落空批次。
        if not due or ticket_repo.count_live_due_tickets(conn, clock) == 0:
            for t in due:
                ticket_repo.mark_superseded(conn, t["id"])
            conn.commit()
            return {
                "applied": False,
                "message": "当前业务钟下没有到期改价单",
                "business_clock": clock,
                "ticket_count": 0,
                "line_count": 0,
            }

        batch_no = batch_repo.next_batch_no(conn)
        created = _now()
        line_total = 0
        applied_tickets = []
        # create batch first with zeros, update after
        batch_id = batch_repo.create_batch(
            conn,
            batch_no=batch_no,
            clock_at=clock,
            ticket_count=0,
            line_count=0,
            status="running",
            created_at=created,
        )

        for t in due:
            # 行级半开区间 [effective_at, cut_at)：只写 clock 时刻仍存活的行。
            # cut_at <= clock 的前序行严禁写价签。
            # 注意：应用路径对 cut_at/cut_by_ticket_id 只读，禁止在此修改切断标记。
            lines = ticket_repo.live_lines(conn, t["id"], clock)
            if not lines:
                # 整单有效区间已完全流逝（所有行都过了 cut_at），转终态并跳过，
                # 不产生任何价签写入。
                ticket_repo.mark_superseded(conn, t["id"])
                continue
            for line in lines:
                price_repo.upsert_book(
                    conn,
                    sku_code=line["sku_code"],
                    price=float(line["new_price"]),
                    ticket_no=t["ticket_no"],
                    effective_at=t["effective_at"],
                    updated_at=created,
                )
                line_total += 1
            ticket_repo.mark_applied(conn, t["id"], applied_at=created, batch_id=batch_id)
            applied_tickets.append(t["ticket_no"])

        written_count = len(applied_tickets)
        conn.execute(
            "UPDATE apply_batches SET ticket_count=?, line_count=?, status=? WHERE id=?",
            (written_count, line_total, "done", batch_id),
        )
        conn.commit()
        return {
            "applied": True,
            "batch_no": batch_no,
            "business_clock": clock,
            "ticket_count": written_count,
            "line_count": line_total,
            "tickets": applied_tickets,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
