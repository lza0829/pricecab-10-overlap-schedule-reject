# 06-pricecab（价签柜）

Pricecab — 改价单按预约时刻生效，现行价签可查，并与销售夹具对账。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）、Proofpack（采样放行）主链路不同。  
不是电商订单/购物车，不是仓库进出库 CRUD，不是考勤 OA。夹具全本地。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5600 |
| API | http://localhost:10600 |

## 主链（0-1）

选商品建改价单（写明生效时刻）→ 拨业务钟或点「应用到期」→ 现行价签更新 → 销售对账看一致/不一致

## 重叠预约的处理（切断账本）

同一张商品存在多张未来 scheduled 单时，全系统只实现一种互斥策略：**允许创建并切断**
（不存在「创建即拒绝」分支）。

- 每条改价明细行携带有效区间 `[ticket.effective_at, line.cut_at)`，`cut_at` 为 NULL
  表示右端开放；`cut_by_ticket_id` 记录由哪张后序单切断。
- 建单 / 改约时，对每个 sku 扫描同商品未作废（`scheduled`）且仍开放的明细行：
  前序单（含同时刻）在新单生效时刻被切断；同时刻重叠时后建单确定性胜出，先建单
  有效区间退化为空区间，任何业务钟下都不会写价签。
- 切断标记只允许在建单 / 改约事务（`overlap_service`）中写入；应用（apply）路径
  对其只读，禁止修改。
- 到期应用按业务钟 T 只写仍存活的行（`effective_at <= T < cut_at`）；窗口完全流逝
  且从未应用的单转 `superseded` 终态，截止后不可能再写入前序单新价。
- 改约（`PUT /api/tickets/{id}/reschedule`）先撤销本单此前留下的切断标记，再按新生效
  时刻同源重算；仅 `scheduled` 单可改约，`applied` / `superseded` 返回 409。

## 测试

后端端到端测试仅依赖 Python 标准库（SQLite 真实事务，fastapi 以最小 stub 注入）：

```bash
cd backend && python3 -I tests/test_overlap_cut.py
```

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。
