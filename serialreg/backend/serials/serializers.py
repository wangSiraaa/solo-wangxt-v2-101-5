from django.db import transaction
from rest_framework import serializers

from .models import (
    Binding, BindingEntry, ConservationEvent, ConservationOrder,
    Issue, IssueNumber, IssueNumbering, Item, Title,
    item_conservation_summary,
)


class TitleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Title
        fields = [
            "id", "title", "issn", "publisher", "status",
            "ceased_month", "created_at",
        ]

    def validate(self, attrs):
        status = attrs.get("status", getattr(self.instance, "status", None))
        ceased_month = attrs.get(
            "ceased_month", getattr(self.instance, "ceased_month", None),
        )
        if status == Title.PublicationStatus.CEASED and not ceased_month:
            raise serializers.ValidationError(
                {"ceased_month": "停刊刊名必须填写停刊月份。"},
            )
        return attrs


class IssueNumberSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssueNumber
        fields = ["id", "title", "volume", "number", "sort_key"]


class IssueNumberingSerializer(serializers.ModelSerializer):
    number_id = serializers.IntegerField(source="number.id", read_only=True)
    volume = serializers.CharField(source="number.volume", read_only=True)
    number = serializers.CharField(source="number.number", read_only=True)
    label = serializers.CharField()

    class Meta:
        model = IssueNumbering
        fields = ["number_id", "volume", "number", "label"]


class IssueSerializer(serializers.ModelSerializer):
    """发行期。numbers 为期号 id 列表：普通期 1 个，合刊 ≥2 个。"""

    number_ids = serializers.PrimaryKeyRelatedField(
        queryset=IssueNumber.objects.all(),
        many=True, write_only=True, source="numbers",
    )
    numberings = IssueNumberingSerializer(many=True, read_only=True)

    class Meta:
        model = Issue
        fields = [
            "id", "title", "kind", "issue_month", "issue_month_end",
            "note", "number_ids", "numberings", "created_at",
        ]

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        numbers = attrs.get("numbers")
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if numbers is not None:
            if len(numbers) < 1:
                raise serializers.ValidationError(
                    {"number_ids": "至少关联一个期号。"},
                )
            wrong = [n.id for n in numbers if n.title_id != title.id]
            if wrong:
                raise serializers.ValidationError(
                    {"number_ids": f"期号 {wrong} 不属于该刊。"},
                )
            if len({n.id for n in numbers}) != len(numbers):
                raise serializers.ValidationError(
                    {"number_ids": "期号不能重复。"},
                )
            if kind == Issue.IssueKind.COMBINED and len(numbers) < 2:
                raise serializers.ValidationError(
                    {"number_ids": "合刊必须关联至少两个期号。"},
                )
            if kind == Issue.IssueKind.REGULAR and len(numbers) != 1:
                raise serializers.ValidationError(
                    {"number_ids": "普通期只能关联一个期号。"},
                )
            # 一个编号槽位只能被发行一次
            qs = IssueNumbering.objects.filter(number__in=numbers)
            if self.instance:
                qs = qs.exclude(issue=self.instance)
            if qs.exists():
                used = list(
                    qs.values_list("number__volume", "number__number", "issue_id"),
                )
                raise serializers.ValidationError(
                    {"number_ids": f"期号已被其他发行期占用：{used}"},
                )
        start = attrs.get("issue_month", getattr(self.instance, "issue_month", None))
        end = attrs.get(
            "issue_month_end", getattr(self.instance, "issue_month_end", None),
        )
        if start and end and end < start:
            raise serializers.ValidationError(
                {"issue_month_end": "截止年月不能早于起始年月。"},
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        numbers = validated_data.pop("numbers")
        issue = Issue.objects.create(**validated_data)
        labels = self._labels(issue, numbers)
        IssueNumbering.objects.bulk_create([
            IssueNumbering(issue=issue, number=n, label=labels.get(n.id, ""))
            for n in numbers
        ])
        return issue

    def _labels(self, issue, numbers):
        raw = self.initial_data.get("labels") or {}
        labels = {}
        if isinstance(raw, dict):
            for n in numbers:
                labels[n.id] = str(raw.get(str(n.id), raw.get(n.id, "")))
        if not any(labels.values()):
            joined = "-".join(n.number for n in numbers)
            labels = {n.id: f"no.{joined}" if len(numbers) > 1 else "" for n in numbers}
        return labels


class ItemSerializer(serializers.ModelSerializer):
    """入藏实物。current_location 在装订后取装订册位置、处理中取临时位置。"""

    current_location = serializers.SerializerMethodField()
    bound = serializers.SerializerMethodField()
    binding_call_number = serializers.SerializerMethodField()
    number_ids = serializers.SerializerMethodField()
    serviceable = serializers.SerializerMethodField()
    active_conservation = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            "id", "barcode", "title", "issue", "location", "status",
            "accessioned_at", "current_location", "bound",
            "binding_call_number", "number_ids", "serviceable",
            "active_conservation",
        ]
        read_only_fields: list = []

    def validate_status(self, value):
        # status 可经 PATCH 修改（报失/找回）；bound 只能由装订/拆订流程设置
        if value == Item.ItemStatus.BOUND:
            raise serializers.ValidationError(
                "已装订状态只能通过装订/拆订操作变更。",
            )
        # 保护处理状态（含报废）只能由处理单事件驱动
        if value in Item.CONSERVATION_STATUSES or value == Item.ItemStatus.DISCARDED:
            raise serializers.ValidationError(
                "保护处理状态只能通过保护处理单的交接/返还/报废事件变更。",
            )
        if self.instance is not None:
            if self.instance.status == Item.ItemStatus.DISCARDED:
                raise serializers.ValidationError(
                    "已报废实体为终态，不能再变更状态。",
                )
            order = self.instance.active_conservation_order
            if order is not None:
                raise serializers.ValidationError(
                    f"该实体正在保护处理中（处理单 #{order.id}，"
                    f"{order.get_status_display()}），不能标记为可服务；"
                    "请通过处理单的交接/返还事件变更状态。",
                )
        return value

    def get_current_location(self, obj):
        return obj.current_location()

    def get_bound(self, obj):
        return obj.is_bound

    def get_binding_call_number(self, obj):
        return obj.binding_entry.binding.call_number if obj.is_bound else None

    def get_number_ids(self, obj):
        return list(obj.issue.numbers.values_list("id", flat=True))

    def get_serviceable(self, obj):
        return obj.is_serviceable

    def get_active_conservation(self, obj):
        return item_conservation_summary(obj)

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        issue = attrs.get("issue", getattr(self.instance, "issue", None))
        if issue and title and issue.title_id != title.id:
            raise serializers.ValidationError("实物所属刊与发行期不一致。")
        return attrs


class BindingSerializer(serializers.ModelSerializer):
    item_ids = serializers.PrimaryKeyRelatedField(
        queryset=Item.objects.all(), many=True, write_only=True,
    )
    items = serializers.SerializerMethodField()

    class Meta:
        model = Binding
        fields = [
            "id", "call_number", "title", "location", "bound_month",
            "item_ids", "items", "created_at",
        ]

    def get_items(self, obj):
        return [
            {
                "barcode": e.item.barcode,
                "status": e.item.status,
                "previous_location": e.previous_location,
                "current_location": e.item.current_location(),
                "conservation": item_conservation_summary(e.item),
            }
            for e in obj.entries.select_related("item")
        ]

    def validate_item_ids(self, items):
        if not items:
            raise serializers.ValidationError("装订册至少包含一个实物。")
        # 合刊实物会同时挂在多个期号下，前端可能重复勾选：按主键去重
        deduped = list({it.id: it for it in items}.values())
        already = [it.barcode for it in deduped if it.is_bound]
        if already:
            raise serializers.ValidationError(
                f"实物已在装订册中：{already}，请先拆订。",
            )
        # 保护处理中/已报废的实体不能装订
        treating = [
            it.barcode for it in deduped
            if it.active_conservation_order is not None
            or it.status in Item.CONSERVATION_STATUSES
            or it.status == Item.ItemStatus.DISCARDED
        ]
        if treating:
            raise serializers.ValidationError(
                f"以下实物正在保护处理中或已报废，不能装订：{treating}。",
            )
        return deduped

    def validate(self, attrs):
        title = attrs["title"]
        bad = [it.barcode for it in attrs["item_ids"] if it.title_id != title.id]
        if bad:
            raise serializers.ValidationError(
                {"item_ids": f"以下实物不属于该刊，不能混装：{bad}"},
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        items = validated_data.pop("item_ids")
        binding = Binding.objects.create(**validated_data)
        BindingEntry.objects.bulk_create([
            BindingEntry(
                item=it, binding=binding, previous_location=it.location or "",
            )
            for it in items
        ])
        Item.objects.filter(id__in=[it.id for it in items]).update(
            status=Item.ItemStatus.BOUND,
        )
        return binding


class UnbindSerializer(serializers.Serializer):
    """拆订：恢复每个实物装订前的位置与在馆状态。"""

    binding_id = serializers.PrimaryKeyRelatedField(
        queryset=Binding.objects.all(),
    )

    @transaction.atomic
    def save(self, **kwargs):
        binding = self.validated_data["binding_id"]
        entries = list(binding.entries.select_related("item"))
        # 有成员在保护处理中时禁止拆订：不能静默解除其与册的关系，
        # 也不能让册内只有部分成员被恢复可用
        blocked = [
            e.item.barcode for e in entries
            if e.item.active_conservation_order is not None
            or e.item.status in Item.CONSERVATION_STATUSES
        ]
        if blocked:
            raise serializers.ValidationError(
                f"装订册成员正在保护处理中，不能拆订：{blocked}。"
                "请先完成处理并返还，再拆订。",
            )
        for e in entries:
            e.item.location = e.previous_location
            e.item.status = Item.ItemStatus.AVAILABLE
            e.item.save(update_fields=["location", "status"])
        BindingEntry.objects.filter(binding=binding).delete()
        binding.delete()
        return [e.item for e in entries]


# ---------- 保护处理单 ----------

class ConservationEventSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    from_status_label = serializers.CharField(
        source="get_from_status_display", read_only=True,
    )
    to_status_label = serializers.CharField(
        source="get_to_status_display", read_only=True,
    )

    class Meta:
        model = ConservationEvent
        fields = [
            "id", "kind", "kind_label", "version",
            "from_status", "from_status_label", "to_status", "to_status_label",
            "condition_note", "location", "operator", "note",
            "idempotency_key", "created_at",
        ]


class ConservationOrderSerializer(serializers.ModelSerializer):
    """保护处理单详情：含完整事件历史（审计）。"""

    events = ConservationEventSerializer(many=True, read_only=True)
    item_barcode = serializers.CharField(source="item.barcode", read_only=True)
    title_id = serializers.IntegerField(source="item.title_id", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    cause_label = serializers.CharField(source="get_cause_display", read_only=True)
    active = serializers.SerializerMethodField()

    class Meta:
        model = ConservationOrder
        fields = [
            "id", "item", "item_barcode", "title_id",
            "cause", "cause_label", "description",
            "status", "status_label", "active",
            "temporary_location", "restore_location",
            "binding", "binding_call_number",
            "version", "idempotency_key",
            "opened_at", "updated_at", "closed_at", "events",
        ]

    def get_active(self, obj):
        return obj.is_active


class ConservationOrderOpenSerializer(serializers.Serializer):
    """开立保护处理单：实体从在馆/已装订进入待隔离。"""

    item = serializers.PrimaryKeyRelatedField(queryset=Item.objects.all())
    cause = serializers.ChoiceField(choices=ConservationOrder.Cause.choices)
    description = serializers.CharField(
        required=False, allow_blank=True, max_length=255,
    )
    temporary_location = serializers.CharField(
        required=False, allow_blank=True, max_length=100,
    )
    restore_location = serializers.CharField(
        required=False, allow_blank=True, max_length=100,
    )
    operator = serializers.CharField(
        required=False, allow_blank=True, max_length=40,
    )
    condition_note = serializers.CharField(required=False, allow_blank=True)
    idempotency_key = serializers.CharField(max_length=64)


class ConservationEventCreateSerializer(serializers.Serializer):
    """追加处理事件：必须带幂等键与所基于的处理单版本。"""

    kind = serializers.ChoiceField(choices=[
        c for c in ConservationEvent.Kind.choices
        if c[0] != ConservationEvent.Kind.OPEN
    ])
    idempotency_key = serializers.CharField(max_length=64)
    version = serializers.IntegerField(min_value=1)
    condition_note = serializers.CharField(required=False, allow_blank=True)
    location = serializers.CharField(
        required=False, allow_blank=True, max_length=100,
    )
    operator = serializers.CharField(
        required=False, allow_blank=True, max_length=40,
    )
    note = serializers.CharField(
        required=False, allow_blank=True, max_length=255,
    )
