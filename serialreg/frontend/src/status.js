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
  quarantine: "待隔离",
  in_treatment: "处理中",
  treatment_done: "处理完成",
  discarded: "报废",
};

// 保护处理单状态（处理完成后还要返还交接才恢复可取）
export const CONSERVATION_STATUS = {
  quarantine: "待隔离",
  in_treatment: "处理中",
  completed: "处理完成",
  closed: "已恢复",
  discarded: "已报废",
};

export const CONSERVATION_CAUSE = {
  damp: "受潮",
  pest: "虫害",
  mold: "霉变",
  damage: "破损",
  other: "其他",
};

export const CONSERVATION_EVENT = {
  open: "开立处理单",
  handover_out: "送出交接",
  assessment: "状况评估",
  complete: "处理完成",
  handover_in: "返还交接",
  discard: "报废",
};

// 实体处于保护处理流程中的馆藏状态（不可服务）
export const IN_CONSERVATION = ["quarantine", "in_treatment", "treatment_done"];

export function itemBadgeClass(status) {
  if (status === "lost" || status === "discarded") return "missing";
  if (IN_CONSERVATION.includes(status)) return "treat";
  return "ok";
}
