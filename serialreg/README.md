# 连续出版物登记应用（SerialReg）

管理连续出版物的「编号覆盖」与「实体位置」两层关系。典型场景：**一期合刊覆盖两个期号，
同时又是馆内不同的物理实体**——系统用四层模型同时表达，不允许用一个条码覆盖多个期号关系。

## 核心领域模型（PostgreSQL）

| 层 | 模型 | 含义 |
|---|---|---|
| 书目 | `Title` | 刊名、ISSN、出版状态；停刊需填**停刊月份** |
| 编号 | `IssueNumber` | 卷期编号槽位（卷+期唯一），**不含年月**，跨年卷只有一个编号 |
| 发行 | `Issue` + `IssueNumbering` | 一次出版行为。普通期关联 1 个编号，**两期合刊关联 ≥2 个独立编号记录** |
| 实体 | `Item` | 一条条码 = 一个实物，指向一个 `Issue`，多期号覆盖由关联表表达 |
| 装订 | `Binding` + `BindingEntry` | 多个实物订成一册，记录装订后位置并封存各实物原位置 |
| 保护 | `ConservationOrder` + `ConservationEvent` | 保护处理单与追加式处理事件（交接/评估/返还/报废），带版本号与幂等键 |

关键业务规则：

- **发行年月与卷期编号分开录入**：跨年卷（如 v.60 no.3 印 2023-12～2024-01）
  编号仍是一条，发行覆盖区间记在 `Issue.issue_month / issue_month_end`。
- **缺号 ≠ 缺藏**：
  - `not_published`（在刊但无发行记录）/ `ceased_gap`（停刊月之后）= **缺号**，
    只表示没有发行，不自动判定缺藏；
  - `issued+missing` = **缺藏**：已发行但没有可用实物（未入藏或全部丢失）。
- **合刊**：一个实物 + 两条（或更多）`IssueNumbering`。从 no.3 或 no.4 都能定位到同一实物。
- **装订**：实物条码与多期号关系不变，实际位置改指向装订册；
  **拆订**后按封存的 `previous_location` 恢复各自位置，合刊编号关系依旧完整。
- 禁止跨刊混装、重复装订；`status=bound` 只能由装订/拆订流程设置。
- **保护处理**（受潮/虫害送修或隔离）：
  - 实体状态流转：在馆 → **待隔离 → 处理中 → 处理完成** → 返还恢复（或**报废**，终态）；
    处理单记录每次交接、状况评估、**临时位置**与**恢复位置**；
  - 定位与时间轴显示处理状态、临时位置与「暂不可取」原因，
    发行关系、条码与缺号/缺藏语义不变；
  - 处理中的实体**不能**被标记为可服务、不能装订、不算可用副本（不抵消缺藏）；
  - 装订册内实体送修**不拆订**：册与其他成员关系保留在处理单快照中，
    处理中禁止拆订（防止静默拆订或只有部分成员恢复可用），
    返还后该成员恢复为「已装订」而非散册在馆；
  - 交接/返还事件携带**幂等键 + 处理单版本**：重复提交幂等去重，
    迟到/过期版本返回 409 不覆盖更新后的处置，处理中途刷新不丢审计。

## 技术栈

- 后端：Django 5 + Django REST Framework（`backend/`）
- 数据库：PostgreSQL 16（本地无 PG 时可用 `SERIALREG_DB=sqlite` 跑开发/测试）
- 前端：Vue 3 + Vite（`frontend/`），馆员时间轴界面

## 快速启动

### Docker Compose（推荐，含 PostgreSQL 与样例数据）

```bash
cd serialreg
docker compose up --build
# 前端 http://localhost:5173   后端 http://localhost:8000/api/
```

后端容器启动时自动 `migrate` 并执行 `seed_sample`（跨年卷 / 停刊 / 两期合刊 + 装订样例）。

### 本地分别启动

```bash
# 后端
cd backend
pip install -r requirements.txt
SERIALREG_DB=postgres PGHOST=127.0.0.1 python manage.py migrate
SERIALREG_DB=postgres python manage.py seed_sample
SERIALREG_DB=postgres python manage.py runserver

# 前端
cd frontend
npm install && npm run dev     # http://localhost:5173 ，/api 代理到 8000
```

## 验证

```bash
# PostgreSQL
SERIALREG_DB=postgres pytest -q
# 无 PG 环境（SQLite，ORM 通用）
SERIALREG_DB=sqlite pytest -q
```

23 条测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重；
以及保护处理验收：隔离后期号/条码/时间轴均显示处理状态与临时位置、
处理中实体不能标为可服务或装订且发行与缺号状态不变、
装订册成员送修后册关系可追溯且局部恢复被阻止、
重复交接幂等去重、迟到返还 409、刷新后最新状态与完整历史保留、报废终态。

手工端到端（样例数据）：

```bash
# 装订态下从合刊任一期号定位 → 装订库
curl "/api/items/locate/?title=3&volume=8&number=4"
# 拆订 → 恢复「现刊区 B-02」
curl -X POST /api/bindings/unbind/ -H "Content-Type: application/json" -d '{"binding_id":1}'
# 保护处理中的实体定位 → 临时位置 + 不可服务原因
curl "/api/items/locate/?barcode=NJ-60-3"
# 查看处理单与完整事件历史
curl "/api/conservation/1/"
# 送出交接（幂等键 + 版本）；处理完成；返还交接恢复可取
curl -X POST /api/conservation/1/events/ -H "Content-Type: application/json" \
  -d '{"kind":"handover_out","idempotency_key":"demo-1","version":2,"location":"修复中心"}'
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET/POST /api/titles/` | 刊名；停刊须带 `ceased_month` |
| `GET/POST /api/numbers/` | 卷期编号槽位 |
| `GET/POST /api/issues/` | 发行期；`kind=combined` 时 `number_ids` 至少 2 个 |
| `GET/POST /api/items/` | 入藏实物（条码+发行期+位置） |
| `GET /api/items/locate/?title=&volume=&number=` | 按期号定位实物/位置（缺号返回空匹配+状态） |
| `GET /api/items/locate/?barcode=` | 按条码反查（含合刊覆盖的全部期号） |
| `GET/POST /api/bindings/` | 装订（同刊、未装订且未在保护处理中的实物） |
| `POST /api/bindings/unbind/` | 拆订，恢复各自位置（有成员在处理中时拒绝） |
| `GET /api/timeline/?title=` | 时间轴：编号槽位×发行×实物×停刊标记（含处理状态与临时位置） |
| `GET/POST /api/conservation/` | 保护处理单列表/开立（`item`+`cause`+`temporary_location`+`idempotency_key`；同键重放返回原单） |
| `GET /api/conservation/{id}/` | 处理单详情：状态、版本、临时/恢复位置、完整事件历史 |
| `POST /api/conservation/{id}/events/` | 追加事件（`handover_out`/`assessment`/`complete`/`handover_in`/`discard`），须带 `idempotency_key`+`version`；重复键幂等去重，版本过期 409 |

## 界面

- 左侧刊种列表（含停刊月份徽标）与新增刊种；
- **时间轴**：每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  展开显示发行年月区间、合刊徽标、实物条码与实际位置（装订后显示装订册）；
  保护处理中的实物显示「暂不可取」、处理单号、临时位置与恢复位置；
- 定位栏：按期号（合刊任一期号）或条码检索，处理中实体标明不可服务原因；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- **保护处理面板**：开立处理单（原因/临时位置/恢复位置），进行中处理单的
  送出交接、状况评估、处理完成、返还交接、报废操作与完整事件历史；
  每个动作自带幂等键，网络失败可直接重试，版本冲突自动刷新到最新状态；
- 装订面板：勾选同刊未装订实物建装订册，一键拆订并显示各实物原位置；
  处理中的成员带徽标提示，且此时拆订按钮禁用。
