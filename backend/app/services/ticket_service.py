from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import skus as sku_repo
from app.repositories import tickets as ticket_repo
from app.services import overlap_service


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_tickets() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return ticket_repo.list_tickets(conn)
    finally:
        conn.close()


def get_ticket(ticket_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        t = ticket_repo.get_ticket(conn, ticket_id)
        if not t:
            raise HTTPException(404, "改价单不存在")
        t["lines"] = ticket_repo.list_lines(conn, ticket_id)
        return t
    finally:
        conn.close()


def create_ticket(body: dict[str, Any]) -> dict[str, Any]:
    title = (body.get("title") or "").strip()
    effective_at = overlap_service.normalize_effective_at(body.get("effective_at") or "")
    note = (body.get("note") or None)
    lines = body.get("lines") or []
    if not title:
        raise HTTPException(400, "标题必填")
    if not lines:
        raise HTTPException(400, "至少一行改价明细")

    conn = connect(immediate=True)
    try:
        ticket_no = ticket_repo.next_ticket_no(conn)
        tid = ticket_repo.create_ticket(
            conn,
            ticket_no=ticket_no,
            title=title,
            effective_at=effective_at,
            note=note,
            created_at=_now(),
        )
        sku_codes: list[str] = []
        for line in lines:
            sku_code = (line.get("sku_code") or "").strip()
            new_price = line.get("new_price")
            if not sku_code:
                raise HTTPException(400, "明细缺少 sku_code")
            sku = sku_repo.get_by_code(conn, sku_code)
            if not sku:
                raise HTTPException(400, f"未知商品 {sku_code}")
            book = conn.execute(
                "SELECT current_price FROM price_book WHERE sku_code=?", (sku_code,)
            ).fetchone()
            old_price = float(book["current_price"]) if book else float(sku["list_price"])
            if new_price is None:
                raise HTTPException(400, f"{sku_code} 缺少 new_price")
            ticket_repo.add_line(
                conn,
                ticket_id=tid,
                sku_code=sku_code,
                old_price=old_price,
                new_price=float(new_price),
            )
            sku_codes.append(sku_code)

        # 切断策略：与同商品未作废 scheduled 单的开放区间重算账本。
        result = overlap_service.apply_overlap_policy(
            conn, ticket_id=tid, effective_at=effective_at, sku_codes=sku_codes
        )
        conn.commit()
        out = get_ticket(tid)
        out["cut_tickets"] = result["cut_tickets"]
        return out
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reschedule_ticket(ticket_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """改约：重排生效时刻并按切断策略重算同商品账本。

    只允许 scheduled 单改约；applied/superseded 为终态。整个过程在一个
    IMMEDIATE 事务内：先撤销本单此前留下的切断标记，再按新生效时刻重算，
    任一步冲突则整体回滚，已有单不被部分改动。
    """
    effective_at = overlap_service.normalize_effective_at(body.get("effective_at") or "")
    conn = connect(immediate=True)
    try:
        ticket = ticket_repo.get_ticket(conn, ticket_id)
        if not ticket:
            raise HTTPException(404, "改价单不存在")
        if ticket["status"] != "scheduled":
            raise HTTPException(409, f"改价单状态为 {ticket['status']}，不可改约")

        # 撤销本单此前对其他单的切断，并清除本单自身行上由其他单写入的旧切点；
        # 之后按新生效时刻统一重算，避免旧 cut_at 在区间变化后残留。
        ticket_repo.reopen_ticket_lines(conn, ticket_id)
        ticket_repo.reset_own_line_cuts(conn, ticket_id)
        ticket_repo.update_effective_at(conn, ticket_id, effective_at)

        sku_codes = [line["sku_code"] for line in ticket_repo.list_lines(conn, ticket_id)]
        result = overlap_service.apply_overlap_policy(
            conn,
            ticket_id=ticket_id,
            effective_at=effective_at,
            sku_codes=sku_codes,
            exclude_ticket_id=ticket_id,
        )
        conn.commit()
        out = get_ticket(ticket_id)
        out["cut_tickets"] = result["cut_tickets"]
        return out
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
