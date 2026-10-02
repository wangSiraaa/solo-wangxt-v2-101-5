"""保护处理单验收：受潮/虫害处置的隔离、流转、装订关系保持与幂等事件。

运行：SERIALREG_DB=sqlite pytest -q
"""
import pytest
from rest_framework.test import APIClient

from serials.models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item,
    PreservationEvent, PreservationOrder, Title,
    number_holding_status,
)


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def regular_title(db):
    """《季刊》：v.3 no.1 已入藏（CY 年报无关的普通期），no.2 缺号。"""
    t = Title.objects.create(title="保护季刊", issn="5555-6666")
    n1 = IssueNumber.objects.create(title=t, volume="3", number="1", sort_key=1)
    n2 = IssueNumber.objects.create(title=t, volume="3", number="2", sort_key=2)
    iss1 = Issue.objects.create(
        title=t, kind=Issue.IssueKind.REGULAR, issue_month="2025-03-01",
    )
    iss1.numbers.add(n1)
    item = Item.objects.create(
        barcode="PR-001", title=t, issue=iss1, location="现刊区 P-01",
    )
    return t, {"n1": n1, "n2": n2}, item, iss1


@pytest.fixture
def bound_pair(db):
    """《合订刊》：CB-1、CB-2 两件实物装订在 Q/BD-1 中。"""
    t = Title.objects.create(title="合订刊", issn="7777-8888")
    nums = []
    for i in (1, 2):
        n = IssueNumber.objects.create(
            title=t, volume="5", number=str(i), sort_key=i,
        )
        iss = Issue.objects.create(
            title=t, kind=Issue.IssueKind.REGULAR,
            issue_month=f"2025-0{i}-01",
        )
        iss.numbers.add(n)
        nums.append(n)
    it1 = Item.objects.create(
        barcode="BD-1", title=t, issue=nums[0].issues.get(), location="书库 S-1",
    )
    it2 = Item.objects.create(
        barcode="BD-2", title=t, issue=nums[1].issues.get(), location="书库 S-2",
    )
    binding = Binding.objects.create(
        call_number="Q/BD-1", title=t, location="装订库 Z-3",
    )
    for it in (it1, it2):
        BindingEntry.objects.create(
            item=it, binding=binding, previous_location=it.location,
        )
    Item.objects.filter(id__in=[it1.id, it2.id]).update(
        status=Item.ItemStatus.BOUND,
    )
    return t, binding, it1, it2


def _open_order(api, item, key, **extra):
    payload = {
        "item": item.id, "cause": "water",
        "temporary_location": "隔离柜 Q-1",
        "idempotency_key": key,
    }
    payload.update(extra)
    return api.post("/api/preservation/", payload, format="json")


def _event(api, order_id, etype, key, version, **extra):
    payload = {"event_type": etype, "idempotency_key": key, "version": version}
    payload.update(extra)
    return api.post(f"/api/preservation/{order_id}/events/", payload,
                    format="json")


# ---------- 验收 1：普通期隔离后，期号与条码定位均显示处理状态及临时位置 ----------

@pytest.mark.django_db
def test_quarantined_item_locatable_by_number_and_barcode(regular_title, api):
    t, nums, item, iss1 = regular_title
    resp = _open_order(api, item, "ord-1", condition_assessment="书脊受潮")
    assert resp.status_code == 201, resp.json()
    order = resp.json()["order"]
    assert order["status"] == "quarantine_pending"
    assert order["restore_location"] == "现刊区 P-01"  # 默认取开单时位置

    item.refresh_from_db()
    assert item.status == Item.ItemStatus.QUARANTINE_PENDING

    # 按期号定位：处理状态 + 临时位置
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=3&number=1")
    assert resp.status_code == 200
    match = resp.json()["matches"][0]
    assert match["barcode"] == "PR-001"
    assert match["status"] == "quarantine_pending"
    assert match["serviceable"] is False
    assert match["location"] == "隔离柜 Q-1"
    pres = match["preservation"]
    assert pres["cause"] == "water"
    assert pres["temporary_location"] == "隔离柜 Q-1"
    assert pres["restore_location"] == "现刊区 P-01"

    # 按条码反查：同样的处理信息，发行关系（期号）不变
    resp = api.get("/api/items/locate/?barcode=PR-001")
    match = resp.json()["matches"][0]
    assert match["status"] == "quarantine_pending"
    assert match["location"] == "隔离柜 Q-1"
    assert match["preservation"]["order_id"] == order["id"]
    assert match["numbers"] == [{"volume": "3", "number": "1"}]

    # 时间轴：实物行显示处理中徽标与临时位置；缺号槽位语义不变
    resp = api.get(f"/api/timeline/?title={t.id}")
    slots = {s["number"]: s for s in resp.json()["slots"]}
    slot_item = slots["1"]["issues"][0]["items"][0]
    assert slot_item["preservation"]["status"] == "quarantine_pending"
    assert slot_item["location"] == "隔离柜 Q-1"
    assert slots["2"]["holding_status"] == "not_published"


# ---------- 验收 2：处理中的实体不能标为可服务/装订，发行与缺号状态不受影响 ----------

@pytest.mark.django_db
def test_in_treatment_item_cannot_be_serviceable_or_bound(regular_title, api):
    t, nums, item, iss1 = regular_title
    order = _open_order(api, item, "ord-2").json()["order"]
    resp = _event(api, order["id"], "handover", "ev-h1", 2,
                  location="修复室 R-7")
    assert resp.status_code == 201
    assert resp.json()["applied"] is True
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.IN_TREATMENT

    # 直接标记为可服务（在馆）→ 拒绝
    resp = api.patch(f"/api/items/{item.id}/", {"status": "available"},
                     format="json")
    assert resp.status_code == 400
    # 报失、借出等直接状态变更同样拒绝
    assert api.patch(f"/api/items/{item.id}/", {"status": "lost"},
                     format="json").status_code == 400
    # 手工把状态改成处理中/报废 → 拒绝（必须走处理单事件）
    assert api.patch(f"/api/items/{item.id}/", {"status": "in_treatment"},
                     format="json").status_code == 400
    assert api.patch(f"/api/items/{item.id}/", {"status": "discarded"},
                     format="json").status_code == 400

    # 处理中的实物不能装订
    resp = api.post("/api/bindings/", {
        "call_number": "Q/NO", "title": t.id,
        "location": "装订库", "item_ids": [item.id],
    }, format="json")
    assert resp.status_code == 400
    assert "保护处理" in str(resp.json())

    # 状态全都没被改坏
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.IN_TREATMENT
    assert not Binding.objects.exists()

    # 发行关系不受影响：期号仍能定位到该实物，编号关联未动
    assert IssueNumbering.objects.filter(number=nums["n1"]).count() == 1
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=3&number=1")
    assert resp.json()["matches"][0]["barcode"] == "PR-001"
    # 缺号语义不变：no.2 仍是缺号而非缺藏
    assert number_holding_status(t, nums["n2"]) == "not_published"
    # 处理中的实物不是可用副本，不能抵消缺藏：no.1 暂时落入缺藏
    assert number_holding_status(t, nums["n1"]) == "issued+missing"


@pytest.mark.django_db
def test_return_restores_service_and_location(regular_title, api):
    t, nums, item, iss1 = regular_title
    order = _open_order(api, item, "ord-3").json()["order"]
    _event(api, order["id"], "handover", "ev-h", 2)
    _event(api, order["id"], "complete", "ev-c", 3)
    resp = _event(api, order["id"], "return", "ev-r", 4,
                  location="现刊区 P-09")
    assert resp.json()["applied"] is True

    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
    assert item.location == "现刊区 P-09"  # 返还到恢复位置
    # 恢复为可用副本后，缺藏随之解除
    assert number_holding_status(t, nums["n1"]) == "issued+held"
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=3&number=1")
    match = resp.json()["matches"][0]
    assert match["location"] == "现刊区 P-09"
    assert match["preservation"] is None


# ---------- 验收 3：装订册成员进入处理，册关系可追溯，局部恢复被阻止 ----------

@pytest.mark.django_db
def test_bound_member_preservation_keeps_binding_traceable(bound_pair, api):
    t, binding, it1, it2 = bound_pair
    # 册内 BD-1 发现虫害 → 开单（装订关系必须保留）
    resp = _open_order(api, it1, "ord-b1", cause="pest",
                       temporary_location="熏蒸室 F-2")
    assert resp.status_code == 201, resp.json()
    order = resp.json()["order"]
    assert order["binding_call_number"] == "Q/BD-1"  # 快照册号
    assert order["restore_location"] == "装订库 Z-3"

    it1.refresh_from_db()
    assert it1.status == Item.ItemStatus.QUARANTINE_PENDING
    # 册关系没有被静默拆除
    assert BindingEntry.objects.filter(item=it1, binding=binding).exists()
    assert binding.entries.count() == 2

    # 册视图可追溯：成员列表仍含两件，处理中的成员带标记与临时位置
    resp = api.get("/api/bindings/")
    members = {m["barcode"]: m for m in resp.json()[0]["items"]}
    assert set(members) == {"BD-1", "BD-2"}
    assert members["BD-1"]["in_preservation"] is True
    assert members["BD-1"]["current_location"] == "熏蒸室 F-2"
    assert members["BD-2"]["in_preservation"] is False

    # 另一成员不受影响：仍已装订、位于装订册
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=5&number=2")
    match = resp.json()["matches"][0]
    assert match["status"] == "bound"
    assert match["location"] == "装订库 Z-3"

    # 错误的局部恢复 1：直接把处理中成员标为可服务 → 拒绝
    assert api.patch(f"/api/items/{it1.id}/", {"status": "available"},
                     format="json").status_code == 400
    # 错误的局部恢复 2：册内有处理中成员时拆订 → 拒绝（不能静默拆订）
    resp = api.post("/api/bindings/unbind/", {"binding_id": binding.id},
                    format="json")
    assert resp.status_code == 400
    assert "保护处理" in str(resp.json())
    assert Binding.objects.filter(id=binding.id).exists()

    # 走完处理流程：交接 → 完成 → 返还
    _event(api, order["id"], "handover", "ev-bh", 2)
    _event(api, order["id"], "complete", "ev-bc", 3)
    resp = _event(api, order["id"], "return", "ev-br", 4)
    assert resp.json()["applied"] is True

    it1.refresh_from_db()
    # 返还后恢复「已装订」而非散件可服务：册与其他成员保持完整
    assert it1.status == Item.ItemStatus.BOUND
    assert it1.is_bound
    assert it1.current_location() == "装订库 Z-3"
    assert binding.entries.count() == 2
    it2.refresh_from_db()
    assert it2.status == Item.ItemStatus.BOUND
    # 全册恢复可服务
    resp = api.get(f"/api/items/locate/?title={t.id}&volume=5&number=1")
    assert resp.json()["matches"][0]["location"] == "装订库 Z-3"


# ---------- 验收 4：重复交接、迟到返还、刷新重启后状态与历史完整 ----------

@pytest.mark.django_db
def test_idempotent_duplicate_and_late_events(regular_title, api):
    t, nums, item, iss1 = regular_title
    # 开单幂等：同一幂等键重复提交（如刷新后重试）只建一张单
    resp1 = _open_order(api, item, "ord-4")
    resp2 = _open_order(api, item, "ord-4")
    assert resp1.status_code == 201
    assert resp2.status_code == 200 and resp2.json()["duplicate"] is True
    assert PreservationOrder.objects.count() == 1
    order = resp1.json()["order"]

    # 同一实物在开单闭环前不能再开第二张单
    resp = _open_order(api, item, "ord-4b")
    assert resp.status_code == 400

    # 重复交接：同一幂等键重放，不产生第二条事件、不重复推进
    r1 = _event(api, order["id"], "handover", "ev-h", 2)
    r2 = _event(api, order["id"], "handover", "ev-h", 2)
    assert r1.status_code == 201 and r1.json()["applied"] is True
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    assert PreservationEvent.objects.filter(
        order_id=order["id"], event_type="handover").count() == 1

    # 条件评估：新版本生效，迟到版本只留审计不覆盖
    _event(api, order["id"], "assess", "ev-a1", 3,
           condition_assessment="封面水渍，内页完好")
    _event(api, order["id"], "assess", "ev-a2", 4,
           condition_assessment="修订：封底亦有霉斑")
    resp = _event(api, order["id"], "assess", "ev-a1b", 3,
                  condition_assessment="封面水渍，内页完好")
    assert resp.json()["applied"] is False  # 迟到评估
    o = PreservationOrder.objects.get(id=order["id"])
    assert o.condition_assessment == "修订：封底亦有霉斑"
    assert o.version == 4

    _event(api, order["id"], "complete", "ev-c", 5)

    # 迟到返还（版本低于当前）：记入审计但不生效
    resp = _event(api, order["id"], "return", "ev-r-late", 3,
                  location="错误位置 X-0")
    assert resp.status_code == 201
    assert resp.json()["applied"] is False
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.TREATMENT_DONE  # 未被迟到事件改动
    assert item.location == "现刊区 P-01"

    # 有效返还：版本更高，正常生效
    resp = _event(api, order["id"], "return", "ev-r", 6,
                  location="现刊区 P-09")
    assert resp.json()["applied"] is True

    # 闭环后的迟到事件：同样只留审计，不能推翻已生效的返还
    resp = _event(api, order["id"], "discard", "ev-d-late", 4)
    assert resp.json()["applied"] is False

    # 模拟刷新/重启：全新客户端重新读取，状态与完整历史都在
    fresh = APIClient()
    resp = fresh.get(f"/api/preservation/{order['id']}/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "returned"
    assert body["version"] == 6
    assert body["restore_location"] == "现刊区 P-09"
    types = [(e["event_type"], e["applied"]) for e in body["events"]]
    assert types == [
        ("open", True),
        ("handover", True),
        ("assess", True),
        ("assess", True),
        ("assess", False),   # 迟到评估：审计保留
        ("complete", True),
        ("return", False),   # 迟到返还：审计保留
        ("return", True),
        ("discard", False),  # 闭环后迟到报废：审计保留，未生效
    ]
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
    assert item.location == "现刊区 P-09"

    # 闭环后同一实物可以开新单
    resp = _open_order(api, item, "ord-5", cause="pest")
    assert resp.status_code == 201


@pytest.mark.django_db
def test_discard_is_terminal_and_late_return_cannot_revive(regular_title, api):
    t, nums, item, iss1 = regular_title
    order = _open_order(api, item, "ord-6").json()["order"]
    _event(api, order["id"], "handover", "ev-h", 2)
    resp = _event(api, order["id"], "discard", "ev-d", 3, note="霉烂无法修复")
    assert resp.json()["applied"] is True
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.DISCARDED

    # 报废后迟到/错误返还：低版本只记审计，高版本被状态机拒绝
    resp = _event(api, order["id"], "return", "ev-r-late", 2)
    assert resp.json()["applied"] is False
    resp = _event(api, order["id"], "return", "ev-r-new", 4)
    assert resp.status_code == 400
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.DISCARDED

    # 报废实物不能被 PATCH 复活，也不能再开单
    assert api.patch(f"/api/items/{item.id}/", {"status": "available"},
                     format="json").status_code == 400
    assert _open_order(api, item, "ord-7").status_code == 400

    # 报废实物不再是可用副本：该期落入缺藏，但条码仍可定位并显示报废
    assert number_holding_status(t, nums["n1"]) == "issued+missing"
    resp = api.get("/api/items/locate/?barcode=PR-001")
    match = resp.json()["matches"][0]
    assert match["status"] == "discarded"
    assert match["serviceable"] is False
    assert match["preservation"]["status"] == "discarded"


@pytest.mark.django_db
def test_closed_order_rejects_new_events(regular_title, api):
    t, nums, item, iss1 = regular_title
    order = _open_order(api, item, "ord-8").json()["order"]
    _event(api, order["id"], "handover", "ev-h", 2)
    _event(api, order["id"], "return", "ev-r", 3)
    # 已返还的处理单是终态：更高版本的事件也被拒绝
    resp = _event(api, order["id"], "handover", "ev-h2", 4)
    assert resp.status_code == 400
    item.refresh_from_db()
    assert item.status == Item.ItemStatus.AVAILABLE
