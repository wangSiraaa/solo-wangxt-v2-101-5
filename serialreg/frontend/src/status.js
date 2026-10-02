// 馆藏/编号槽位状态的展示字典。
// not_published / ceased_gap 都是「缺号」——没有发行记录，不自动等同缺藏。
export const HOLDING_STATUS = {
  "issued+held": { label: "已入藏", cls: "ok", hint: "已发行且有在馆实物" },
  "issued+missing": {
    label: "缺藏",
    cls: "missing",
    hint: "已发行但没有可用实物",
  },
  not_published: {
    label: "缺号",
    cls: "gap",
    hint: "没有发行记录，不自动等同缺藏",
  },
  ceased_gap: {
    label: "停刊后缺号",
    cls: "ceased",
    hint: "停刊月份之后，再无发行",
  },
  unregistered: { label: "未登记", cls: "gap", hint: "编号槽位不存在" },
};

export const ITEM_STATUS = {
  available: "在馆",
  checked_out: "借出",
  lost: "丢失",
  bound: "已装订",
  quarantine_pending: "待隔离",
  in_treatment: "处理中",
  treatment_done: "处理完成",
  discarded: "报废",
};

// 保护处理占用的状态：实物暂时不可服务
export const PRESERVATION_STATUSES = [
  "quarantine_pending",
  "in_treatment",
  "treatment_done",
];
export const isPreservation = (s) => PRESERVATION_STATUSES.includes(s);

export const PRESERVATION_CAUSE = {
  water: "受潮",
  pest: "虫害",
  mold: "霉变",
  other: "其他",
};

export const ORDER_STATUS = {
  quarantine_pending: "待隔离",
  in_treatment: "处理中",
  treatment_done: "处理完成",
  returned: "已返还",
  discarded: "已报废",
};

export const EVENT_TYPE = {
  open: "开立处理单",
  handover: "交接送出",
  assess: "条件评估",
  complete: "处理完成",
  return: "返还上架",
  discard: "报废",
};

export const ISSUE_KIND = { regular: "普通期", combined: "两期合刊" };
