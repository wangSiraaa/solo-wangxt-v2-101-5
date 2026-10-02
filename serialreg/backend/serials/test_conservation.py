"""保护处理单验收：隔离/送修/返还状态流转、定位展示、装订册关系、幂等与版本。

对应验收：
1) 普通期隔离后按期号和条码均显示处理状态及临时位置；
2) 处理中的实体尝试标为可服务或装订被拒绝，原发行和缺号状态不受影响；
3) 装订册内一件实体进入处理后，册关系和其他成员保持可追溯，错误的局部恢复被阻止；
4) 重复交接、迟到返还和刷新重启后，最新有效状态、恢复位置及完整处理历史均正确保留。

运行：SERIALREG_DB=sqlite pytest -q
"""
import pytest
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, ConservationEvent, ConservationOrder,
    Issue, IssueNumber, IssueNumbering, Item, Title,
    number_holding_status,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def regular_title(db):
    """《季刊报》：no.1/no.2 各入藏一册，no.3 已登记但未发行（缺号）。"""
    t = Title.objects.create(title="季刊报")
    n1 = IssueNumber.objects.create(title=t, volume="3", number="1", sort_key=1)
    n2 = IssueNumber.objects.create(title=t, volume="3", number="2", sort_key=2)
    n3 = IssueNumber.objects.create(title=t, volume="3", number="3", sort_key=3)
    i1 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2025-01-01",
    )
    i1.numbers.add(n1)
    i2 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2025-04-01",
    )
    i2.numbers.add(n2)
    it1 = Item.objects.create(
        barcode="QK-01", title=t, issue=i1, location="现刊区 C-01",
    )
    it2 = Item.objects.create(
        barcode="QK-02", title=t, issue=i2, location="现刊区 C-01",
    )
    return t, {"n1": n1, "n2": n2, "n3": n3}, {"it1": it1, "it2": it2}


def open_order(api, item, key, **kw):
    payload = {
        "item": item.id, "cause": "damp",
        "temporary_location": "隔离室 Q-1",
        "idempotency_key": key, "operator": "馆员甲",
    }
    payload.update(kw)
    return api.post("/api/conservation/", payload, format="json")


def post_event(api, order_id, kind, key, version, **kw):
    payload = {"kind": kind, "idempotency_key": key, "version": version}
    payload.update(kw)
    return api.post(f"/api/conservation/{order_id}/events/", payload, format="json")


# ---------- 验收 1：隔离后按期号/条码/时间轴显示处理状态与临时位置 ----------

@pytest.mark.django_db
def test_quarantine_visible_in_locate_and_timeline(api, regular_title):
    t, nums, items = regular_title
    it1 = items["it1"]
    resp = open_order(api, it1, "k1-open")
    assert resp.status_code == 201, resp.json()
    order = resp.json()
    assert order["status"] == "quarantine"
    # 恢复位置默认取原馆藏位置
    assert order["restore_location"] == "现刊区 C-01"

    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.QUARANTINE

    # 按期号定位：显示处理状态、临时位置与不可服务原因
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=3&number=1")
    assert resp.status_code == 200
    body = resp.json()
    # 处理中实体不算可用副本 → 槽位层面为缺藏（语义不变，仍区别于缺号）
    assert body["holding_status"] == "issued+missing"
    m = body["matches"][0]
    assert m["barcode"] == "QK-01"
    assert m["status"] == "quarantine"
    assert m["serviceable"] is False
    assert "保护" in m["unavailable_reason"]
    assert m["location"] == "隔离室 Q-1"  # 实际位置 = 临时位置
    assert m["conservation"]["temporary_location"] == "隔离室 Q-1"
    assert m["conservation"]["restore_location"] == "现刊区 C-01"
    assert m["conservation"]["cause"] == "damp"

    # 按条码反查：同样显示处理状态与临时位置
    resp = api.get("/api/items/locate/?barcode=QK-01")
    m = resp.json()["matches"][0]
    assert m["status"] == "quarantine"
    assert m["location"] == "隔离室 Q-1"
    assert m["conservation"]["order_id"] == order["id"]
    assert m["serviceable"] is False

    # 缺号语义不受影响：no.3 依旧是没有发行记录的缺号
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=3&number=3")
    assert resp.json()["holding_status"] == "not_published"

    # 时间轴同样展示处理状态与临时位置
    resp = api.get(f"/api/timeline/?title={t.id}")
    slots = {s["number"]: s for s in resp.json()["slots"]}
    tl_item = slots["1"]["issues"][0]["items"][0]
    assert tl_item["status"] == "quarantine"
    assert tl_item["location"] == "隔离室 Q-1"
    assert tl_item["serviceable"] is False
    assert tl_item["conservation"]["restore_location"] == "现刊区 C-01"
    assert slots["1"]["holding_status"] == "issued+missing"
    assert slots["3"]["holding_status"] == "not_published"


# ---------- 验收 2：处理中实体不能标为可服务/装订，发行与缺号状态不变 ----------

@pytest.mark.django_db
def test_treatment_item_cannot_be_serviceable_or_bound(api, regular_title):
    t, nums, items = regular_title
    it1, it2 = items["it1"], items["it2"]
    oid = open_order(api, it1, "k2-open").json()["id"]
    resp = post_event(api, oid, "handover_out", "k2-ho", 1,
                      location="修复中心", condition_note="书脊受潮")
    assert resp.status_code == 201, resp.json()
    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.IN_TREATMENT
    # 交接更新了临时位置
    assert it1.current_location() == "修复中心"

    # ① 不能标记为可服务
    resp = api.patch(f"/api/items/{it1.id}/", {"status": "available"},
                     format="json")
    assert resp.status_code == 400
    assert "保护处理" in str(resp.json())
    # 也不能直接报失或借出绕过流程
    for st in ("lost", "checked_out"):
        resp = api.patch(f"/api/items/{it1.id}/", {"status": st}, format="json")
        assert resp.status_code == 400, st
    # 保护处理状态本身也不能直接 PATCH 进入
    resp = api.patch(f"/api/items/{it1.id}/", {"status": "in_treatment"},
                     format="json")
    assert resp.status_code == 400

    # ② 不能作为成员装订
    resp = api.post("/api/bindings/", {
        "call_number": "Q/X-1", "title": t.id, "location": "装订库",
        "item_ids": [it1.id, it2.id],
    }, format="json")
    assert resp.status_code == 400
    assert "保护处理" in str(resp.json())
    assert Binding.objects.count() == 0

    # ③ 不作为可用副本抵消缺藏
    assert number_holding_status(t, nums["n1"]) == "issued+missing"

    # 原发行关系、条码、缺号/缺藏语义不受影响
    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.IN_TREATMENT
    assert it1.barcode == "QK-01"
    assert it1.issue.numbers.count() == 1
    assert IssueNumbering.objects.filter(number=nums["n1"]).count() == 1
    assert number_holding_status(t, nums["n2"]) == "issued+held"
    assert number_holding_status(t, nums["n3"]) == "not_published"


# ---------- 验收 3：装订册成员进入处理，册关系可追溯，局部恢复被阻止 ----------

@pytest.mark.django_db
def test_binding_member_treatment_keeps_relations(api, regular_title):
    t, nums, items = regular_title
    it1, it2 = items["it1"], items["it2"]
    resp = api.post("/api/bindings/", {
        "call_number": "Q/QK-2025", "title": t.id, "location": "装订库 D-3",
        "item_ids": [it1.id, it2.id],
    }, format="json")
    assert resp.status_code == 201, resp.json()
    binding_id = resp.json()["id"]

    # 册内一件进入保护处理
    resp = open_order(api, it1, "k3-open")
    assert resp.status_code == 201, resp.json()
    order = resp.json()
    # 处理单记录所属装订册（可追溯），默认恢复位置为册位置
    assert order["binding_call_number"] == "Q/QK-2025"
    assert order["restore_location"] == "装订库 D-3"

    # 册关系未被静默解除，其他成员保持原状
    assert BindingEntry.objects.filter(item=it1, binding_id=binding_id).exists()
    assert Binding.objects.filter(id=binding_id).exists()
    it2.refresh_from_db()
    assert it2.status == Item.ItemStatus.BOUND
    assert it2.current_location() == "装订库 D-3"

    # 装订册接口仍列出两个成员，处理中的成员带处理信息与临时位置
    resp = api.get("/api/bindings/")
    members = {m["barcode"]: m for m in resp.json()[0]["items"]}
    assert set(members) == {"QK-01", "QK-02"}
    assert members["QK-01"]["conservation"]["status"] == "quarantine"
    assert members["QK-01"]["current_location"] == "隔离室 Q-1"
    assert members["QK-02"]["conservation"] is None
    assert members["QK-02"]["current_location"] == "装订库 D-3"

    # 定位该成员：位置指向临时位置，但册关系仍在
    resp = api.get("/api/items/locate/?barcode=QK-01")
    m = resp.json()["matches"][0]
    assert m["bound"] is True
    assert m["binding"] == "Q/QK-2025"
    assert m["location"] == "隔离室 Q-1"
    assert m["conservation"]["binding"] == "Q/QK-2025"

    # 错误的局部恢复被阻止①：处理中不能拆订
    # （否则会把处理中的成员静默拆出册、并让册内只剩部分成员可用）
    resp = api.post("/api/bindings/unbind/", {"binding_id": binding_id},
                    format="json")
    assert resp.status_code == 400
    assert "保护处理" in str(resp.json())
    assert BindingEntry.objects.filter(item=it1).exists()
    # 错误的局部恢复被阻止②：不能直接 PATCH 成在馆
    resp = api.patch(f"/api/items/{it1.id}/", {"status": "available"},
                     format="json")
    assert resp.status_code == 400

    # 正常流程返还：恢复为「已装订」而非散册在馆，册关系完好
    oid = order["id"]
    assert post_event(api, oid, "handover_out", "k3-ho", 1).status_code == 201
    assert post_event(api, oid, "complete", "k3-done", 2).status_code == 201
    resp = post_event(api, oid, "handover_in", "k3-hi", 3)
    assert resp.status_code == 201, resp.json()
    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.BOUND  # 不是 available：册内成员不局部恢复
    assert it1.current_location() == "装订库 D-3"
    assert BindingEntry.objects.filter(item=it1, binding_id=binding_id).exists()
    it2.refresh_from_db()
    assert it2.status == Item.ItemStatus.BOUND
    # 两册都可用后，期号恢复「已入藏」
    assert number_holding_status(t, nums["n1"]) == "issued+held"


# ---------- 验收 4：重复交接、迟到返还、刷新后状态与历史完整 ----------

@pytest.mark.django_db
def test_idempotent_events_late_return_and_refresh(api, regular_title):
    t, nums, items = regular_title
    it1 = items["it1"]

    # 重复开立：同一幂等键 → 同一处理单，不重复建单
    r1 = open_order(api, it1, "k4-open")
    r2 = open_order(api, it1, "k4-open")
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["id"] == r1.json()["id"]
    assert ConservationOrder.objects.count() == 1
    oid = r1.json()["id"]

    # 重复交接：同一幂等键 → 幂等返回，不重复入账
    e1 = post_event(api, oid, "handover_out", "k4-ho", 1, location="修复中心")
    e2 = post_event(api, oid, "handover_out", "k4-ho", 1, location="修复中心")
    assert e1.status_code == 201
    assert e2.status_code == 200
    assert e2.json()["idempotent_replay"] is True
    order = ConservationOrder.objects.get(id=oid)
    assert order.version == 2
    assert order.events.count() == 2  # open + handover_out

    # 迟到返还：基于旧版本 → 409，不覆盖更新后的处置
    late = post_event(api, oid, "handover_in", "k4-hi-late", 1)
    assert late.status_code == 409
    assert late.json()["current"]["version"] == 2
    order.refresh_from_db()
    assert order.status == ConservationOrder.Status.IN_TREATMENT
    assert order.version == 2
    assert order.events.count() == 2
    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.IN_TREATMENT

    # 中途刷新（重新拉取处理单）后按最新版本推进
    fresh = api.get(f"/api/conservation/{oid}/").json()
    assert fresh["version"] == 2
    assert fresh["status"] == "in_treatment"
    assert fresh["temporary_location"] == "修复中心"
    assert post_event(api, oid, "complete", "k4-done",
                      fresh["version"]).status_code == 201
    resp = post_event(api, oid, "handover_in", "k4-hi", 3)
    assert resp.status_code == 201
    assert resp.json()["status"] == "closed"

    # 重复返还：幂等去重，不重复入账
    resp = post_event(api, oid, "handover_in", "k4-hi", 3)
    assert resp.status_code == 200
    assert resp.json()["idempotent_replay"] is True
    assert ConservationEvent.objects.filter(order_id=oid).count() == 4

    # 重启后（重新从库中读取）最新有效状态与恢复位置正确
    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.AVAILABLE
    assert it1.location == "现刊区 C-01"
    order.refresh_from_db()
    assert order.status == ConservationOrder.Status.CLOSED
    assert order.version == 4
    assert order.restore_location == "现刊区 C-01"
    assert order.closed_at is not None

    # 完整处理历史保留：顺序、版本、交接位置都在
    events = list(order.events.all())
    assert [e.kind for e in events] == [
        "open", "handover_out", "complete", "handover_in",
    ]
    assert [e.version for e in events] == [1, 2, 3, 4]
    assert events[1].location == "修复中心"

    # 定位恢复可服务，不再显示处理信息
    resp = api.get("/api/items/locate/?barcode=QK-01")
    m = resp.json()["matches"][0]
    assert m["serviceable"] is True
    assert m["conservation"] is None
    assert m["location"] == "现刊区 C-01"
    assert number_holding_status(t, nums["n1"]) == "issued+held"


# ---------- 补充：报废终态、重复开单、不可送修状态 ----------

@pytest.mark.django_db
def test_discard_flow_is_terminal(api, regular_title):
    t, nums, items = regular_title
    it1 = items["it1"]
    oid = open_order(api, it1, "k5-open").json()["id"]
    assert post_event(api, oid, "handover_out", "k5-ho", 1).status_code == 201
    resp = post_event(api, oid, "discard", "k5-discard", 2,
                      condition_note="虫蛀严重，无法修复")
    assert resp.status_code == 201
    assert resp.json()["status"] == "discarded"

    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.DISCARDED
    # 报废为终态：任何直接状态变更都被拒
    resp = api.patch(f"/api/items/{it1.id}/", {"status": "available"},
                     format="json")
    assert resp.status_code == 400
    # 已办结处理单不能再推进
    resp = post_event(api, oid, "handover_in", "k5-late", 3)
    assert resp.status_code == 409
    # 报废实体不算可用副本；定位显示报废原因
    assert number_holding_status(t, nums["n1"]) == "issued+missing"
    m = api.get("/api/items/locate/?barcode=QK-01").json()["matches"][0]
    assert m["status"] == "discarded"
    assert m["serviceable"] is False
    assert m["unavailable_reason"] == "已报废"
    # 报废后可以重新开立处理单已无意义：非在馆/已装订状态被拒
    resp = open_order(api, it1, "k5-again")
    assert resp.status_code == 409


@pytest.mark.django_db
def test_second_active_order_rejected(api, regular_title):
    t, nums, items = regular_title
    it1 = items["it1"]
    assert open_order(api, it1, "k6-a").status_code == 201
    # 不同幂等键的再次开立：同一实体只能有一张进行中的处理单
    resp = open_order(api, it1, "k6-b")
    assert resp.status_code == 409
    assert ConservationOrder.objects.count() == 1
    # 同一实体也不能重复送修（状态已不是 available/bound）
    assert Item.objects.get(id=it1.id).status == Item.ItemStatus.QUARANTINE


@pytest.mark.django_db
def test_cannot_open_for_lost_or_checked_out_item(api, regular_title):
    t, nums, items = regular_title
    it2 = items["it2"]
    it2.status = Item.ItemStatus.LOST
    it2.save(update_fields=["status"])
    resp = open_order(api, it2, "k7-lost")
    assert resp.status_code == 409
    it2.status = Item.ItemStatus.CHECKED_OUT
    it2.save(update_fields=["status"])
    resp = open_order(api, it2, "k7-out")
    assert resp.status_code == 409
    assert ConservationOrder.objects.count() == 0


@pytest.mark.django_db
def test_assessment_records_condition_and_moves_location(api, regular_title):
    """状况评估：不改变状态，但留下审计，可随交接更新临时位置。"""
    t, nums, items = regular_title
    it1 = items["it1"]
    oid = open_order(api, it1, "k8-open").json()["id"]
    resp = post_event(api, oid, "assessment", "k8-as1", 1,
                      condition_note="受潮面积 30%，需干燥处理",
                      location="隔离室 Q-2", operator="馆员乙")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "quarantine"  # 评估不改变状态
    assert body["version"] == 2
    assert body["temporary_location"] == "隔离室 Q-2"
    it1.refresh_from_db()
    assert it1.current_location() == "隔离室 Q-2"
    ev = body["events"][-1]
    assert ev["kind"] == "assessment"
    assert ev["condition_note"] == "受潮面积 30%，需干燥处理"
    assert ev["operator"] == "馆员乙"
