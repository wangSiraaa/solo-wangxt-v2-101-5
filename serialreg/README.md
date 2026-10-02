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
| 保护 | `PreservationOrder` + `PreservationEvent` | 受潮/虫害保护处理单：待隔离→处理中→处理完成→返还/报废，事件带幂等键与版本 |

关键业务规则：

- **发行年月与卷期编号分开录入**：跨年卷（如 v.60 no.3 印 2023-12～2024-01）
  编号仍是一条，发行覆盖区间记在 `Issue.issue_month / issue_month_end`。
- **缺号 ≠ 缺藏**：
  - `not_published`（在刊但无发行记录）/ `ceased_gap`（停刊月之后）= **缺号**，
    只表示没有发行，不自动判定缺藏；
  - `issued+missing` = **缺藏**：已发行但没有可用实物（未入藏、全部丢失/报废，
    或全部在保护处理中——处理中的实物不是可用副本，不能抵消缺藏）。
- **合刊**：一个实物 + 两条（或更多）`IssueNumbering`。从 no.3 或 no.4 都能定位到同一实物。
- **装订**：实物条码与多期号关系不变，实际位置改指向装订册；
  **拆订**后按封存的 `previous_location` 恢复各自位置，合刊编号关系依旧完整。
- 禁止跨刊混装、重复装订；`status=bound` 只能由装订/拆订流程设置。
- **保护处理**：
  - 在馆/已装订实物可开立处理单 → `待隔离 → 处理中 → 处理完成 → 返还/报废`；
    处理期间实际位置指向**临时位置**，恢复位置开单时封存（可改）；
  - 处理中的实物**不能**被标记为可服务、不能装订、不计入可用副本；
    定位与时间轴仍显示条码、发行关系，并标明「暂时不可取」原因；
  - 册内成员进入处理**不拆订**：`BindingEntry` 与其他成员保持可追溯，
    册内有处理中成员时禁止拆订，成员返还后恢复「已装订」而非散件可服务；
  - 交接/评估/返还事件都带**幂等键 + 版本**：重复提交不重复生效，
    迟到事件只留审计（`applied=false`）不覆盖更新后的处置。

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

22 条测试覆盖：跨年卷单编号跨两年、停刊必须填月份、缺号(`ceased_gap`/`not_published`)
不等于缺藏(`issued+missing`)、合刊保留两条编号关联、任一期号可定位、条码反查得到两个期号、
装订后从 no.3/no.4/no.5 均指向装订册、禁止跨刊混装与重复装订、拆订恢复原位置且关系完好、
合刊实物在两个槽位下重复提交装订时自动去重；
以及保护处理验收：隔离后期号/条码定位显示处理状态与临时位置、处理中实物禁止标可服务/装订
且发行与缺号语义不变、册内成员处理期间册关系可追溯且局部恢复被阻止、
重复交接幂等去重、迟到返还只留审计、刷新重启后状态/恢复位置/完整历史正确保留。

手工端到端（样例数据）：

```bash
# 装订态下从合刊任一期号定位 → 装订库
curl "/api/items/locate/?title=3&volume=8&number=4"
# 拆订 → 恢复「现刊区 B-02」
curl -X POST /api/bindings/unbind/ -H "Content-Type: application/json" -d '{"binding_id":1}'
# 受潮样例：NJ-60-3 待隔离，定位显示临时位置「隔离柜 Q-1」
curl "/api/items/locate/?barcode=NJ-60-3"
# 交接送出（幂等键+版本）→ 处理中
curl -X POST /api/preservation/1/events/ -H "Content-Type: application/json" \
  -d '{"event_type":"handover","idempotency_key":"demo-h1","version":2,"location":"修复室 R-7"}'
```

## 主要 API

| 方法/路径 | 说明 |
|---|---|
| `GET/POST /api/titles/` | 刊名；停刊须带 `ceased_month` |
| `GET/POST /api/numbers/` | 卷期编号槽位 |
| `GET/POST /api/issues/` | 发行期；`kind=combined` 时 `number_ids` 至少 2 个 |
| `GET/POST /api/items/` | 入藏实物（条码+发行期+位置） |
| `GET /api/items/locate/?title=&volume=&number=` | 按期号定位实物/位置（缺号返回空匹配+状态；处理中显示临时位置与原因） |
| `GET /api/items/locate/?barcode=` | 按条码反查（含合刊覆盖的全部期号与保护处理信息） |
| `GET/POST /api/bindings/` | 装订（同刊、未装订且不在处理中的实物） |
| `POST /api/bindings/unbind/` | 拆订，恢复各自位置（册内有处理中成员时拒绝） |
| `GET/POST /api/preservation/` | 保护处理单；开单需 `idempotency_key`，重复提交返回原单 |
| `POST /api/preservation/{id}/events/` | 追加事件（`handover/assess/complete/return/discard`），带幂等键+版本 |
| `GET /api/timeline/?title=` | 时间轴：编号槽位×发行×实物×停刊标记×保护处理状态 |

## 界面

- 左侧刊种列表（含停刊月份徽标）与新增刊种；
- **时间轴**：每个卷期一个节点，区分「已入藏 / 缺藏 / 缺号 / 停刊后缺号」，
  展开显示发行年月区间、合刊徽标、实物条码与实际位置（装订后显示装订册）；
  保护处理中的实物带「待隔离/处理中/处理完成」徽标、受损原因、临时位置与恢复位置，
  槽位级提示「暂时不可取」以免误判为永久缺藏；
- 定位栏：按期号（合刊任一期号）或条码检索，处理中的实物标明处理单号与临时位置；
- 登记操作：编号槽位 → 发行期（普通/合刊，年月与编号分录）→ 入藏；
- **保护处理面板**：对在馆/已装订实物开单（受潮/虫害/霉变），记录条件评估与临时位置，
  交接送出 → 处理完成 → 返还上架/报废逐步推进；每个动作自动携带幂等键与版本，
  事件时间线完整展示（迟到事件标「仅审计」）；
- 装订面板：勾选同刊未装订且不在处理中的实物建装订册，一键拆订并显示各实物原位置；
  册内成员在处理中时显示其临时位置并阻止拆订。
