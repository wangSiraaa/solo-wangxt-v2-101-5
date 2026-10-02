<template>
  <div class="panel">
    <h2>保护处理（隔离 / 送修 / 返还）</h2>

    <h3>① 开立保护处理单（实体进入待隔离）</h3>
    <div class="row">
      <label class="field"><b>实体</b>
        <select v-model="open.item" style="min-width: 210px">
          <option :value="null">选择在馆/已装订实体</option>
          <option v-for="it in eligibleItems" :key="it.item_id" :value="it.item_id">
            {{ it.barcode }} — {{ it.location || "未排架" }}
            {{ it.bound ? `（装订册 ${it.binding}）` : "" }}
          </option>
        </select>
      </label>
      <label class="field"><b>原因</b>
        <select v-model="open.cause">
          <option v-for="(label, k) in consCause" :key="k" :value="k">{{ label }}</option>
        </select>
      </label>
      <label class="field"><b>临时位置</b>
        <input v-model="open.temporary_location" placeholder="隔离室 Q-1" style="width: 120px" />
      </label>
      <label class="field"><b>恢复位置</b>
        <input v-model="open.restore_location" placeholder="留空＝回原位/册位" style="width: 140px" />
      </label>
      <label class="field"><b>经办人</b>
        <input v-model="open.operator" style="width: 80px" />
      </label>
      <label class="field"><b>情况说明</b>
        <input v-model="open.description" placeholder="书页受潮 / 发现虫蛀…" style="width: 160px" />
      </label>
      <button @click="openOrder" :disabled="!open.item">开立并隔离</button>
    </div>
    <p class="muted">
      处理中的实体不可服务、不能装订、不算可用副本；发行关系与条码保持不变。
      装订册内实体送修不拆订，册关系保留。
    </p>

    <h3>② 进行中的处理单</h3>
    <p v-if="activeOrders.length === 0" class="muted">（当前没有进行中的保护处理单）</p>
    <div v-for="o in activeOrders" :key="o.id" class="order-card">
      <div class="row">
        <strong>处理单 #{{ o.id }}</strong>
        <code>{{ o.item_barcode }}</code>
        <span class="badge treat">{{ consStatus[o.status] }}</span>
        <span class="muted">
          {{ consCause[o.cause] }}<template v-if="o.description">｜{{ o.description }}</template>
        </span>
        <span class="muted">v{{ o.version }}</span>
      </div>
      <div class="muted" style="margin-top:2px">
        临时位置：{{ o.temporary_location || "—" }}
        ｜恢复至：{{ o.restore_location || "—" }}
        <template v-if="o.binding_call_number">
          ｜所属装订册 {{ o.binding_call_number }}（册关系保留，未拆订）
        </template>
      </div>

      <div class="row" style="margin-top:6px">
        <input v-model="forms[o.id].location" placeholder="事件位置（可更新临时位置）"
               style="width: 160px" />
        <input v-model="forms[o.id].condition_note" placeholder="状况评估 / 备注"
               style="width: 150px" />
        <input v-model="forms[o.id].operator" placeholder="经办人" style="width: 80px" />
        <button v-if="o.status === 'quarantine'" class="tiny"
                @click="sendEvent(o, 'handover_out')">送出交接</button>
        <button v-if="o.status === 'in_treatment'" class="tiny"
                @click="sendEvent(o, 'complete')">处理完成</button>
        <button v-if="['in_treatment', 'completed'].includes(o.status)" class="tiny"
                @click="sendEvent(o, 'handover_in')">返还交接（恢复可取）</button>
        <button v-if="o.status === 'quarantine'" class="tiny ghost"
                @click="sendEvent(o, 'handover_in')">解除隔离</button>
        <button class="tiny ghost" @click="sendEvent(o, 'assessment')">记录评估</button>
        <button class="tiny danger" @click="sendEvent(o, 'discard')">报废</button>
      </div>

      <div class="event-list">
        <div v-for="e in o.events" :key="e.id" class="event-row">
          <span class="muted">{{ fmtTime(e.created_at) }}</span>
          <span class="badge" :class="e.kind === 'discard' ? 'missing' : 'treat'">
            {{ consEvent[e.kind] || e.kind }}
          </span>
          <span>{{ e.from_status_label || "—" }} → {{ e.to_status_label }}</span>
          <span v-if="e.location" class="loc">📍{{ e.location }}</span>
          <span v-if="e.condition_note">评估：{{ e.condition_note }}</span>
          <span v-if="e.operator" class="muted">{{ e.operator }}</span>
          <span class="muted">v{{ e.version }}</span>
        </div>
      </div>
    </div>

    <template v-if="closedOrders.length">
      <h3>③ 已办结</h3>
      <div v-for="o in closedOrders" :key="o.id" class="order-card closed">
        <div class="row">
          <strong>处理单 #{{ o.id }}</strong>
          <code>{{ o.item_barcode }}</code>
          <span class="badge" :class="o.status === 'discarded' ? 'missing' : 'ok'">
            {{ consStatus[o.status] }}
          </span>
          <span class="muted">{{ consCause[o.cause] }}｜恢复至 {{ o.restore_location || "—" }}</span>
        </div>
        <div class="event-list">
          <div v-for="e in o.events" :key="e.id" class="event-row">
            <span class="muted">{{ fmtTime(e.created_at) }}</span>
            <span>{{ consEvent[e.kind] || e.kind }}</span>
            <span v-if="e.location" class="loc">📍{{ e.location }}</span>
            <span v-if="e.condition_note">评估：{{ e.condition_note }}</span>
          </div>
        </div>
      </div>
    </template>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import {
  CONSERVATION_CAUSE, CONSERVATION_EVENT, CONSERVATION_STATUS,
} from "../status.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});
const emit = defineEmits(["changed"]);

const consCause = CONSERVATION_CAUSE;
const consStatus = CONSERVATION_STATUS;
const consEvent = CONSERVATION_EVENT;

const orders = ref([]);
const forms = reactive({});
const msg = ref(null);

const open = reactive({
  item: null, cause: "damp", temporary_location: "",
  restore_location: "", operator: "", description: "",
});
// 每个开立/事件动作各持有一个幂等键：网络失败重试复用同一键，不会重复入账
let openKey = newKey();
const pendingKeys = new Map();

function newKey() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    `k-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function notify(text, err = false) {
  msg.value = { text, err };
  setTimeout(() => (msg.value = null), 5000);
}

// 可送修实体：在馆/已装订且没有进行中处理单（合刊实物按期号去重）
const eligibleItems = computed(() => {
  const seen = new Set();
  const out = [];
  for (const s of props.timeline?.slots || []) {
    for (const iss of s.issues) {
      for (const it of iss.items) {
        if (seen.has(it.item_id)) continue;
        seen.add(it.item_id);
        if (it.conservation) continue;
        if (!["available", "bound"].includes(it.status)) continue;
        out.push(it);
      }
    }
  }
  return out;
});

const activeOrders = computed(() => orders.value.filter((o) => o.active));
const closedOrders = computed(() => orders.value.filter((o) => !o.active));

async function loadOrders() {
  if (!props.titleId) return;
  try {
    orders.value = await api.listConservation({ title: props.titleId });
    for (const o of orders.value) {
      if (!forms[o.id]) forms[o.id] = { location: "", condition_note: "", operator: "" };
    }
  } catch (e) { /* 列表非关键路径 */ }
}
watch(() => props.titleId, loadOrders, { immediate: true });

async function openOrder() {
  try {
    const data = await api.openConservation({
      item: open.item, cause: open.cause, description: open.description,
      temporary_location: open.temporary_location,
      restore_location: open.restore_location,
      operator: open.operator, idempotency_key: openKey,
    });
    openKey = newKey(); // 成功后换键
    notify(data.idempotent_replay
      ? `处理单 #${data.id} 已存在（幂等去重，未重复开立）。`
      : `处理单 #${data.id} 已开立，${data.item_barcode} 进入待隔离。`);
    open.item = null; open.temporary_location = "";
    open.restore_location = ""; open.description = "";
    await loadOrders();
    emit("changed");
  } catch (e) {
    if (e.status) openKey = newKey(); // 服务器明确拒绝：换新键
    notify(e.message, true);          // 网络异常：保留键，重试安全
  }
}

async function sendEvent(o, kind) {
  const f = forms[o.id];
  const pk = `${o.id}:${kind}`;
  let key = pendingKeys.get(pk);
  if (!key) {
    key = newKey();
    pendingKeys.set(pk, key);
  }
  try {
    const data = await api.addConservationEvent(o.id, {
      kind, idempotency_key: key, version: o.version,
      location: f.location, condition_note: f.condition_note,
      operator: f.operator,
    });
    pendingKeys.delete(pk);
    notify(data.idempotent_replay
      ? `「${consEvent[kind]}」已记录过（幂等去重，未重复入账）。`
      : `「${consEvent[kind]}」已记录，处理单 #${o.id} → ${consStatus[data.status]}。`);
    forms[o.id] = { location: "", condition_note: "", operator: "" };
    await loadOrders();
    emit("changed");
  } catch (e) {
    if (e.status) {
      // 明确拒绝（如版本过期的迟到事件）：换新键并刷新到最新状态
      pendingKeys.delete(pk);
      notify(e.message, true);
      await loadOrders();
      emit("changed");
    } else {
      // 网络异常：保留幂等键，直接重试不会重复入账
      notify(`网络异常：${e.message}。可直接重试，事件不会重复入账。`, true);
    }
  }
}

function fmtTime(s) {
  return s ? s.slice(0, 16).replace("T", " ") : "";
}
</script>
