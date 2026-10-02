"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

保护处理（受潮/虫害送修或隔离）：
  ConservationOrder   保护处理单：待隔离 → 处理中 → 处理完成 → 已恢复 / 已报废，
                      带版本号与幂等键；一个实体同一时间最多一张进行中的处理单
  ConservationEvent   处理事件（交接/状况评估/返还/报废），追加式审计，
                      幂等键去重、版本号防止迟到事件覆盖更新后的处置

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有可用 Item（未入藏、丢失或全部在保护处理中）。
"""
from django.db import models
from django.db.models import Prefetch, Q
from django.core.exceptions import ValidationError
from django.utils import timezone

# 处理单「进行中」的状态：实体在这些状态下不可服务、不可装订、不可直接改状态
CONSERVATION_ACTIVE_STATUSES = ("quarantine", "in_treatment", "completed")


class ConservationError(Exception):
    """保护处理流程的领域错误基类。"""


class InvalidTransitionError(ConservationError):
    """当前状态不允许该操作。"""


class StaleVersionError(ConservationError):
    """事件基于过期的处理单版本（迟到/重复提交），拒绝覆盖更新后的处置。"""


class Title(models.Model):
    """连续出版物刊名。"""

    class PublicationStatus(models.TextChoices):
        ACTIVE = "active", "在刊"
        CEASED = "ceased", "停刊"

    title = models.CharField("刊名", max_length=255)
    issn = models.CharField("ISSN", max_length=9, blank=True)
    publisher = models.CharField("出版者", max_length=255, blank=True)
    status = models.CharField(
        "出版状态", max_length=10,
        choices=PublicationStatus.choices, default=PublicationStatus.ACTIVE,
    )
    # 停刊月份：与卷期编号分开记录，只表示出版停止，不改变任何馆藏状态
    ceased_month = models.DateField("停刊月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.status == self.PublicationStatus.CEASED and not self.ceased_month:
            raise ValidationError({"ceased_month": "停刊刊名必须填写停刊月份。"})


class IssueNumber(models.Model):
    """卷期编号（编号槽位），与发行年月解耦。

    跨年卷：同一卷可以跨自然年，例如 v.60 no.3 印的是 2023-12、2024-01，
    编号仍只有一条 (volume=60, number=3)，发行时间记录在 Issue 上。
    """

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="numbers",
    )
    volume = models.CharField("卷", max_length=20, blank=True)
    number = models.CharField("期", max_length=20)
    sort_key = models.PositiveIntegerField(
        "排序键", default=0,
        help_text="馆员录入的编号顺序，跨年卷按编号顺序而非月份排列",
    )

    class Meta:
        verbose_name = "期号"
        unique_together = ("title", "volume", "number")
        ordering = ["sort_key", "id"]

    def __str__(self):
        return f"{self.volume}({self.number})" if self.volume else self.number


class Issue(models.Model):
    """一次发行。普通期关联一个 IssueNumber；两期合刊关联两个（或更多）。"""

    class IssueKind(models.TextChoices):
        REGULAR = "regular", "普通期"
        COMBINED = "combined", "合刊"

    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="issues",
    )
    kind = models.CharField(
        "类型", max_length=10,
        choices=IssueKind.choices, default=IssueKind.REGULAR,
    )
    # 发行年月与卷期编号分开录入
    issue_month = models.DateField("发行年月", help_text="只取年月；合刊可只填起始月")
    issue_month_end = models.DateField(
        "发行截止年月", null=True, blank=True, help_text="合刊/跨年卷的覆盖结束月",
    )
    numbers = models.ManyToManyField(
        IssueNumber, through="IssueNumbering", related_name="issues",
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "发行期"
        ordering = ["issue_month", "id"]

    def __str__(self):
        nums = "·".join(str(n) for n in self.numbers.all())
        return f"{self.title.title} {nums}"

    def clean(self):
        if self.issue_month_end and self.issue_month_end < self.issue_month:
            raise ValidationError({"issue_month_end": "截止年月不能早于起始年月。"})


class IssueNumbering(models.Model):
    """Issue ↔ IssueNumber 关联表。

    合刊的两个期号必须是两条独立关联记录，而不是把 3-4 塞进一个条码字段。
    """

    issue = models.ForeignKey(
        Issue, on_delete=models.CASCADE, related_name="numberings",
    )
    number = models.ForeignKey(
        IssueNumber, on_delete=models.CASCADE, related_name="numberings",
    )
    label = models.CharField("封面标识", max_length=40, blank=True,
                             help_text="如 no.3-4，仅作展示")

    class Meta:
        unique_together = ("issue", "number")


class Item(models.Model):
    """馆内实物（册）。一个条码 = 一个实物。"""

    class ItemStatus(models.TextChoices):
        AVAILABLE = "available", "在馆"
        CHECKED_OUT = "checked_out", "借出"
        LOST = "lost", "丢失"
        BOUND = "bound", "已装订"
        # 保护处理流程状态：只能由处理单事件驱动，不允许直接 PATCH
        QUARANTINE = "quarantine", "待隔离"
        IN_TREATMENT = "in_treatment", "处理中"
        TREATMENT_DONE = "treatment_done", "处理完成"
        DISCARDED = "discarded", "报废"

    # 馆藏层面「有副本」的状态（缺藏判定用；借出/装订仍算入藏，
    # 保护处理中与报废不算——处理中的实体不能作为可用副本抵消缺藏）
    HELD_STATUSES = (
        ItemStatus.AVAILABLE, ItemStatus.CHECKED_OUT, ItemStatus.BOUND,
    )
    # 由保护处理流程驱动的状态，不允许直接 PATCH
    CONSERVATION_STATUSES = (
        ItemStatus.QUARANTINE, ItemStatus.IN_TREATMENT, ItemStatus.TREATMENT_DONE,
    )

    barcode = models.CharField("条码", max_length=40, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="items",
    )
    issue = models.ForeignKey(
        Issue, on_delete=models.PROTECT, related_name="items",
        help_text="实物对应的发行期；合刊实物只指向这一个 Issue，"
                  "对多个期号的覆盖由 IssueNumbering 表达",
    )
    # 未装订时的实际位置；装订后以 binding 的 location 为准；
    # 保护处理中实际位置以处理单的 temporary_location 为准
    location = models.CharField("馆藏位置", max_length=100, blank=True)
    status = models.CharField(
        "馆藏状态", max_length=16,
        choices=ItemStatus.choices, default=ItemStatus.AVAILABLE,
    )
    accessioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

    @property
    def active_conservation_order(self):
        """进行中的保护处理单（待隔离/处理中/处理完成），无则 None。"""
        cached = getattr(self, "_active_conservation_orders", None)
        if cached is not None:
            return cached[0] if cached else None
        return self.conservation_orders.filter(
            status__in=CONSERVATION_ACTIVE_STATUSES,
        ).first()

    @property
    def is_serviceable(self):
        """读者当前是否可取：在架（含装订册内）且不在保护处理流程中。"""
        if self.active_conservation_order is not None:
            return False
        return self.status in (self.ItemStatus.AVAILABLE, self.ItemStatus.BOUND)

    def current_location(self):
        """实际位置：保护处理中取临时位置，装订后取装订册位置，否则取自身位置。"""
        order = self.active_conservation_order
        if order is not None and order.temporary_location:
            return order.temporary_location
        entry = getattr(self, "binding_entry", None)
        if entry is not None:
            return entry.binding.location
        return self.location

    def __str__(self):
        return self.barcode


class Binding(models.Model):
    """装订册：把若干已入藏实物装订在一起，实物身份与条码不变。"""

    call_number = models.CharField("装订索书号", max_length=60, unique=True)
    title = models.ForeignKey(
        Title, on_delete=models.CASCADE, related_name="bindings",
    )
    location = models.CharField("装订后位置", max_length=100)
    bound_month = models.DateField("装订月份", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    items = models.ManyToManyField(Item, through="BindingEntry", related_name="bindings")

    class Meta:
        verbose_name = "装订册"
        ordering = ["call_number"]

    def __str__(self):
        return self.call_number


class BindingEntry(models.Model):
    item = models.OneToOneField(
        Item, on_delete=models.CASCADE, related_name="binding_entry",
    )
    binding = models.ForeignKey(
        Binding, on_delete=models.CASCADE, related_name="entries",
    )
    # 装订时封存该实物原位置，拆订后恢复
    previous_location = models.CharField("装订前位置", max_length=100, blank=True)
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("item", "binding")

    def clean(self):
        if self.item_id and self.item.title_id != self.binding.title_id:
            raise ValidationError("装订册内的实物必须属于同一种刊。")


class ConservationOrder(models.Model):
    """保护处理单：实体受潮/虫害等情况下的隔离、送修、返还全流程凭证。

    状态机：待隔离 → 处理中 → 处理完成 → 已恢复（关闭）；任一进行中状态可报废。
    - version：每次成功应用事件 +1，事件必须携带所基于的版本，迟到事件被拒；
    - idempotency_key：开立幂等键，重复开立返回同一处理单；
    - 装订册内实体送修时，binding/binding_call_number 快照保证册关系可追溯，
      处理动作本身不改动 BindingEntry（不静默拆订）。
    """

    class Status(models.TextChoices):
        QUARANTINE = "quarantine", "待隔离"
        IN_TREATMENT = "in_treatment", "处理中"
        COMPLETED = "completed", "处理完成"
        CLOSED = "closed", "已恢复"
        DISCARDED = "discarded", "已报废"

    class Cause(models.TextChoices):
        DAMP = "damp", "受潮"
        PEST = "pest", "虫害"
        MOLD = "mold", "霉变"
        DAMAGE = "damage", "破损"
        OTHER = "other", "其他"

    ACTIVE_STATUSES = CONSERVATION_ACTIVE_STATUSES

    item = models.ForeignKey(
        Item, on_delete=models.CASCADE, related_name="conservation_orders",
    )
    cause = models.CharField("处理原因", max_length=10, choices=Cause.choices)
    description = models.CharField("情况说明", max_length=255, blank=True)
    status = models.CharField(
        "处理单状态", max_length=12,
        choices=Status.choices, default=Status.QUARANTINE,
    )
    temporary_location = models.CharField(
        "临时位置", max_length=100, blank=True,
        help_text="隔离/送修期间实体所在位置，定位与时间轴据此展示",
    )
    restore_location = models.CharField(
        "恢复位置", max_length=100, blank=True,
        help_text="处理完成返还后的去向；默认回原位置（装订册内实体回册位置）",
    )
    # 开立时所属装订册快照：处理动作不拆订，册与其他成员关系保持可追溯
    binding = models.ForeignKey(
        Binding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="conservation_orders",
    )
    binding_call_number = models.CharField(
        "所属装订册（快照）", max_length=60, blank=True,
    )
    version = models.PositiveIntegerField("版本", default=1)
    idempotency_key = models.CharField("开立幂等键", max_length=64, unique=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField("办结时间", null=True, blank=True)

    class Meta:
        verbose_name = "保护处理单"
        ordering = ["-opened_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["item"],
                condition=Q(status__in=CONSERVATION_ACTIVE_STATUSES),
                name="uniq_active_conservation_per_item",
            ),
        ]

    @property
    def is_active(self):
        return self.status in CONSERVATION_ACTIVE_STATUSES

    def __str__(self):
        return f"处理单#{self.pk} {self.item.barcode}（{self.get_status_display()}）"


class ConservationEvent(models.Model):
    """处理事件（追加式审计）：交接、状况评估、处理完成、返还、报废。

    - idempotency_key 在处理单内唯一：重复提交返回已记录结果，不重复入账；
    - version 记录事件应用后的处理单版本，形成完整有序历史；
    - 事件只增不改，处理中途刷新/重启后历史完整可查。
    """

    class Kind(models.TextChoices):
        OPEN = "open", "开立处理单"
        HANDOVER_OUT = "handover_out", "送出交接"
        ASSESSMENT = "assessment", "状况评估"
        COMPLETE = "complete", "处理完成"
        HANDOVER_IN = "handover_in", "返还交接"
        DISCARD = "discard", "报废"

    order = models.ForeignKey(
        ConservationOrder, on_delete=models.CASCADE, related_name="events",
    )
    kind = models.CharField("事件类型", max_length=15, choices=Kind.choices)
    idempotency_key = models.CharField("幂等键", max_length=64)
    version = models.PositiveIntegerField("应用后处理单版本")
    from_status = models.CharField(
        "原状态", max_length=12, blank=True,
        choices=ConservationOrder.Status.choices,
    )
    to_status = models.CharField(
        "新状态", max_length=12, choices=ConservationOrder.Status.choices,
    )
    condition_note = models.CharField("状况评估", max_length=255, blank=True)
    location = models.CharField(
        "事件后所在位置", max_length=100, blank=True,
        help_text="交接/评估时实体所在位置，会同步为处理单的临时位置",
    )
    operator = models.CharField("经办人", max_length=40, blank=True)
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "保护处理事件"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "idempotency_key"],
                name="uniq_cons_event_key_per_order",
            ),
        ]

    def __str__(self):
        return f"{self.order_id}#{self.version} {self.get_kind_display()}"


# 事件 → 允许的处理单状态迁移（评估不改变状态，只追加审计）
CONSERVATION_EVENT_TRANSITIONS = {
    ConservationEvent.Kind.HANDOVER_OUT: {
        ConservationOrder.Status.QUARANTINE: ConservationOrder.Status.IN_TREATMENT,
    },
    ConservationEvent.Kind.ASSESSMENT: {
        ConservationOrder.Status.QUARANTINE: ConservationOrder.Status.QUARANTINE,
        ConservationOrder.Status.IN_TREATMENT: ConservationOrder.Status.IN_TREATMENT,
        ConservationOrder.Status.COMPLETED: ConservationOrder.Status.COMPLETED,
    },
    ConservationEvent.Kind.COMPLETE: {
        ConservationOrder.Status.IN_TREATMENT: ConservationOrder.Status.COMPLETED,
    },
    ConservationEvent.Kind.HANDOVER_IN: {
        # 未送出而解除隔离、处理中直接返还、完成后返还，都归为「已恢复」
        ConservationOrder.Status.QUARANTINE: ConservationOrder.Status.CLOSED,
        ConservationOrder.Status.IN_TREATMENT: ConservationOrder.Status.CLOSED,
        ConservationOrder.Status.COMPLETED: ConservationOrder.Status.CLOSED,
    },
    ConservationEvent.Kind.DISCARD: {
        ConservationOrder.Status.QUARANTINE: ConservationOrder.Status.DISCARDED,
        ConservationOrder.Status.IN_TREATMENT: ConservationOrder.Status.DISCARDED,
        ConservationOrder.Status.COMPLETED: ConservationOrder.Status.DISCARDED,
    },
}

# 处理单进行中状态 → 实体馆藏状态
_ORDER_TO_ITEM_STATUS = {
    ConservationOrder.Status.QUARANTINE: Item.ItemStatus.QUARANTINE,
    ConservationOrder.Status.IN_TREATMENT: Item.ItemStatus.IN_TREATMENT,
    ConservationOrder.Status.COMPLETED: Item.ItemStatus.TREATMENT_DONE,
}


def open_conservation_order(item, *, cause, idempotency_key, description="",
                            temporary_location="", restore_location="",
                            operator="", condition_note=""):
    """开立保护处理单：实体进入待隔离。调用方须已持有 item 行锁。"""
    if item.status not in (Item.ItemStatus.AVAILABLE, Item.ItemStatus.BOUND):
        raise InvalidTransitionError(
            f"只有在馆/已装订的实体才能送保护处理（当前："
            f"{item.get_status_display()}）。"
        )
    if item.active_conservation_order is not None:
        raise InvalidTransitionError("该实体已有进行中的保护处理单。")
    entry = getattr(item, "binding_entry", None)
    if not restore_location:
        # 默认恢复位置：装订册内实体回册位置，散册回原馆藏位置
        restore_location = entry.binding.location if entry else item.location
    order = ConservationOrder.objects.create(
        item=item, cause=cause, description=description,
        temporary_location=temporary_location,
        restore_location=restore_location,
        binding=entry.binding if entry else None,
        binding_call_number=entry.binding.call_number if entry else "",
        idempotency_key=idempotency_key,
    )
    ConservationEvent.objects.create(
        order=order, kind=ConservationEvent.Kind.OPEN,
        idempotency_key=idempotency_key, version=1,
        from_status="", to_status=ConservationOrder.Status.QUARANTINE,
        condition_note=condition_note, location=temporary_location,
        operator=operator,
    )
    item.status = Item.ItemStatus.QUARANTINE
    item.save(update_fields=["status"])
    return order


def apply_conservation_event(order, *, kind, idempotency_key, version,
                             condition_note="", location="", operator="", note=""):
    """把事件应用到处理单（调用方须已 select_for_update 锁住处理单）。

    版本不符（迟到/并发）抛 StaleVersionError；状态不允许抛 InvalidTransitionError；
    两者都不改动任何数据，已入账历史保持不变。
    """
    transitions = CONSERVATION_EVENT_TRANSITIONS.get(kind)
    if transitions is None:
        raise InvalidTransitionError(f"事件类型不允许直接提交：{kind}")
    if version != order.version:
        raise StaleVersionError(
            f"事件基于版本 v{version}，但处理单已到 v{order.version}"
            f"（{order.get_status_display()}）；请刷新后按最新版本重试。"
        )
    to_status = transitions.get(order.status)
    if to_status is None:
        raise InvalidTransitionError(
            f"处理单当前为「{order.get_status_display()}」，"
            f"不能执行「{ConservationEvent.Kind(kind).label}」。"
        )
    from_status = order.status
    new_version = order.version + 1

    order.status = to_status
    order.version = new_version
    update_fields = ["status", "version", "updated_at"]
    if location and to_status in CONSERVATION_ACTIVE_STATUSES:
        # 交接/评估可更新临时位置（如从隔离室转到修复中心）
        order.temporary_location = location
        update_fields.append("temporary_location")
    if to_status in (ConservationOrder.Status.CLOSED,
                     ConservationOrder.Status.DISCARDED):
        order.closed_at = timezone.now()
        update_fields.append("closed_at")
    order.save(update_fields=update_fields)

    item = order.item
    if to_status == ConservationOrder.Status.CLOSED:
        # 返还：装订册内成员恢复为「已装订」（不能局部恢复成散册在馆），
        # 位置回到处理单记录的恢复位置
        item.status = (Item.ItemStatus.BOUND if item.is_bound
                       else Item.ItemStatus.AVAILABLE)
        if order.restore_location:
            item.location = order.restore_location
            item.save(update_fields=["status", "location"])
        else:
            item.save(update_fields=["status"])
    elif to_status == ConservationOrder.Status.DISCARDED:
        item.status = Item.ItemStatus.DISCARDED
        item.save(update_fields=["status"])
    else:
        item.status = _ORDER_TO_ITEM_STATUS[to_status]
        item.save(update_fields=["status"])

    return ConservationEvent.objects.create(
        order=order, kind=kind, idempotency_key=idempotency_key,
        version=new_version, from_status=from_status, to_status=to_status,
        condition_note=condition_note, location=location,
        operator=operator, note=note,
    )


def active_conservation_prefetch():
    """预取进行中处理单到 _active_conservation_orders，避免逐项查询。"""
    return Prefetch(
        "conservation_orders",
        queryset=ConservationOrder.objects.filter(
            status__in=CONSERVATION_ACTIVE_STATUSES,
        ),
        to_attr="_active_conservation_orders",
    )


def item_conservation_summary(item):
    """定位/时间轴用的进行中处理单摘要；无则 None。"""
    order = item.active_conservation_order
    if order is None:
        return None
    return {
        "order_id": order.id,
        "status": order.status,
        "cause": order.cause,
        "temporary_location": order.temporary_location,
        "restore_location": order.restore_location,
        "binding": order.binding_call_number or None,
        "version": order.version,
        "opened_at": order.opened_at,
    }


def unavailable_reason(item):
    """读者视角的不可服务原因；可服务返回 None。"""
    order = item.active_conservation_order
    if order is not None:
        return (f"保护{order.get_status_display()}（{order.get_cause_display()}），"
                f"暂不可取")
    if item.status in Item.CONSERVATION_STATUSES:
        return "保护处理中，暂不可取"
    if item.status == Item.ItemStatus.CHECKED_OUT:
        return "借出中"
    if item.status == Item.ItemStatus.LOST:
        return "已丢失"
    if item.status == Item.ItemStatus.DISCARDED:
        return "已报废"
    return None


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有可用实物（在馆/借出/装订；保护处理中与报废不算）
    issued+missing    已发行但缺藏（无实物，或实物丢失/报废/全部在保护处理中）
    not_published     缺号：没有任何发行记录，不自动等同缺藏
    ceased_gap        停刊后出现的编号（永远不会有发行）
    """
    issues = list(number.issues.prefetch_related("items"))
    if not issues:
        ceased = title.ceased_month
        if title.status == Title.PublicationStatus.CEASED and ceased:
            return "ceased_gap"
        return "not_published"
    items = [it for iss in issues for it in iss.items.all()]
    held = any(it.status in Item.HELD_STATUSES for it in items)
    return "issued+held" if held else "issued+missing"


def locate_item_row(item):
    """单个实物的定位视图：状态、实际位置、可服务性与保护处理信息。"""
    return {
        "barcode": item.barcode,
        "status": item.status,
        "location": item.current_location(),
        "bound": item.is_bound,
        "binding": item.binding_entry.binding.call_number if item.is_bound else None,
        "serviceable": item.is_serviceable,
        "unavailable_reason": unavailable_reason(item),
        "conservation": item_conservation_summary(item),
    }


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订、保护处理都可命中）。"""
    rows = []
    for issue in number.issues.all():
        items = issue.items.select_related(
            "title", "binding_entry__binding",
        ).prefetch_related(active_conservation_prefetch())
        for item in items:
            rows.append({"issue_id": issue.id, **locate_item_row(item)})
    return rows
