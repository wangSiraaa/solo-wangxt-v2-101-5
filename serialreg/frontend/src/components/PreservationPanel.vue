<template>
  <div class="panel">
    <h2>保护处理单（受潮 / 虫害处置）</h2>

    <!-- 开单 -->
    <div class="row">
      <label class="field"><b>实物</b>
        <select v-model="form.item" style="min-width: 180px">
          <option :value="null">选择可处置实物</option>
          <option v-for="it in candidates" :key="it.item_pk" :value="it.item_pk">
            {{ it.barcode }}（{{ it.statusLabel }}）
          </option>
        </select>
      </label>
      <label class="field"><b>受损原因</b>
        <select v-model="form.cause">
          <option v-for="(label, k) in causes" :key="k" :value="k">{{ label }}</option>
        </select>
      </label>
      <label class="field"><b>临时位置</b>
        <input v-model="form.temporary_location" placeholder="隔离柜 Q-1" style="width: 130px" />
      </label>
      <label class="field"><b>恢复位置（可空）</b>
        <input v-model="form.restore_location" placeholder="默认取当前位置" style="width: 130px" />
      </label>
    </div>
    <div class="row" style="margin-top:6px">
      <label class="field" style="flex:1"><b>条件评估</b>
        <input v-model="form.condition_assessment" placeholder="如：书脊受潮、封面霉斑…" />
      </label>
      <button @click="openOrder" :disabled="!form.item">开立处理单</button>
    </div>
    <p class="muted">
      开单后实物进入「待隔离」，暂时不可取、不参与装订；交接/评估/返还都会记入处理单时间线。
    </p>

    <!-- 处理单列表 -->
    <h3 v-if="orders.length">处理单（{{ orders.length }}）</h3>
    <div v-for="o in orders" :key="o.id" class="order-card"
         :class="{ closed: !isActive(o.status) }">
      <div class="row">
        <strong>#{{ o.id }}</strong>
        <code>{{ o.barcode }}</code>
        <span class="badge preserve">{{ causes[o.cause] || o.cause }}</span>
        <span class="badge" :class="isActive(o.status) ? 'preserve' : 'ceased'">
          {{ orderStatus[o.status] || o.status }}
        </span>
        <span v-if="o.binding_call_number" class="badge combined"
              title="处置期间装订关系保留">
          册 {{ o.binding_call_number }}
        </span>
        <span class="muted">v{{ o.version }}</span>
      </div>
      <div class="muted" style="margin-top:4px">
        📍 临时位置 {{ o.temporary_location || "（未定）" }}
        ｜ 恢复位置 {{ o.restore_location || "（未定）" }}
        <template v-if="o.condition_assessment">
          ｜ 评估：{{ o.condition_assessment }}
        </template>
      </div>

      <!-- 状态机操作：每个动作携带幂等键与版本，重试/刷新不会重复生效 -->
      <div class="row" style="margin-top:6px" v-if="isActive(o.status)">
        <button v-if="o.status === 'quarantine_pending'" class="tiny"
                @click="sendEvent(o, 'handover')">交接送出</button>
        <button v-if="o.status === 'in_treatment'" class="tiny"
                @click="sendEvent(o, 'complete')">处理完成</button>
        <button v-if="o.status !== 'quarantine_pending'" class="tiny"
                @click="sendEvent(o, 'return')">返还上架</button>
        <button class="tiny ghost" @click="assess(o)">记录评估</button>
        <button class="tiny danger" @click="sendEvent(o, 'discard')">报废</button>
      </div>

      <!-- 事件时间线：含迟到（仅审计）事件 -->
      <div class="event-log">
        <div v-for="e in o.events" :key="e.id" class="event-line"
             :class="{ stale: !e.applied }">
          <span class="etype">{{ eventType[e.event_type] || e.event_type }}</span>
          <span class="muted">{{ fmt(e.created_at) }} · v{{ e.version }}</span>
          <span v-if="!e.applied" class="badge gap">迟到事件，仅审计</span>
          <span v-if="e.location" class="loc">📍 {{ e.location }}</span>
          <span v-if="e.condition_assessment" class="muted">
            评估：{{ e.condition_assessment }}
          </span>
          <span v-if="e.note" class="muted">{{ e.note }}</span>
        </div>
      </div>
    </div>
    <p v-if="orders.length === 0" class="muted">（暂无处理单）</p>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import {
  EVENT_TYPE, ITEM_STATUS, ORDER_STATUS, PRESERVATION_CAUSE,
  isPreservation,
} from "../status.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
  preselectItem: [Number, String],
});
const emit = defineEmits(["changed"]);

const causes = PRESERVATION_CAUSE;
const orderStatus = ORDER_STATUS;
const eventType = EVENT_TYPE;

const orders = ref([]);
const msg = ref(null);
const form = reactive({
  item: null, cause: "water", temporary_location: "",
  restore_location: "", condition_assessment: "",
});
// 幂等键：开单一个键；每个处理单动作各持有一个键，成功后才换新。
// 网络重试/页面刷新重提交同一键，服务端只生效一次。
const createKey = ref(newKey());
const actionKeys = reactive({});

function newKey() {
  return crypto.randomUUID ? crypto.randomUUID()
    : `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 5000);
}
const isActive = (s) => isPreservation(s);

// 可处置实物：在馆或已装订（处理中/报废的不能重复开单）
const candidates = computed(() => {
  const seen = new Set();
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const iss of s.issues) {
      for (const it of iss.items) {
        if (seen.has(it.item_id)) continue;
        seen.add(it.item_id);
        if (it.status === "available" || it.status === "bound") {
          out.push({
            item_pk: it.item_id,
            barcode: it.barcode,
            statusLabel: ITEM_STATUS[it.status] || it.status,
          });
        }
      }
    }
  }
  return out;
});

async function loadOrders() {
  try {
    orders.value = await api.listPreservationOrders(props.titleId);
  } catch (e) { /* 列表非关键路径 */ }
}
watch(() => props.titleId, () => {
  form.item = null;
  loadOrders();
}, { immediate: true });

// 时间轴「送修」按钮预选实物
watch(() => props.preselectItem, (v) => {
  if (v) form.item = Number(v);
});

async function openOrder() {
  try {
    const data = await api.createPreservationOrder({
      item: form.item,
      cause: form.cause,
      temporary_location: form.temporary_location,
      restore_location: form.restore_location,
      condition_assessment: form.condition_assessment,
      idempotency_key: createKey.value,
    });
    createKey.value = newKey(); // 成功后才换下一张单的幂等键
    notify(data.duplicate
      ? "该开单请求已提交过，返回原处理单。"
      : `处理单 #${data.order.id} 已开立，实物进入待隔离。`);
    form.item = null; form.temporary_location = "";
    form.restore_location = ""; form.condition_assessment = "";
    await loadOrders();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function sendEvent(order, type, extra = {}) {
  const k = `${order.id}:${type}`;
  if (!actionKeys[k]) actionKeys[k] = newKey();
  try {
    const data = await api.addPreservationEvent(order.id, {
      event_type: type,
      idempotency_key: actionKeys[k],
      version: order.version + 1,
      ...extra,
    });
    delete actionKeys[k]; // 生效后该动作完成，下次操作用新键
    if (data.duplicate) notify("该事件已提交过，未重复生效。");
    else if (!data.applied) notify("事件版本已过期，仅记入审计，未改变处置。", true);
    else notify(`「${eventType[type]}」已生效。`);
    await loadOrders();
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

function assess(order) {
  const text = window.prompt("录入条件评估：", order.condition_assessment || "");
  if (text == null || text === "") return;
  sendEvent(order, "assess", { condition_assessment: text });
}

function fmt(iso) {
  return iso ? iso.replace("T", " ").slice(0, 16) : "";
}
</script>
