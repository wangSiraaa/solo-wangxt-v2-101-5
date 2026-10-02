from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    Binding, Issue, IssueNumber, IssueNumbering, Item,
    PreservationOrder, Title,
    item_locate_dict, item_preservation_dict, locate_number,
    number_holding_status,
)
from .serializers import (
    BindingSerializer, IssueSerializer, ItemSerializer,
    IssueNumberSerializer, PreservationEventSerializer,
    PreservationOrderSerializer, TitleSerializer, UnbindSerializer,
    record_preservation_event,
)


class TitleViewSet(viewsets.ModelViewSet):
    queryset = Title.objects.all()
    serializer_class = TitleSerializer


class IssueNumberViewSet(viewsets.ModelViewSet):
    queryset = IssueNumber.objects.all()
    serializer_class = IssueNumberSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.prefetch_related(
        "numberings__number", "items",
    ).select_related("title")
    serializer_class = IssueSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs


class ItemViewSet(viewsets.ModelViewSet):
    queryset = Item.objects.select_related(
        "title", "issue", "binding_entry__binding",
    ).prefetch_related("issue__numbers", "preservation_orders")
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置；
        保护处理中的实物返回临时位置与处理状态，但发行/条码关系不变。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            result = []
            for it in items:
                row = item_locate_dict(it, it.issue)
                row["numbers"] = [
                    {"volume": n.volume, "number": n.number}
                    for n in it.issue.numbers.all()
                ]
                result.append(row)
            return Response({"query": {"barcode": barcode}, "matches": result})

        if not (title_id and number):
            return Response(
                {"detail": "需要提供 barcode，或同时提供 title 与 number。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = IssueNumber.objects.filter(
            title_id=title_id, number=number,
        )
        if volume != "":
            qs = qs.filter(volume=volume)
        try:
            issue_number = qs.get()
        except IssueNumber.DoesNotExist:
            # 编号本身未登记：区别于「已登记但无发行」的缺号
            return Response({
                "detail": "该卷期编号未在馆藏系统登记。",
                "holding_status": "unregistered",
                "matches": [],
            }, status=status.HTTP_404_NOT_FOUND)
        except IssueNumber.MultipleObjectsReturned:
            return Response(
                {"detail": "卷/期定位到多条编号，请补全卷号。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        matches = locate_number(issue_number)
        # 缺号（无发行记录）是正常业务状态，返回 200，不自动等同缺藏
        return Response({
            "query": {"title": title_id, "volume": volume, "number": number},
            "holding_status": number_holding_status(issue_number.title, issue_number),
            "matches": matches,
        })


class BindingViewSet(viewsets.ModelViewSet):
    queryset = Binding.objects.prefetch_related(
        "entries__item",
    ).select_related("title")
    serializer_class = BindingSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        title_id = self.request.query_params.get("title")
        if title_id:
            qs = qs.filter(title_id=title_id)
        return qs

    @action(detail=False, methods=["post"])
    def unbind(self, request):
        serializer = UnbindSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.save()
        return Response({
            "detail": "拆订完成，各实物已恢复原位置。",
            "restored": [
                {"barcode": it.barcode, "location": it.location,
                 "status": it.status}
                for it in items
            ],
        })


class TimelineViewSet(viewsets.ViewSet):
    """前端时间轴数据源：编号 × 发行 × 实物三层，外加停刊标记。"""

    def list(self, request):
        title_id = request.query_params.get("title")
        if not title_id:
            return Response(
                {"detail": "需要提供 title 参数。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = Title.objects.get(pk=title_id)
        numbers = (
            IssueNumber.objects.filter(title=title)
            .prefetch_related(
                Prefetch(
                    "issues",
                    queryset=Issue.objects.prefetch_related(
                        Prefetch(
                            "numberings",
                            queryset=IssueNumbering.objects.select_related("number"),
                        ),
                        Prefetch(
                            "items",
                            queryset=Item.objects.select_related(
                                "binding_entry__binding",
                            ).prefetch_related("preservation_orders"),
                        ),
                    ),
                ),
            )
            .order_by("sort_key", "id")
        )
        slots = []
        for n in numbers:
            issues = list(n.issues.all())
            items = [it for iss in issues for it in iss.items.all()]
            slots.append({
                "number_id": n.id,
                "volume": n.volume,
                "number": n.number,
                "holding_status": number_holding_status(title, n),
                "issues": [
                    {
                        "issue_id": iss.id,
                        "kind": iss.kind,
                        "issue_month": iss.issue_month,
                        "issue_month_end": iss.issue_month_end,
                        "label": "·".join(
                            f"{nn.number.volume}({nn.number.number})"
                            for nn in iss.numberings.all()
                        ),
                        "combined_numbers": [
                            {"volume": nn.number.volume, "number": nn.number.number}
                            for nn in iss.numberings.all()
                        ],
                        "items": [
                            {
                                "item_id": it.id,
                                "barcode": it.barcode,
                                "status": it.status,
                                "serviceable": (
                                    it.status in Item.SERVICEABLE_STATUSES
                                ),
                                "location": it.current_location(),
                                "bound": it.is_bound,
                                "binding": it.binding_entry.binding.call_number
                                if it.is_bound else None,
                                "preservation": item_preservation_dict(it),
                            }
                            for it in iss.items.all()
                        ],
                    }
                    for iss in issues
                ],
            })
        return Response({
            "title": TitleSerializer(title).data,
            "slots": slots,
        })


class PreservationOrderViewSet(viewsets.ModelViewSet):
    """保护处理单：开单（幂等）、查询、追加处理事件（幂等键+版本）。"""

    queryset = PreservationOrder.objects.select_related(
        "item", "item__binding_entry__binding",
    ).prefetch_related("events")
    serializer_class = PreservationOrderSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("title"):
            qs = qs.filter(item__title_id=params["title"])
        if params.get("item"):
            qs = qs.filter(item_id=params["item"])
        if params.get("barcode"):
            qs = qs.filter(item__barcode=params["barcode"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("active") in ("1", "true"):
            qs = qs.filter(status__in=PreservationOrder.ACTIVE_STATUSES)
        return qs

    def create(self, request, *args, **kwargs):
        """开单幂等：同一 idempotency_key 重复提交返回原单，不重复建单。"""
        key = request.data.get("idempotency_key")
        if key:
            existing = PreservationOrder.objects.filter(
                idempotency_key=key,
            ).first()
            if existing is not None:
                return Response({
                    "duplicate": True,
                    "order": PreservationOrderSerializer(existing).data,
                }, status=status.HTTP_200_OK)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                order = serializer.save()
        except IntegrityError:
            # 并发下同键撞唯一约束：以先到的单为准
            order = PreservationOrder.objects.get(idempotency_key=key)
            return Response({
                "duplicate": True,
                "order": PreservationOrderSerializer(order).data,
            }, status=status.HTTP_200_OK)
        return Response({
            "duplicate": False,
            "order": PreservationOrderSerializer(order).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def events(self, request, pk=None):
        """追加处理事件（交接/评估/完成/返还/报废）。

        - 幂等键重复 → 200，返回原事件，不重复生效；
        - 事件版本 ≤ 处理单当前版本 → 201 记录审计（applied=false），
          不覆盖更新后的处置；
        - 状态机不允许的流转 → 400。
        """
        order = self.get_object()
        serializer = PreservationEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event, outcome = record_preservation_event(
            order, **serializer.validated_data,
        )
        order.refresh_from_db()
        payload = {
            "outcome": outcome,
            "duplicate": outcome == "duplicate",
            "applied": outcome == "applied",
            "event": PreservationEventSerializer(event).data,
            "order": PreservationOrderSerializer(order).data,
        }
        if outcome == "duplicate":
            return Response(payload, status=status.HTTP_200_OK)
        return Response(payload, status=status.HTTP_201_CREATED)
