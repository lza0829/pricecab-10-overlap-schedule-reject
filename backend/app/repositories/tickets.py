from __future__ import annotations

import sqlite3
from typing import Any


def next_ticket_no(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) c FROM price_tickets").fetchone()["c"] + 1
    return f"PT-{n:04d}"


def create_ticket(
    conn: sqlite3.Connection,
    *,
    ticket_no: str,
    title: str,
    effective_at: str,
    note: str | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO price_tickets(ticket_no, title, status, effective_at, note, created_at)
        VALUES (?,?,?,?,?,?)
        ''',
        (ticket_no, title, "scheduled", effective_at, note, created_at),
    )
    return int(cur.lastrowid)


def add_line(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    sku_code: str,
    old_price: float,
    new_price: float,
) -> int:
    cur = conn.execute(
        '''
        INSERT INTO price_ticket_lines(ticket_id, sku_code, old_price, new_price)
        VALUES (?,?,?,?)
        ''',
        (ticket_id, sku_code, old_price, new_price),
    )
    return int(cur.lastrowid)


def list_tickets(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT t.*,
               (SELECT COUNT(*) FROM price_ticket_lines l WHERE l.ticket_id=t.id) AS line_count,
               (SELECT COUNT(*) FROM price_ticket_lines l
                 WHERE l.ticket_id=t.id AND l.cut_at IS NOT NULL) AS cut_line_count
        FROM price_tickets t
        ORDER BY t.id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM price_tickets WHERE id=?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def list_lines(conn: sqlite3.Connection, ticket_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM price_ticket_lines WHERE ticket_id=? ORDER BY id",
        (ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 切断账本（cut ledger）
#
# 一条明细行的有效区间是半开区间 [ticket.effective_at, line.cut_at)：
#   cut_at IS NULL  -> 仍然开放，生效时刻到达后可写入价签
#   cut_at IS NOT NULL -> 截止时刻（不含）之后不得再由该行写入价签
# cut_at / cut_by_ticket_id 只允许在建单/改约事务里写入，应用路径只读不写。
# ---------------------------------------------------------------------------


def open_lines_for_sku(
    conn: sqlite3.Connection, sku_code: str, *, exclude_ticket_id: int | None = None
) -> list[dict[str, Any]]:
    """同商品所有未作废（scheduled）且未被切断的明细行，附所在单生效时刻。"""
    sql = '''
        SELECT l.*, t.ticket_no, t.effective_at AS ticket_effective_at
        FROM price_ticket_lines l
        JOIN price_tickets t ON t.id = l.ticket_id
        WHERE l.sku_code = ?
          AND t.status = 'scheduled'
          AND l.cut_at IS NULL
    '''
    params: list[Any] = [sku_code]
    if exclude_ticket_id is not None:
        sql += " AND t.id != ?"
        params.append(exclude_ticket_id)
    sql += " ORDER BY t.effective_at, t.id, l.id"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def cut_line(
    conn: sqlite3.Connection,
    line_id: int,
    *,
    cut_at: str,
    cut_by_ticket_id: int,
) -> None:
    """把一条开放明细行切断到 cut_at（不含）。幂等：已切断的行不会被改写。"""
    conn.execute(
        '''
        UPDATE price_ticket_lines
        SET cut_at = ?, cut_by_ticket_id = ?
        WHERE id = ? AND cut_at IS NULL
        ''',
        (cut_at, cut_by_ticket_id, line_id),
    )


def cut_ticket_lines_for_sku(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    sku_code: str,
    cut_at: str,
    cut_by_ticket_id: int,
) -> None:
    """切断某张单内指定商品的开放行（改约时本单被更晚的后序单切断用）。"""
    conn.execute(
        '''
        UPDATE price_ticket_lines
        SET cut_at = ?, cut_by_ticket_id = ?
        WHERE ticket_id = ? AND sku_code = ? AND cut_at IS NULL
        ''',
        (cut_at, cut_by_ticket_id, ticket_id, sku_code),
    )


def reopen_ticket_lines(conn: sqlite3.Connection, ticket_id: int) -> None:
    """改约时清除本单此前对其他单造成的切断标记，使账本按新生效时刻重算。"""
    conn.execute(
        '''
        UPDATE price_ticket_lines
        SET cut_at = NULL, cut_by_ticket_id = NULL
        WHERE cut_by_ticket_id = ?
        ''',
        (ticket_id,),
    )


def reset_own_line_cuts(conn: sqlite3.Connection, ticket_id: int) -> None:
    """改约时清除本单自身行上由其他单写入的切断标记。

    本单生效时刻一旦改变，旧的切点可能失效（例如改到后序单之后，本单应重新
    开放），统一清空后由 overlap 策略同源重算。
    """
    conn.execute(
        '''
        UPDATE price_ticket_lines
        SET cut_at = NULL, cut_by_ticket_id = NULL
        WHERE ticket_id = ?
        ''',
        (ticket_id,),
    )


def due_tickets(conn: sqlite3.Connection, clock_at: str) -> list[dict[str, Any]]:
    """所有到期 scheduled 单（含有效区间已完全流逝、将在扫描中转终态的单）。

    行级存活判定（[effective_at, cut_at) 半开区间）交给 live_lines；
    这里不按 cut_at 过滤，保证错过整个窗口的单也能被一次性标记为 superseded，
    不会以 scheduled 状态永久滞留。
    """
    rows = conn.execute(
        '''
        SELECT * FROM price_tickets
        WHERE status='scheduled' AND effective_at <= ?
        ORDER BY effective_at, id
        ''',
        (clock_at,),
    ).fetchall()
    return [dict(r) for r in rows]


def live_lines(conn: sqlite3.Connection, ticket_id: int, clock_at: str) -> list[dict[str, Any]]:
    """应用扫描用：取在 clock_at 时刻仍存活的明细行。

    存活条件（半开区间）：effective_at <= clock_at < cut_at；
    cut_at IS NULL 表示右端开放。cut_at <= clock_at 的行严禁写价签。
    """
    rows = conn.execute(
        '''
        SELECT * FROM price_ticket_lines
        WHERE ticket_id=?
          AND (cut_at IS NULL OR cut_at > ?)
        ORDER BY id
        ''',
        (ticket_id, clock_at),
    ).fetchall()
    return [dict(r) for r in rows]


def count_live_due_tickets(conn: sqlite3.Connection, clock_at: str) -> int:
    """业务钟下真正会产生写入的到期单数（与应用扫描的存活判定同源）。"""
    row = conn.execute(
        '''
        SELECT COUNT(*) c FROM price_tickets t
        WHERE t.status='scheduled'
          AND t.effective_at <= ?
          AND EXISTS (
                SELECT 1 FROM price_ticket_lines l
                WHERE l.ticket_id=t.id AND (l.cut_at IS NULL OR l.cut_at > ?)
          )
        ''',
        (clock_at, clock_at),
    ).fetchone()
    return int(row["c"])


def mark_applied(
    conn: sqlite3.Connection,
    ticket_id: int,
    *,
    applied_at: str,
    batch_id: int,
) -> None:
    conn.execute(
        '''
        UPDATE price_tickets
        SET status='applied', applied_at=?, apply_batch_id=?
        WHERE id=?
        ''',
        (applied_at, batch_id, ticket_id),
    )


def mark_superseded(conn: sqlite3.Connection, ticket_id: int) -> None:
    """终态：应用扫描确认该单在当前业务钟下已无任何存活明细行（窗口完全流逝，
    且从未应用过）。进入后不再参与到期扫描与改约。"""
    conn.execute(
        "UPDATE price_tickets SET status='superseded' WHERE id=? AND status='scheduled'",
        (ticket_id,),
    )


def update_effective_at(conn: sqlite3.Connection, ticket_id: int, effective_at: str) -> None:
    conn.execute(
        "UPDATE price_tickets SET effective_at=? WHERE id=?",
        (effective_at, ticket_id),
    )
