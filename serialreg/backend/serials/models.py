"""连续出版物登记领域模型。

四层结构：
  Title        连续出版物（书目层，可标记停刊）
  IssueNumber  卷期编号（一个编号槽位，跨发行月的跨年卷按卷+期唯一）
  Issue        发行实体（一次出版行为；普通期挂一个编号，合刊挂多个编号）
  Item         馆内实物（一条条码=一个实物，不允许一条条码代表多个合刊期号关系）
  Binding      装订册（多个 Item 装订在一起，拆订后 Item 恢复各自位置）

保护处理（受潮/虫害等）：
  PreservationOrder  保护处理单（一次处置工单，记录原因、评估、临时/恢复位置）
  PreservationEvent  处理事件（交接/评估/完成/返还/报废，带幂等键与版本）

两条易混的业务规则分开表达：
  缺号 = IssueNumber 没有对应 Issue（没有发行记录），不自动等于缺藏；
  缺藏 = 该编号已发行（存在 Issue），但没有可用实物（未入藏、丢失或全部在保护处理中）。
"""
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError


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
        # 保护处理流转状态：只能由保护处理单事件驱动，不能手工 PATCH
        QUARANTINE_PENDING = "quarantine_pending", "待隔离"
        IN_TREATMENT = "in_treatment", "处理中"
        TREATMENT_DONE = "treatment_done", "处理完成"
        DISCARDED = "discarded", "报废"

    # 保护处理占用的状态：实物不可服务，且不计入「可用副本」
    PRESERVATION_STATUSES = frozenset({
        ItemStatus.QUARANTINE_PENDING,
        ItemStatus.IN_TREATMENT,
        ItemStatus.TREATMENT_DONE,
    })
    # 可服务（可抵消缺藏）的状态；lost/保护处理/报废都不算
    SERVICEABLE_STATUSES = frozenset({
        ItemStatus.AVAILABLE,
        ItemStatus.CHECKED_OUT,
        ItemStatus.BOUND,
    })

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
    # 保护处理期间以处理单的临时位置为准（本字段仍是它的「家」位置）
    location = models.CharField("馆藏位置", max_length=100, blank=True)
    status = models.CharField(
        "馆藏状态", max_length=20,
        choices=ItemStatus.choices, default=ItemStatus.AVAILABLE,
    )
    accessioned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["barcode"]

    @property
    def is_bound(self):
        return hasattr(self, "binding_entry")

    @property
    def in_preservation(self):
        return self.status in self.PRESERVATION_STATUSES

    def active_preservation_order(self):
        """当前进行中的保护处理单（每件实物同时最多一张）。"""
        for order in self.preservation_orders.all():
            if order.status in PreservationOrder.ACTIVE_STATUSES:
                return order
        return None

    def preservation_display_order(self):
        """定位/时间轴展示用：进行中的处理单；已报废实物显示其报废单。"""
        latest_discarded = None
        for order in self.preservation_orders.all():
            if order.status in PreservationOrder.ACTIVE_STATUSES:
                return order
            if (latest_discarded is None
                    and order.status == PreservationOrder.OrderStatus.DISCARDED):
                latest_discarded = order
        if self.status == Item.ItemStatus.DISCARDED:
            return latest_discarded
        return None

    def current_location(self):
        """实际位置：保护处理中 → 临时位置；装订后 → 装订册位置；否则自身位置。"""
        order = self.active_preservation_order()
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


class PreservationOrder(models.Model):
    """保护处理单：一次受潮/虫害等保护处置的工单。

    实物从在馆（或已装订）进入待隔离 → 处理中 → 处理完成，
    最终返还上架（恢复位置）或报废。处理期间：
      - 实物不可服务，也不作为可用副本抵消缺藏；
      - 发行关系、条码、装订关系全部保留，只是位置暂指向临时位置；
      - 每次交接/评估/返还都写 PreservationEvent，带幂等键与版本。
    """

    class Cause(models.TextChoices):
        WATER = "water", "受潮"
        PEST = "pest", "虫害"
        MOLD = "mold", "霉变"
        OTHER = "other", "其他"

    class OrderStatus(models.TextChoices):
        QUARANTINE_PENDING = "quarantine_pending", "待隔离"
        IN_TREATMENT = "in_treatment", "处理中"
        TREATMENT_DONE = "treatment_done", "处理完成"
        RETURNED = "returned", "已返还"
        DISCARDED = "discarded", "已报废"

    # 进行中（未闭环）的处理单状态
    ACTIVE_STATUSES = frozenset({
        OrderStatus.QUARANTINE_PENDING,
        OrderStatus.IN_TREATMENT,
        OrderStatus.TREATMENT_DONE,
    })

    item = models.ForeignKey(
        Item, on_delete=models.PROTECT, related_name="preservation_orders",
        help_text="被处置的实物；发行/条码/装订关系不因处理而改变",
    )
    cause = models.CharField(
        "受损原因", max_length=10, choices=Cause.choices,
    )
    status = models.CharField(
        "处理单状态", max_length=20,
        choices=OrderStatus.choices, default=OrderStatus.QUARANTINE_PENDING,
    )
    condition_assessment = models.TextField("条件评估", blank=True)
    temporary_location = models.CharField("临时位置", max_length=100, blank=True)
    restore_location = models.CharField(
        "恢复位置", max_length=100, blank=True,
        help_text="处理完成后返还上架的位置；默认取开单时的实际位置",
    )
    # 开单时若实物在装订册中，快照册号；BindingEntry 本身全程保留
    binding_call_number = models.CharField(
        "处置时所在装订册", max_length=60, blank=True,
    )
    # 乐观并发版本：只接受更高版本的事件，迟到事件只记审计不生效
    version = models.PositiveIntegerField("版本", default=1)
    idempotency_key = models.CharField("幂等键", max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField("闭环时间", null=True, blank=True)

    class Meta:
        verbose_name = "保护处理单"
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["item"],
                condition=Q(status__in=[
                    "quarantine_pending", "in_treatment", "treatment_done",
                ]),
                name="unique_active_preservation_order_per_item",
            ),
        ]

    def __str__(self):
        return f"处理单#{self.pk} {self.item.barcode}（{self.get_cause_display()}）"


class PreservationEvent(models.Model):
    """保护处理事件：交接、条件评估、处理完成、返还、报废。

    - idempotency_key：同一处理单内唯一，重复提交不产生第二条事件；
    - version：事件版本必须高于处理单当前版本才生效，否则只留审计
      （applied=False），不会覆盖更新后的处置。
    """

    class EventType(models.TextChoices):
        OPEN = "open", "开立处理单"
        HANDOVER = "handover", "交接送出"
        ASSESS = "assess", "条件评估"
        COMPLETE = "complete", "处理完成"
        RETURN = "return", "返还上架"
        DISCARD = "discard", "报废"

    order = models.ForeignKey(
        PreservationOrder, on_delete=models.CASCADE, related_name="events",
    )
    event_type = models.CharField(
        "事件类型", max_length=10, choices=EventType.choices,
    )
    idempotency_key = models.CharField("幂等键", max_length=64)
    version = models.PositiveIntegerField("事件版本")
    applied = models.BooleanField(
        "已生效", default=True,
        help_text="False 表示迟到/过期事件：仅保留审计，不改变处置状态",
    )
    location = models.CharField(
        "事件位置", max_length=100, blank=True,
        help_text="交接时的临时位置 / 返还时的恢复位置",
    )
    condition_assessment = models.TextField("条件评估", blank=True)
    actor = models.CharField("经手人", max_length=60, blank=True)
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "保护处理事件"
        ordering = ["id"]
        unique_together = ("order", "idempotency_key")

    def __str__(self):
        return f"{self.get_event_type_display()}@{self.order_id} v{self.version}"


def number_holding_status(title, number):
    """计算某个期号的馆藏视图状态。

    issued+held       已发行且有可服务实物（在馆/借出/已装订）
    issued+missing    已发行但缺藏（无实物，或实物全部丢失/报废/在保护处理中——
                      处理中的实物不是可用副本，不能抵消缺藏）
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
    held = any(it.status in Item.SERVICEABLE_STATUSES for it in items)
    return "issued+held" if held else "issued+missing"


def item_preservation_dict(item):
    """定位/时间轴展示用的保护处理信息；无进行中（或报废）处理单时为 None。"""
    order = item.preservation_display_order()
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
    }


def item_locate_dict(item, issue):
    """定位结果中一个实物的公共视图（期号定位与条码反查共用）。"""
    return {
        "issue_id": issue.id,
        "barcode": item.barcode,
        "status": item.status,
        "serviceable": item.status in Item.SERVICEABLE_STATUSES,
        "location": item.current_location(),
        "bound": item.is_bound,
        "binding": item.binding_entry.binding.call_number if item.is_bound else None,
        "preservation": item_preservation_dict(item),
    }


def locate_number(number):
    """从任一期号找到其所在实物与实际位置（合刊、装订、保护处理都可命中）。"""
    rows = []
    issues = number.issues.prefetch_related(
        models.Prefetch(
            "items",
            queryset=Item.objects.select_related(
                "title", "binding_entry__binding",
            ).prefetch_related("preservation_orders"),
        ),
    )
    for issue in issues:
        for item in issue.items.all():
            rows.append(item_locate_dict(item, issue))
    return rows
