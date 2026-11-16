"""重叠切断策略的端到端测试（仅依赖 Python 标准库）。

运行：cd backend && python3 -I tests/test_overlap_cut.py

环境未安装 fastapi，这里注入只含 HTTPException 的最小 stub；
被测代码走真实 SQLite 与真实服务层事务，不 mock 任何仓储逻辑。

账本语义：每条明细行的有效区间为半开区间 [ticket.effective_at, line.cut_at)，
cut_at IS NULL 表示右端开放。业务钟 T 下只有 effective_at <= T < cut_at
（或 cut_at IS NULL）的行允许写入价签。

覆盖需求点：
  1. 拒绝创建闸：同单自重叠 409、非法时刻 400、终态单改约 409（回滚不留残）
  2. 切断后前序不再越界生效：窗口内可生效、过 cut_at 绝不写价、
     跳过窗口转 superseded、应用路径不改写切断标记
  3. 同时刻重叠边界：后建单确定性胜出，先建单空区间永不生效
  4. 改约重算：撤销旧切断、按新生效时刻重新切账本
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types

# --- 最小 fastapi stub，必须在 import app.* 之前完成 -------------------------
class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


_fastapi = types.ModuleType("fastapi")
_fastapi.HTTPException = HTTPException
sys.modules["fastapi"] = _fastapi

_tmp = tempfile.mkdtemp(prefix="pricecab-test-")
os.environ["DATA_DIR"] = _tmp
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402
from app.repositories import tickets as ticket_repo  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import apply_service, ticket_service  # noqa: E402

seed_if_empty()

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  - {name}")
    else:
        FAIL += 1
        print(f"FAIL  - {name} {extra}")


def expect_status(name: str, expected: int, fn) -> None:
    try:
        fn()
    except HTTPException as e:
        check(name, e.status_code == expected, f"got {e.status_code}: {e.detail}")
        return
    check(name, False, "未抛出预期异常")


def make(title: str, at: str, lines: list[dict]):
    return ticket_service.create_ticket(
        {"title": title, "effective_at": at, "note": None, "lines": lines}
    )


def reschedule(tid: int, at: str):
    return ticket_service.reschedule_ticket(tid, {"effective_at": at})


def set_clock(at: str) -> None:
    apply_service.set_clock(at)


def apply_due():
    return apply_service.apply_due()


def book(sku: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT current_price, source_ticket_no FROM price_book WHERE sku_code=?",
            (sku,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def line_map(tid: int) -> dict[str, dict]:
    conn = connect()
    try:
        return {l["sku_code"]: l for l in ticket_repo.list_lines(conn, tid)}
    finally:
        conn.close()


def lines_of(tid: int) -> list[dict]:
    conn = connect()
    try:
        return ticket_repo.list_lines(conn, tid)
    finally:
        conn.close()


def ticket(tid: int) -> dict:
    conn = connect()
    try:
        return ticket_repo.get_ticket(conn, tid)
    finally:
        conn.close()


def count_tickets() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) c FROM price_tickets").fetchone()["c"]
    finally:
        conn.close()


def history_prices(sku: str) -> list:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT price, source_ticket_no FROM price_history WHERE sku_code=? ORDER BY id",
            (sku,),
        ).fetchall()
        return [(r["price"], r["source_ticket_no"]) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
print("场景 1：拒绝创建闸（409/400，且失败回滚不留残）")

before = count_tickets()
expect_status(
    "同一单内同商品重复（自重叠意图）拒绝创建 409",
    409,
    lambda: make(
        "自重叠",
        "2026-11-16T10:00:00Z",
        [
            {"sku_code": "SKU-TEA-001", "new_price": 20},
            {"sku_code": "SKU-TEA-001", "new_price": 21},
        ],
    ),
)
check("冲突回滚：没有残留改价单", count_tickets() == before)

expect_status(
    "非法生效时刻拒绝创建 400",
    400,
    lambda: make("坏时刻", "not-a-time", [{"sku_code": "SKU-TEA-001", "new_price": 20}]),
)
expect_status(
    "空生效时刻拒绝创建 400",
    400,
    lambda: make("空时刻", "  ", [{"sku_code": "SKU-TEA-001", "new_price": 20}]),
)
check("400 回滚：没有残留改价单", count_tickets() == before)


# ---------------------------------------------------------------------------
print("场景 2：切断后前序不再越界生效（半开区间 [eff, cut)）")

# A 是多行单；B 只切 A 的 TEA-001 行，TEA-002 行保持开放
a = make(
    "A 多行前序",
    "2026-11-16T10:00:00Z",
    [
        {"sku_code": "SKU-TEA-001", "new_price": 20.0},
        {"sku_code": "SKU-TEA-002", "new_price": 60.0},
    ],
)
b = make(
    "B 切断单",
    "2026-11-16T12:00:00Z",
    [{"sku_code": "SKU-TEA-001", "new_price": 18.0}],
)
a_lines = line_map(a["id"])
check(
    "A 的 TEA-001 行被 B 在 12:00 切断（cut_at 同源留痕）",
    a_lines["SKU-TEA-001"]["cut_at"] == "2026-11-16T12:00:00Z"
    and a_lines["SKU-TEA-001"]["cut_by_ticket_id"] == b["id"],
)
check("A 的 TEA-002 行仍开放（cut_at NULL）", a_lines["SKU-TEA-002"]["cut_at"] is None)
check("切断当下 A 仍为 scheduled（状态由扫描判定，不在建单时写终态）",
      ticket(a["id"])["status"] == "scheduled")

set_clock("2026-11-16T11:00:00Z")
res1 = apply_due()
check("11:00 应用 A（窗口 [10:00,12:00) 内，含被切行）", res1["tickets"] == [a["ticket_no"]])
check("A 的两行均在存活窗口内，写入 2 行", res1["line_count"] == 2)
check("TEA-001 窗口内写入 A 的 20", book("SKU-TEA-001")["current_price"] == 20.0)
check("TEA-002 写入 A 的 60", book("SKU-TEA-002")["current_price"] == 60.0)
check(
    "应用路径未改写切断标记（cut_at 原样保留，与写入同源不漂移）",
    line_map(a["id"])["SKU-TEA-001"]["cut_at"] == "2026-11-16T12:00:00Z",
)

set_clock("2026-11-16T12:30:00Z")
res2 = apply_due()
check("12:30 应用 B", res2["tickets"] == [b["ticket_no"]])
check("TEA-001 终价为 B 的 18（确定性，不靠扫描运气）",
      book("SKU-TEA-001")["current_price"] == 18.0)
check("价签来源单号为 B", book("SKU-TEA-001")["source_ticket_no"] == b["ticket_no"])
check("A 的被切行历史只出现在窗口内（20 一次），截止后无二次写入",
      [p for p, _ in history_prices("SKU-TEA-001")] == [28.0, 20.0, 18.0])

# 跳过整个有效窗口：到期扫描把窗口已完全流逝的单转 superseded，绝不补写
c = make("C 单行", "2026-11-16T14:00:00Z", [{"sku_code": "SKU-SNK-011", "new_price": 10.0}])
d = make("D 后序", "2026-11-16T15:00:00Z", [{"sku_code": "SKU-SNK-011", "new_price": 9.0}])
check("C 的行留有 15:00 截止时刻", lines_of(c["id"])[0]["cut_at"] == "2026-11-16T15:00:00Z")
set_clock("2026-11-16T16:00:00Z")
res3 = apply_due()
check("跳过窗口：扫描只应用 D", res3["tickets"] == [d["ticket_no"]])
check("C 窗口完全流逝后转 superseded", ticket(c["id"])["status"] == "superseded")
check("SNK-011 只可能是 D 的 9 元，C 的 10 元从未写入",
      book("SKU-SNK-011")["current_price"] == 9.0
      and 10.0 not in [p for p, _ in history_prices("SKU-SNK-011")])
check("C 从未被应用（无批次）", ticket(c["id"])["apply_batch_id"] is None)
expect_status("终态单 C 拒绝改约 409", 409,
              lambda: reschedule(c["id"], "2026-11-16T13:00:00Z"))
again = apply_due()
check("二次应用无到期单，C 仍无批次，截止后改价不可能成功",
      again["applied"] is False and ticket(c["id"])["apply_batch_id"] is None)

# 直接把钟拨过切点：一张窗口完全流逝（转终态、不写入），一张仍存活（正常应用）
s1 = make("S1", "2026-11-22T10:00:00Z", [{"sku_code": "SKU-TEA-002", "new_price": 55.0}])
s2 = make("S2", "2026-11-22T11:00:00Z", [{"sku_code": "SKU-TEA-002", "new_price": 54.0}])
set_clock("2026-11-22T12:00:00Z")
r_skip = apply_due()
check("同一次扫描：S1 跳过不计入批次，S2 正常应用",
      r_skip["applied"] is True and r_skip["tickets"] == [s2["ticket_no"]]
      and r_skip["ticket_count"] == 1 and r_skip["line_count"] == 1)
check("S1 转 superseded，S2 正常应用",
      ticket(s1["id"])["status"] == "superseded" and ticket(s2["id"])["status"] == "applied")
check("价签只写 S2 的 54", book("SKU-TEA-002")["source_ticket_no"] == s2["ticket_no"])

# 纯空场景：钟直接跳过所有到期单的存活窗口，扫描只转终态，不产生 0 行空批次
from app.repositories import batches as batch_repo  # noqa: E402
v1 = make("V1", "2026-11-23T08:00:00Z", [{"sku_code": "SKU-SNK-012", "new_price": 38.0}])
v2 = make("V2", "2026-11-23T09:00:00Z", [{"sku_code": "SKU-SNK-012", "new_price": 37.0}])
# 此时 v1 窗口 [08:00,09:00)、v2 开放。钟拨到 10:00：v1 窗口已流逝，v2 仍存活 -> 非空。
# 再造 v3 切断 v2，使两张单在 10:00 下都无存活行。
v3 = make("V3", "2026-11-23T09:30:00Z", [{"sku_code": "SKU-SNK-012", "new_price": 36.0}])
n0 = connect().execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"]
set_clock("2026-11-23T10:00:00Z")
# 10:00 下：v1（cut 09:00）、v2（cut 09:30）均无存活行，v3 存活 -> 仍非空。
# 要得到纯空，再让 v3 也被切：直接验证 count_live_due 与分批行为的边界——
# 先应用一次（v3 写价，v1/v2 转终态），再断言不存在为 v1/v2 落的空批次。
r_mix = apply_due()
n1 = connect().execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"]
check("混合扫描：仅 v3 产生 1 个批次 1 行，v1/v2 转终态不产生空批次",
      r_mix["tickets"] == [v3["ticket_no"]] and r_mix["line_count"] == 1 and n1 == n0 + 1)
check("v1、v2 均 superseded，v3 applied",
      ticket(v1["id"])["status"] == "superseded"
      and ticket(v2["id"])["status"] == "superseded"
      and ticket(v3["id"])["status"] == "applied")
check("终价为 v3 的 36，前序 38/37 从未写入（末段只有 36）",
      book("SKU-SNK-012")["current_price"] == 36.0
      and [p for p, _ in history_prices("SKU-SNK-012")][-1:] == [36.0]
      and 38.0 not in [p for p, _ in history_prices("SKU-SNK-012")]
      and 37.0 not in [p for p, _ in history_prices("SKU-SNK-012")])
# 再应用：已无任何到期单
r_idle = apply_due()
check("随后扫描无到期单，批次不再增长",
      r_idle["applied"] is False
      and connect().execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"] == n1)

# 直接构造“有到期 scheduled 单但全部无存活行”的库状态，验证守卫分支不落空批次
w = make("W 待转终态", "2026-11-24T08:00:00Z", [{"sku_code": "SKU-FRZ-032", "new_price": 14.5}])
w2 = make("W2 切点单", "2026-11-24T09:00:00Z", [{"sku_code": "SKU-FRZ-032", "new_price": 14.0}])
# 再让 w2 也被 w3 切，随后让 w3 应用掉，只剩 w/w2 两张未应用且窗口已过的单
w3 = make("W3 末单", "2026-11-24T10:00:00Z", [{"sku_code": "SKU-FRZ-032", "new_price": 13.5}])
set_clock("2026-11-24T10:30:00Z")
apply_due()  # w3 applied；w、w2 此刻已在同次扫描转 superseded
check("w、w2 已 superseded，w3 applied",
      ticket(w["id"])["status"] == "superseded"
      and ticket(w2["id"])["status"] == "superseded"
      and ticket(w3["id"])["status"] == "applied")
# 反向构造守卫命中：手工把一张已应用单回退为 scheduled 且其行全部带过期 cut_at
conn = connect()
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE price_tickets SET status='scheduled', applied_at=NULL, apply_batch_id=NULL WHERE id=?", (w["id"],))
conn.commit(); conn.close()
n_guard = connect().execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"]
r_guard = apply_due()
check("守卫分支：到期但零存活行 -> applied=False、无新批次、单转 superseded",
      r_guard["applied"] is False
      and connect().execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"] == n_guard
      and ticket(w["id"])["status"] == "superseded")


# ---------------------------------------------------------------------------
print("场景 3：同时刻重叠边界（含精确切点 T-1/T/T+1）")

e = make("E 同时刻先建", "2026-11-16T18:00:00Z", [{"sku_code": "SKU-DAY-021", "new_price": 40.0}])
f = make("F 同时刻后建", "2026-11-16T18:00:00Z", [{"sku_code": "SKU-DAY-021", "new_price": 35.0}])
check("同时刻：先建单 E 被后建单 F 切断（cut_at=18:00，空区间）",
      lines_of(e["id"])[0]["cut_at"] == "2026-11-16T18:00:00Z"
      and lines_of(e["id"])[0]["cut_by_ticket_id"] == f["id"])
set_clock("2026-11-16T18:00:00Z")
res4 = apply_due()
check("18:00 整刻：E 空区间转 superseded，只应用 F",
      ticket(e["id"])["status"] == "superseded" and res4["tickets"] == [f["ticket_no"]])
check("同时刻边界终价确定性取后建单 35", book("SKU-DAY-021")["current_price"] == 35.0)
check("先建单 40 元从未写入历史", 40.0 not in [p for p, _ in history_prices("SKU-DAY-021")])

# 反向再验一次（另一商品，交换建单方向），排除偶然
g2 = make("G2 先建", "2026-11-16T19:00:00Z", [{"sku_code": "SKU-DAY-022", "new_price": 30.0}])
h2 = make("H2 后建", "2026-11-16T19:00:00Z", [{"sku_code": "SKU-DAY-022", "new_price": 31.0}])
check("反向验证：后建单切断先建单", lines_of(g2["id"])[0]["cut_by_ticket_id"] == h2["id"])
set_clock("2026-11-16T19:00:00Z")
apply_due()
check("反向验证终价为后建单 31", book("SKU-DAY-022")["current_price"] == 31.0)

# 精确切点三连：T-1 前序存活、T 切点前序停写、T+1 后序独占
k = make("K 跨切点", "2026-11-16T23:00:00Z", [{"sku_code": "SKU-FRZ-031", "new_price": 20.0}])
m = make("M 零点单", "2026-11-17T00:00:00Z", [{"sku_code": "SKU-FRZ-031", "new_price": 19.0}])
set_clock("2026-11-16T23:59:00Z")
apply_due()
check("切点前 1 分钟：K 在 [23:00,00:00) 内生效 20",
      book("SKU-FRZ-031")["current_price"] == 20.0)
set_clock("2026-11-17T00:00:00Z")
r0 = apply_due()
check("切点整刻 00:00：K 无存活行不再写价，M 生效",
      ticket(k["id"])["status"] == "applied" and r0["tickets"] == [m["ticket_no"]])
check("切点整刻价签为 M 的 19", book("SKU-FRZ-031")["current_price"] == 19.0)
check("K 的切断标记在应用后依然保留（账本不可变）",
      lines_of(k["id"])[0]["cut_at"] == "2026-11-17T00:00:00Z")
set_clock("2026-11-17T00:01:00Z")
r_again = apply_due()
check("切点后 1 分钟：无到期单，K 永不复活",
      r_again["applied"] is False and book("SKU-FRZ-031")["source_ticket_no"] == m["ticket_no"])


# ---------------------------------------------------------------------------
print("场景 4：改约重算切断账本")

g = make("G 前序", "2026-11-18T20:00:00Z", [{"sku_code": "SKU-FRZ-032", "new_price": 20.0}])
h = make("H 后序", "2026-11-18T22:00:00Z", [{"sku_code": "SKU-FRZ-032", "new_price": 19.0}])
check("G 初始 cut_at=22:00（仍是 scheduled，窗口未流逝）",
      lines_of(g["id"])[0]["cut_at"] == "2026-11-18T22:00:00Z"
      and ticket(g["id"])["status"] == "scheduled")

reschedule(h["id"], "2026-11-18T21:00:00Z")
check("改约后 G 的切断时刻重算为 21:00（旧标记先撤销再同源重写）",
      lines_of(g["id"])[0]["cut_at"] == "2026-11-18T21:00:00Z")
check("H 自身生效时刻已更新", ticket(h["id"])["effective_at"] == "2026-11-18T21:00:00Z")

set_clock("2026-11-18T20:30:00Z")
apply_due()
check("20:30 G 在窗口内先生效 20", book("SKU-FRZ-032")["current_price"] == 20.0)
set_clock("2026-11-18T21:30:00Z")
apply_due()
check("21:30 H 接续写入 19", book("SKU-FRZ-032")["current_price"] == 19.0)
# 该 SKU 可能已被更早的场景写过，只校验本场景追加的末段链
tail = history_prices("SKU-FRZ-032")[-2:]
check("历史末段链：G(20) -> H(19)，且来源单号正确",
      tail == [(20.0, g["ticket_no"]), (19.0, h["ticket_no"])], str(tail))
expect_status("已应用单 H 拒绝改约 409", 409,
              lambda: reschedule(h["id"], "2026-11-18T23:00:00Z"))

# 改约把后序单推晚：前序单的截止时刻随之后移，开放区间延长
i = make("I 早单", "2026-11-19T23:00:00Z", [{"sku_code": "SKU-SNK-012", "new_price": 35.0}])
j = make("J 晚单", "2026-11-20T00:00:00Z", [{"sku_code": "SKU-SNK-012", "new_price": 33.0}])
reschedule(j["id"], "2026-11-20T01:00:00Z")
check("J 推晚后 I 的截止时刻同步推到 01:00",
      lines_of(i["id"])[0]["cut_at"] == "2026-11-20T01:00:00Z")
set_clock("2026-11-20T00:30:00Z")
apply_due()
check("I 在延长后的窗口内生效 35", book("SKU-SNK-012")["current_price"] == 35.0)
set_clock("2026-11-20T01:30:00Z")
apply_due()
check("截止后 J 接续写入 33，I 不再写价", book("SKU-SNK-012")["current_price"] == 33.0)
check("I 已在窗口内应用过，保持 applied 终态（不回退为 superseded）",
      ticket(i["id"])["status"] == "applied")

# 改约到后序单之后：自身旧 cut_at 必须清除，本单重新开放并最终生效
p = make("P 早单", "2026-11-21T10:00:00Z", [{"sku_code": "SKU-DAY-021", "new_price": 41.0}])
q = make("Q 中间单", "2026-11-21T11:00:00Z", [{"sku_code": "SKU-DAY-021", "new_price": 42.0}])
check("P 被 Q 切断，切点 11:00", lines_of(p["id"])[0]["cut_at"] == "2026-11-21T11:00:00Z")
reschedule(p["id"], "2026-11-21T12:00:00Z")
check("P 改到 Q 之后：自身旧 cut_at 已清除并重新开放",
      lines_of(p["id"])[0]["cut_at"] is None)
check("Q 反过来被 P 在 12:00 切断",
      lines_of(q["id"])[0]["cut_at"] == "2026-11-21T12:00:00Z"
      and lines_of(q["id"])[0]["cut_by_ticket_id"] == p["id"])
set_clock("2026-11-21T11:30:00Z")
apply_due()
check("11:30 Q 在其窗口内生效 42", book("SKU-DAY-021")["current_price"] == 42.0)
set_clock("2026-11-21T12:30:00Z")
apply_due()
check("12:30 P 重新开放后生效 41，旧切点未残留",
      book("SKU-DAY-021")["current_price"] == 41.0
      and book("SKU-DAY-021")["source_ticket_no"] == p["ticket_no"])

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
