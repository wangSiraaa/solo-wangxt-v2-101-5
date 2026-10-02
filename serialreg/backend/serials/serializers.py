from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Binding, BindingEntry, Issue, IssueNumber, IssueNumbering, Item,
    PreservationEvent, PreservationOrder, Title,
    item_preservation_dict,
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
    """入藏实物。current_location 在装订后取装订册位置、处理期间取临时位置。"""

    current_location = serializers.SerializerMethodField()
    bound = serializers.SerializerMethodField()
    binding_call_number = serializers.SerializerMethodField()
    number_ids = serializers.SerializerMethodField()
    preservation = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            "id", "barcode", "title", "issue", "location", "status",
            "accessioned_at", "current_location", "bound",
            "binding_call_number", "number_ids", "preservation",
        ]
        read_only_fields: list = []

    def validate_status(self, value):
        # status 可经 PATCH 修改（报失/找回）；bound 只能由装订/拆订流程设置；
        # 保护处理状态只能由处理单事件驱动，不能手工标记
        if value == Item.ItemStatus.BOUND:
            raise serializers.ValidationError(
                "已装订状态只能通过装订/拆订操作变更。",
            )
        if (value in Item.PRESERVATION_STATUSES
                or value == Item.ItemStatus.DISCARDED):
            raise serializers.ValidationError(
                "保护处理相关状态只能通过保护处理单及其事件变更。",
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

    def get_preservation(self, obj):
        return item_preservation_dict(obj)

    def validate(self, attrs):
        title = attrs.get("title", getattr(self.instance, "title", None))
        issue = attrs.get("issue", getattr(self.instance, "issue", None))
        if issue and title and issue.title_id != title.id:
            raise serializers.ValidationError("实物所属刊与发行期不一致。")
        if self.instance is not None and "status" in attrs:
            # 处理中的实物不能被直接标记为可服务（或报失等），
            # 必须走处理单的返还/报废事件，保证审计与位置恢复一致
            if self.instance.in_preservation:
                raise serializers.ValidationError(
                    {"status": "实物正在保护处理中，不能直接在馆藏状态上变更；"
                               "请通过保护处理单的返还/报废事件闭环。"},
                )
            if self.instance.status == Item.ItemStatus.DISCARDED:
                raise serializers.ValidationError(
                    {"status": "实物已报废，状态为终态，不能再变更。"},
                )
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
                "in_preservation": e.item.in_preservation,
                "previous_location": e.previous_location,
                "current_location": (
                    e.item.current_location() if e.item.in_preservation
                    else obj.location
                ),
            }
            for e in obj.entries.select_related("item")
                .prefetch_related("item__preservation_orders")
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
        # 保护处理中的实物不可服务，不能（再次）装订
        in_pres = [it.barcode for it in deduped if it.in_preservation]
        if in_pres:
            raise serializers.ValidationError(
                f"实物正在保护处理中，不能装订：{in_pres}。",
            )
        discarded = [
            it.barcode for it in deduped
            if it.status == Item.ItemStatus.DISCARDED
        ]
        if discarded:
            raise serializers.ValidationError(
                f"实物已报废，不能装订：{discarded}。",
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

    def validate_binding_id(self, binding):
        # 册内成员正在保护处理时禁止拆订：实物不在册位，
        # 拆订会静默解除关系并把它错误地恢复为可服务
        in_pres = [
            e.item.barcode for e in binding.entries.select_related("item")
            if e.item.in_preservation
        ]
        if in_pres:
            raise serializers.ValidationError(
                f"装订册成员正在保护处理中：{in_pres}，"
                f"须先经处理单返还闭环，不能拆订。",
            )
        return binding

    @transaction.atomic
    def save(self, **kwargs):
        binding = self.validated_data["binding_id"]
        entries = list(binding.entries.select_related("item"))
        for e in entries:
            e.item.location = e.previous_location
            e.item.status = Item.ItemStatus.AVAILABLE
            e.item.save(update_fields=["location", "status"])
        BindingEntry.objects.filter(binding=binding).delete()
        binding.delete()
        return [e.item for e in entries]


# ---------- 保护处理 ----------

class PreservationEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreservationEvent
        fields = [
            "id", "event_type", "idempotency_key", "version", "applied",
            "location", "condition_assessment", "actor", "note", "created_at",
        ]
        read_only_fields = ["applied", "created_at"]


class PreservationOrderSerializer(serializers.ModelSerializer):
    """保护处理单。开单即把实物置为待隔离，并写入第一条开立事件。"""

    events = PreservationEventSerializer(many=True, read_only=True)
    barcode = serializers.CharField(source="item.barcode", read_only=True)
    item_status = serializers.CharField(source="item.status", read_only=True)
    title = serializers.IntegerField(source="item.title_id", read_only=True)

    class Meta:
        model = PreservationOrder
        fields = [
            "id", "item", "barcode", "item_status", "title",
            "cause", "status", "condition_assessment",
            "temporary_location", "restore_location",
            "binding_call_number", "version", "idempotency_key",
            "events", "created_at", "updated_at", "closed_at",
        ]
        read_only_fields = ["status", "version", "closed_at"]

    def validate_item(self, item):
        if item.status not in (
                Item.ItemStatus.AVAILABLE, Item.ItemStatus.BOUND):
            raise serializers.ValidationError(
                f"实物当前状态为「{item.get_status_display()}」，"
                f"只有在馆或已装订的实物才能开立保护处理单。",
            )
        if item.active_preservation_order() is not None:
            raise serializers.ValidationError(
                "该实物已存在进行中的保护处理单，请先闭环再开新单。",
            )
        return item

    @transaction.atomic
    def create(self, validated_data):
        item = Item.objects.select_for_update().get(pk=validated_data["item"].pk)
        order = PreservationOrder.objects.create(
            item=item,
            cause=validated_data["cause"],
            condition_assessment=validated_data.get("condition_assessment", ""),
            temporary_location=validated_data.get("temporary_location", ""),
            # 恢复位置默认取开单时的实际位置（装订册位或自身架位）
            restore_location=validated_data.get("restore_location")
            or item.current_location(),
            binding_call_number=(
                item.binding_entry.binding.call_number if item.is_bound else ""
            ),
            idempotency_key=validated_data["idempotency_key"],
            version=1,
        )
        PreservationEvent.objects.create(
            order=order,
            event_type=PreservationEvent.EventType.OPEN,
            idempotency_key=f"{order.idempotency_key}:open",
            version=1,
            location=order.temporary_location,
            condition_assessment=order.condition_assessment,
            note="开立保护处理单",
        )
        item.status = Item.ItemStatus.QUARANTINE_PENDING
        item.save(update_fields=["status"])
        return order


# 事件 → 允许的前置处理单状态（状态机）
PRESERVATION_TRANSITIONS = {
    PreservationEvent.EventType.HANDOVER: {
        PreservationOrder.OrderStatus.QUARANTINE_PENDING,
    },
    PreservationEvent.EventType.ASSESS: {
        PreservationOrder.OrderStatus.QUARANTINE_PENDING,
        PreservationOrder.OrderStatus.IN_TREATMENT,
        PreservationOrder.OrderStatus.TREATMENT_DONE,
    },
    PreservationEvent.EventType.COMPLETE: {
        PreservationOrder.OrderStatus.IN_TREATMENT,
    },
    PreservationEvent.EventType.RETURN: {
        PreservationOrder.OrderStatus.IN_TREATMENT,
        PreservationOrder.OrderStatus.TREATMENT_DONE,
    },
    PreservationEvent.EventType.DISCARD: {
        PreservationOrder.OrderStatus.QUARANTINE_PENDING,
        PreservationOrder.OrderStatus.IN_TREATMENT,
        PreservationOrder.OrderStatus.TREATMENT_DONE,
    },
}


@transaction.atomic
def record_preservation_event(order, *, event_type, idempotency_key, version,
                              location="", condition_assessment="",
                              actor="", note=""):
    """向处理单追加一个事件，返回 (event, outcome)。

    outcome：
      duplicate  幂等键已存在 → 不重复生效，直接返回原事件
      late       事件版本不高于处理单当前版本 → 只写审计（applied=False），
                 不覆盖更新后的处置
      applied    正常生效：推进处理单/实物状态，记录位置与评估
    """
    order = (
        PreservationOrder.objects.select_for_update().get(pk=order.pk)
    )
    existing = order.events.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, "duplicate"

    if version <= order.version:
        # 迟到/过期事件：审计必须保留，但状态以更新版本为准
        event = PreservationEvent.objects.create(
            order=order, event_type=event_type,
            idempotency_key=idempotency_key, version=version,
            applied=False, location=location,
            condition_assessment=condition_assessment,
            actor=actor, note=note,
        )
        return event, "late"

    allowed = PRESERVATION_TRANSITIONS[event_type]
    if order.status not in allowed:
        raise serializers.ValidationError(
            {"event_type": f"处理单当前状态为"
                           f"「{order.get_status_display()}」，"
                           f"不能执行「{PreservationEvent.EventType(event_type).label}」。"},
        )

    item = Item.objects.select_for_update().get(pk=order.item_id)
    # 位置缺省值：交接记临时位置，返还记恢复位置，保证审计完整
    if not location:
        if event_type == PreservationEvent.EventType.HANDOVER:
            location = order.temporary_location
        elif event_type == PreservationEvent.EventType.RETURN:
            location = order.restore_location

    event = PreservationEvent.objects.create(
        order=order, event_type=event_type,
        idempotency_key=idempotency_key, version=version,
        applied=True, location=location,
        condition_assessment=condition_assessment,
        actor=actor, note=note,
    )

    et = PreservationEvent.EventType
    if event_type == et.HANDOVER:
        order.status = PreservationOrder.OrderStatus.IN_TREATMENT
        if location:
            order.temporary_location = location
        item.status = Item.ItemStatus.IN_TREATMENT
    elif event_type == et.ASSESS:
        if condition_assessment:
            order.condition_assessment = condition_assessment
    elif event_type == et.COMPLETE:
        order.status = PreservationOrder.OrderStatus.TREATMENT_DONE
        item.status = Item.ItemStatus.TREATMENT_DONE
    elif event_type == et.RETURN:
        order.status = PreservationOrder.OrderStatus.RETURNED
        if location:
            order.restore_location = location
        order.closed_at = timezone.now()
        if item.is_bound:
            # 装订册成员：关系全程未动，返还后恢复「已装订」，
            # 实际位置仍取装订册，不做局部恢复为可服务散件
            item.status = Item.ItemStatus.BOUND
        else:
            item.status = Item.ItemStatus.AVAILABLE
            item.location = order.restore_location or item.location
    elif event_type == et.DISCARD:
        order.status = PreservationOrder.OrderStatus.DISCARDED
        order.closed_at = timezone.now()
        item.status = Item.ItemStatus.DISCARDED

    order.version = version
    order.save()
    item.save()
    return event, "applied"
