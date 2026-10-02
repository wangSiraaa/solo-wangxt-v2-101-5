from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import (
    Binding, ConservationOrder, Issue, IssueNumber, IssueNumbering, Item, Title,
    CONSERVATION_ACTIVE_STATUSES,
    InvalidTransitionError, StaleVersionError,
    active_conservation_prefetch, apply_conservation_event,
    locate_item_row, locate_number, number_holding_status,
    open_conservation_order,
)
from .serializers import (
    BindingSerializer, ConservationEventCreateSerializer,
    ConservationOrderOpenSerializer, ConservationOrderSerializer,
    IssueSerializer, ItemSerializer,
    IssueNumberSerializer, TitleSerializer, UnbindSerializer,
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
    ).prefetch_related("issue__numbers", active_conservation_prefetch())
    serializer_class = ItemSerializer

    @action(detail=False, methods=["get"])
    def locate(self, request):
        """按 (title, volume, number) 或 barcode 定位实物。

        合刊的任一期号都必须能找到同一实物；装订后返回装订册位置；
        保护处理中的实物返回临时位置与不可服务原因。
        """
        title_id = request.query_params.get("title")
        volume = request.query_params.get("volume", "")
        number = request.query_params.get("number")
        barcode = request.query_params.get("barcode")

        if barcode:
            items = self.get_queryset().filter(barcode=barcode)
            result = []
            for it in items:
                result.append({
                    "issue_id": it.issue_id,
                    "numbers": [
                        {"volume": n.volume, "number": n.number}
                        for n in it.issue.numbers.all()
                    ],
                    **locate_item_row(it),
                })
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


class ConservationOrderViewSet(viewsets.ModelViewSet):
    """保护处理单：开立（幂等）与事件追加（幂等键+版本）。

    处理单本身只允许读取与开立；状态推进一律走 events 动作，
    保证每次交接/评估/返还都留下有序审计。
    """

    queryset = ConservationOrder.objects.select_related(
        "item", "binding",
    ).prefetch_related("events")
    serializer_class = ConservationOrderSerializer
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
        if params.get("active") in ("1", "true"):
            qs = qs.filter(status__in=CONSERVATION_ACTIVE_STATUSES)
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs

    def _replay(self, order):
        data = self.get_serializer(order).data
        data["idempotent_replay"] = True
        return Response(data, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        serializer = ConservationOrderOpenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        key = data["idempotency_key"]
        # 幂等重放：同一开立幂等键直接返回已建处理单
        existing = ConservationOrder.objects.filter(idempotency_key=key).first()
        if existing is not None:
            return self._replay(existing)
        payload = {k: v for k, v in data.items() if k != "item"}
        with transaction.atomic():
            # 锁住实体行：同一实体的并开立串行化
            item = Item.objects.select_for_update().get(pk=data["item"].pk)
            try:
                with transaction.atomic():  # 保存点：唯一约束竞态兜底
                    order = open_conservation_order(item=item, **payload)
            except InvalidTransitionError as e:
                return Response(
                    {"detail": str(e)}, status=status.HTTP_409_CONFLICT,
                )
            except IntegrityError:
                order = None
            if order is None:  # 唯一约束兜底：并发下重复键或已有进行中处理单
                existing = ConservationOrder.objects.filter(
                    idempotency_key=key,
                ).first()
                if existing is not None:
                    return self._replay(existing)
                return Response(
                    {"detail": "该实体已有进行中的保护处理单。"},
                    status=status.HTTP_409_CONFLICT,
                )
        return Response(
            self.get_serializer(order).data, status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def events(self, request, pk=None):
        """追加处理事件（交接/评估/完成/返还/报废）。

        - 同一幂等键重复提交：返回已记录结果（idempotent_replay），不重复入账；
        - 版本与处理单当前版本不符（迟到/并发）：409，不覆盖更新后的处置。
        """
        serializer = ConservationEventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            order = (
                ConservationOrder.objects.select_for_update()
                .prefetch_related("events").get(pk=pk)
            )
            replay = order.events.filter(
                idempotency_key=data["idempotency_key"],
            ).first()
            if replay is not None:
                payload = self.get_serializer(order).data
                payload["idempotent_replay"] = True
                payload["replayed_event"] = replay.id
                return Response(payload, status=status.HTTP_200_OK)
            try:
                apply_conservation_event(order, **data)
            except StaleVersionError as e:
                return Response({
                    "detail": str(e),
                    "current": self.get_serializer(order).data,
                }, status=status.HTTP_409_CONFLICT)
            except InvalidTransitionError as e:
                return Response(
                    {"detail": str(e)}, status=status.HTTP_409_CONFLICT,
                )
        # 重新读取：事件预取缓存不含刚入账的事件
        order = ConservationOrder.objects.prefetch_related("events").get(pk=pk)
        return Response(
            self.get_serializer(order).data, status=status.HTTP_201_CREATED,
        )


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
                            ).prefetch_related(active_conservation_prefetch()),
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
                                **locate_item_row(it),
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
