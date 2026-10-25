import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  NodeResizer,
  MarkerType,
  ConnectionLineType,
  addEdge,
  reconnectEdge,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";

const FALLBACK_DEFAULTS = {
  completion_manual: "ручная отметка",
  completion_checklist: "чек-лист",
  completion_wait: "ожидание стороны",
  notify_until: "18:00",
  repeat_time: "09:00",
  default_escalation: [
    { interval_type: "первое", delay_minutes: 120, fixed_time: "" },
    { interval_type: "повторное", delay_minutes: 0, fixed_time: "09:00" },
    { interval_type: "частое", delay_minutes: 180, fixed_time: "" },
  ],
};

const STEP_STYLE = {
  padding: 0,
  border: "1px solid #8aa4c8",
  borderRadius: 6,
  background: "#fff",
};

const EDGE_COLOR = "#35557a";
const EDGE_SELECTED_COLOR = "#c45c16";
const EDGE_MARKER = { type: MarkerType.ArrowClosed, width: 18, height: 18, color: EDGE_COLOR };

const EDGE_TYPE_OPTIONS = [
  { value: "default", label: "Гибкая" },
  { value: "smoothstep", label: "Плавная" },
  { value: "step", label: "Ломаная" },
  { value: "straight", label: "Прямая" },
];

const DEFAULT_EDGE_TYPE = "smoothstep";

const CONNECTION_LINE_TYPES = {
  default: ConnectionLineType.Bezier,
  smoothstep: ConnectionLineType.SmoothStep,
  step: ConnectionLineType.Step,
  straight: ConnectionLineType.Straight,
};

function rulesToEscalationSteps(rules, defaults) {
  if (Array.isArray(rules) && rules.length) {
    return rules.map((rule) => {
      if (rule.interval_type === "повторное" || rule.fixed_time) {
        return { kind: "at", minutes: 0, time: rule.fixed_time || defaults.repeat_time || "09:00" };
      }
      return { kind: "after", minutes: Number(rule.delay_minutes) || 120, time: "" };
    });
  }
  return (defaults.default_escalation || []).map((rule) => {
    if (rule.interval_type === "повторное" || rule.fixed_time) {
      return { kind: "at", minutes: 0, time: rule.fixed_time || "09:00" };
    }
    return { kind: "after", minutes: Number(rule.delay_minutes) || 120, time: "" };
  });
}

const DEFAULT_NEW_ESCALATION = [
  { kind: "after", minutes: 120, time: "" },
  { kind: "at", minutes: 0, time: "09:00" },
];

function escalationStepHint(item, index, total) {
  const last = index === total - 1 ? " Этот шаг повторяется, пока процессный шаг не закрыт." : "";
  if (item.kind === "at") {
    return `В это время ближайшего дня, когда уведомлять можно.${last}`;
  }
  if (index === 0) {
    return `Первое напоминание через это время после начала ожидания.${last}`;
  }
  return `Через это время после предыдущего шага.${last}`;
}

function escalationStepsToRules(steps) {
  return (steps || []).map((item, index) => {
    if (item.kind === "at") {
      return { interval_type: "повторное", delay_minutes: 0, fixed_time: item.time || "09:00" };
    }
    return {
      interval_type: index === 0 ? "первое" : "частое",
      delay_minutes: Number(item.minutes) || 180,
      fixed_time: "",
    };
  });
}

function emptyStepData(defaults, title = "Новый шаг") {
  return {
    title,
    completion: defaults.completion_manual,
    checklistText: "",
    escalate: false,
    escalationSteps: [],
    firstHours: 2,
    repeatTime: defaults.repeat_time,
    frequentHours: 3,
    notifyUntil: defaults.notify_until,
    notifyWeekends: false,
    checklistActiveFrom: "",
    manualActiveFrom: "",
    waitSideFrom: "",
    escalationActiveFrom: "",
  };
}

function asDependsList(value) {
  if (!value) {
    return [];
  }
  if (Array.isArray(value)) {
    return value.filter(Boolean).map(String);
  }
  return [String(value)];
}

function withArrow(edge) {
  const selected = Boolean(edge.selected);
  const color = selected ? EDGE_SELECTED_COLOR : EDGE_COLOR;
  return {
    ...edge,
    markerEnd: { ...EDGE_MARKER, color },
    style: {
      ...(edge.style || {}),
      stroke: color,
      strokeWidth: selected ? 3.5 : 2,
    },
  };
}

function makeEdge(source, target, extra = {}) {
  return withArrow({
    id: extra.id || `e-${source}-${target}-${extra.sourceHandle || "out"}-${extra.targetHandle || "in"}`,
    source,
    target,
    sourceHandle: extra.sourceHandle || "out",
    targetHandle: extra.targetHandle || "in",
    type: extra.type || DEFAULT_EDGE_TYPE,
  });
}

function wouldCreateCycle(edges, source, target) {
  const outgoing = new Map();
  edges.forEach((edge) => {
    if (!outgoing.has(edge.source)) {
      outgoing.set(edge.source, []);
    }
    outgoing.get(edge.source).push(edge.target);
  });
  const stack = [target];
  const seen = new Set();
  while (stack.length) {
    const current = stack.pop();
    if (current === source) {
      return true;
    }
    if (seen.has(current)) {
      continue;
    }
    seen.add(current);
    (outgoing.get(current) || []).forEach((next) => stack.push(next));
  }
  return false;
}

function isValidStepConnection(connection, nodes, edges, ignoreEdgeId = null) {
  if (!connection?.source || !connection?.target) {
    return false;
  }
  const sourceNode = nodes.find((node) => node.id === connection.source);
  const targetNode = nodes.find((node) => node.id === connection.target);
  if (sourceNode?.type !== "step" || targetNode?.type !== "step") {
    return false;
  }
  if (connection.source === connection.target) {
    return false;
  }
  const rest = ignoreEdgeId ? edges.filter((edge) => edge.id !== ignoreEdgeId) : edges;
  if (rest.some((edge) => edge.source === connection.source && edge.target === connection.target)) {
    return false;
  }
  return !wouldCreateCycle(rest, connection.source, connection.target);
}

function stepCreatedOrder(node) {
  const raw = Number(node?.data?.createdOrder);
  if (Number.isFinite(raw)) {
    return raw;
  }
  const id = String(node?.id || "");
  const suffix = id.startsWith("s-") ? id.slice(2) : id;
  const stamp = Number(suffix);
  if (Number.isFinite(stamp)) {
    return stamp;
  }
  return 0;
}

function nextCreatedOrder(nodes) {
  return (
    nodes.reduce((max, node) => {
      if (node.type !== "step") {
        return max;
      }
      return Math.max(max, stepCreatedOrder(node));
    }, 0) + 1
  );
}

function stepsByCreated(nodes) {
  return nodes
    .filter((node) => node.type === "step")
    .slice()
    .sort((a, b) => {
      const diff = stepCreatedOrder(a) - stepCreatedOrder(b);
      return diff !== 0 ? diff : String(a.id).localeCompare(String(b.id));
    });
}

function findAutoEdgePair(nodes, edges, selectedIds = []) {
  const all = stepsByCreated(nodes);
  if (all.length < 2) {
    return null;
  }
  const selected = all.filter((node) => selectedIds.includes(node.id));
  const pool = selected.length >= 2 ? selected : all;
  for (let index = pool.length - 2; index >= 0; index -= 1) {
    const source = pool[index].id;
    const target = pool[index + 1].id;
    if (isValidStepConnection({ source, target }, nodes, edges)) {
      return { source, target };
    }
  }
  return null;
}

const BRANCH_TITLE_WIDTH_MIN = 96;
const BRANCH_TITLE_WIDTH_MAX = 560;

function branchTitleWidthForLabel(label) {
  const rows = String(label || "Ветка").split(/\r?\n/);
  const longest = rows.reduce(
    (best, line) => ([...line].length > [...best].length ? line : best),
    ""
  );
  return Math.min(BRANCH_TITLE_WIDTH_MAX, Math.max(BRANCH_TITLE_WIDTH_MIN, 36 + [...longest].length * 8));
}

function clampBranchTitleWidth(width) {
  return Math.min(
    BRANCH_TITLE_WIDTH_MAX,
    Math.max(BRANCH_TITLE_WIDTH_MIN, Math.round(Number(width) || BRANCH_TITLE_WIDTH_MIN))
  );
}

const BRANCH_TITLE_PAD = 4;
const BRANCH_TITLE_VISIBLE = 16;

function clampBranchTitleFieldPos(x, y, fieldWidth, fieldHeight, branchWidth, branchHeight) {
  const fieldW = Math.max(1, Number(fieldWidth) || BRANCH_TITLE_WIDTH_MIN);
  const fieldH = Math.max(1, Number(fieldHeight) || 28);
  const branchW = Math.max(fieldW + BRANCH_TITLE_PAD * 2, Number(branchWidth) || 240);
  const branchH = Math.max(fieldH + BRANCH_TITLE_PAD * 2, Number(branchHeight) || 160);
  const slack = BRANCH_TITLE_VISIBLE;
  const minX = slack - fieldW;
  const maxX = branchW - slack;
  const minY = slack - fieldH;
  const maxY = branchH - slack;
  return {
    x: Math.max(minX, Math.min(maxX, Math.round(Number.isFinite(Number(x)) ? Number(x) : BRANCH_TITLE_PAD))),
    y: Math.max(minY, Math.min(maxY, Math.round(Number.isFinite(Number(y)) ? Number(y) : BRANCH_TITLE_PAD))),
  };
}

function BranchNode({ data, selected }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(data.label || "Ветка");
  const [titleWidth, setTitleWidth] = useState(() =>
    clampBranchTitleWidth(data.labelWidth || branchTitleWidthForLabel(data.label))
  );
  const [titleOffset, setTitleOffset] = useState(() =>
    clampBranchTitleFieldPos(data.labelOffsetX ?? BRANCH_TITLE_PAD, data.labelOffsetY ?? BRANCH_TITLE_PAD, titleWidth, 28, 380, 280)
  );
  const titleOffsetRef = useRef(titleOffset);
  titleOffsetRef.current = titleOffset;
  const titleWrapRef = useRef(null);
  const branchRef = useRef(null);

  const measureTitleField = () => {
    const wrap = titleWrapRef.current;
    const branch = branchRef.current;
    return {
      fieldWidth: wrap?.offsetWidth || titleWidth,
      fieldHeight: wrap?.offsetHeight || 28,
      branchWidth: branch?.clientWidth || 380,
      branchHeight: branch?.clientHeight || 280,
    };
  };

  const snapTitleFieldPos = (x, y) => {
    const box = measureTitleField();
    return clampBranchTitleFieldPos(x, y, box.fieldWidth, box.fieldHeight, box.branchWidth, box.branchHeight);
  };

  useEffect(() => {
    if (!editing) {
      setDraft(data.label || "Ветка");
    }
  }, [data.label, editing]);

  useEffect(() => {
    if (Number.isFinite(Number(data.labelWidth))) {
      setTitleWidth(clampBranchTitleWidth(data.labelWidth));
    }
  }, [data.labelWidth]);

  useLayoutEffect(() => {
    const next = snapTitleFieldPos(
      Number.isFinite(Number(data.labelOffsetX)) ? data.labelOffsetX : BRANCH_TITLE_PAD,
      Number.isFinite(Number(data.labelOffsetY)) ? data.labelOffsetY : BRANCH_TITLE_PAD
    );
    titleOffsetRef.current = next;
    setTitleOffset(next);
  }, [data.labelOffsetX, data.labelOffsetY, data.labelWidth, titleWidth, data.label]);

  const commit = () => {
    const next = (draft || "").replace(/\s+$/u, "").replace(/^\s+/u, "");
    setEditing(false);
    if (!next || !data.onRename) {
      setDraft(data.label || "Ветка");
      return;
    }
    data.onRename(next);
  };

  const startEdit = (event) => {
    if (data.locked) {
      return;
    }
    event.stopPropagation();
    event.preventDefault();
    setDraft(data.label || "Ветка");
    setEditing(true);
  };

  const startTitleResize = (event) => {
    if (data.locked) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startWidth = titleWidth;
    const onMove = (moveEvent) => {
      setTitleWidth(clampBranchTitleWidth(startWidth + (moveEvent.clientX - startX)));
    };
    const onUp = (upEvent) => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      const next = clampBranchTitleWidth(startWidth + (upEvent.clientX - startX));
      setTitleWidth(next);
      data.onTitleWidth?.(next);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const startTitleMove = (event) => {
    if (data.locked || event.button !== 0) {
      return;
    }
    if (event.target.closest?.(".branch-title-resize") || event.target.closest?.("textarea")) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startY = event.clientY;
    const origin = titleOffsetRef.current;
    const box = measureTitleField();
    const onMove = (moveEvent) => {
      const next = clampBranchTitleFieldPos(
        origin.x + (moveEvent.clientX - startX),
        origin.y + (moveEvent.clientY - startY),
        box.fieldWidth,
        box.fieldHeight,
        box.branchWidth,
        box.branchHeight
      );
      titleOffsetRef.current = next;
      setTitleOffset(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      const snapped = snapTitleFieldPos(titleOffsetRef.current.x, titleOffsetRef.current.y);
      titleOffsetRef.current = snapped;
      setTitleOffset(snapped);
      data.onTitleOffset?.(snapped);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  return (
    <div ref={branchRef} className="branch-node">
      <NodeResizer
        minWidth={240}
        minHeight={160}
        isVisible={selected && !data.locked}
        lineClassName="resize-line"
        handleClassName="resize-handle"
      />
      <div
        ref={titleWrapRef}
        className={editing ? "branch-title-wrap is-editing nodrag nopan" : "branch-title-wrap nodrag nopan"}
        style={{ width: titleWidth, left: titleOffset.x, top: titleOffset.y }}
        onPointerDown={startTitleMove}
      >
        {editing ? (
          <textarea
            className="branch-title-input nodrag nopan"
            value={draft}
            rows={Math.max(1, String(draft || "").split(/\r?\n/).length)}
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                commit();
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setDraft(data.label || "Ветка");
                setEditing(false);
              }
            }}
          />
        ) : (
          <div
            className="branch-title"
            title="Перетащите поле, чтобы сместить. Двойной щелчок — переименовать."
            onDoubleClick={startEdit}
          >
            {data.label || "Ветка"}
          </div>
        )}
        {!data.locked && (
          <span
            className="branch-title-resize nodrag nopan"
            title="Потяните, чтобы изменить ширину названия"
            onPointerDown={startTitleResize}
          />
        )}
      </div>
    </div>
  );
}

const PANEL_WIDTH_KEY = "constructor-panel-width";
const PANEL_WIDTH_MIN = 200;
const PANEL_WIDTH_MAX = 520;
const PANEL_WIDTH_DEFAULT = 240;
const CANVAS_MENU_KEY = "constructor-canvas-menu-open";

function clampPanelWidth(width) {
  const room = Math.max(PANEL_WIDTH_MIN, window.innerWidth - 280);
  return Math.min(PANEL_WIDTH_MAX, room, Math.max(PANEL_WIDTH_MIN, Math.round(width)));
}

function readStoredPanelWidth() {
  try {
    const raw = Number(window.localStorage.getItem(PANEL_WIDTH_KEY));
    if (Number.isFinite(raw)) {
      return clampPanelWidth(raw);
    }
  } catch (_error) {
    /* ignore */
  }
  return PANEL_WIDTH_DEFAULT;
}

function readStoredCanvasMenuOpen() {
  try {
    const raw = window.localStorage.getItem(CANVAS_MENU_KEY);
    if (raw === "0") {
      return false;
    }
    if (raw === "1") {
      return true;
    }
  } catch (_error) {
    /* ignore */
  }
  return true;
}

function persistCanvasMenuOpen(open) {
  try {
    window.localStorage.setItem(CANVAS_MENU_KEY, open ? "1" : "0");
  } catch (_error) {
    /* ignore */
  }
}

const RenameContext = createContext(null);

const TipContext = createContext(null);

function StepHandle({ type, id, position, className, tipText }) {
  return (
    <Handle type={type} id={id} position={position} className={className}>
      <span className="step-handle-hotspot nodrag nopan" />
      <span className="step-handle-tip">{tipText}</span>
    </Handle>
  );
}

function StepNode({ data, selected }) {
  const rename = useContext(RenameContext);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(data.title || data.label || "Шаг");
  const press = useRef(null);

  useEffect(() => {
    if (!editing) {
      setDraft(data.title || data.label || "Шаг");
    }
  }, [data.title, data.label, editing]);

  const commit = () => {
    const next = (draft || "").trim();
    setEditing(false);
    const apply = data.onRename || rename;
    if (!next || !apply) {
      setDraft(data.title || data.label || "Шаг");
      data.onEditSize?.(stepBoxForTitle(data.title || data.label || "Шаг"));
      return;
    }
    apply(next);
  };

  const onEditSizeRef = useRef(data.onEditSize);
  onEditSizeRef.current = data.onEditSize;

  useEffect(() => {
    if (!editing || !onEditSizeRef.current) {
      return;
    }
    onEditSizeRef.current(stepBoxForTitle(draft));
  }, [editing, draft]);

  const onPointerDown = (event) => {
    if (data.locked) {
      return;
    }
    press.current = { x: event.clientX, y: event.clientY };
  };

  const onPointerUp = (event) => {
    if (data.locked || !press.current) {
      return;
    }
    const dx = event.clientX - press.current.x;
    const dy = event.clientY - press.current.y;
    press.current = null;
    if (Math.hypot(dx, dy) < 4) {
      event.stopPropagation();
      setDraft(data.title || data.label || "Шаг");
      setEditing(true);
    }
  };

  return (
    <div className="step-node">
      <NodeResizer
        minWidth={72}
        minHeight={48}
        isVisible={selected && !data.locked}
        lineClassName="resize-line"
        handleClassName="resize-handle"
      />
      <StepHandle type="target" id="in" position={Position.Top} className="step-handle step-handle-in" tipText="Сюда можно подключить линию" />
      <StepHandle type="target" id="in-left" position={Position.Left} className="step-handle step-handle-in" tipText="Сюда можно подключить линию" />
      {editing ? (
        <textarea
          className="step-title-input nodrag nopan"
          value={draft}
          rows={1}
          autoFocus
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              commit();
              return;
            }
            if (event.key === "Escape") {
              event.preventDefault();
              setDraft(data.title || data.label || "Шаг");
              setEditing(false);
              data.onEditSize?.(stepBoxForTitle(data.title || data.label || "Шаг"));
            }
          }}
        />
      ) : (
        <div
          className="step-title"
          onPointerDown={onPointerDown}
          onPointerUp={onPointerUp}
        >
          {data.title || data.label || "Шаг"}
        </div>
      )}
      <StepHandle type="source" id="out" position={Position.Bottom} className="step-handle step-handle-out" tipText="Потяните линию к другому шагу" />
      <StepHandle type="source" id="out-right" position={Position.Right} className="step-handle step-handle-out" tipText="Потяните линию к другому шагу" />
    </div>
  );
}

const nodeTypes = { branch: BranchNode, step: StepNode };

function makeBranch(id, x, label = "Основная ветка") {
  return {
    id,
    type: "branch",
    position: { x, y: 40 },
    data: { label },
    style: { width: 380, height: 280, background: "rgba(70, 110, 160, 0.08)" },
    width: 380,
    height: 280,
  };
}

function stepWidthForTitle(title) {
  return stepBoxForTitle(title).width;
}

function stepHeightForTitle(title, width) {
  return stepBoxForTitle(title, width).height;
}

function stepBoxForTitle(title) {
  const rows = String(title || "Шаг").split(/\r?\n/);
  const longest = rows.reduce(
    (best, line) => ([...line].length > [...best].length ? line : best),
    ""
  );
  const width = Math.min(640, Math.max(140, 52 + [...longest].length * 11));
  const height = Math.max(56, 18 + Math.max(rows.length, 1) * 22);
  return { width, height };
}

function makeStep(id, parentId, y, data, x = 24) {
  const title = data?.title || data?.label;
  const { width, height } = stepBoxForTitle(title);
  return {
    id,
    type: "step",
    parentId,
    extent: "parent",
    position: { x, y },
    data,
    style: { ...STEP_STYLE, width, height },
    width,
    height,
  };
}

function stepPositionInParent(parent, flowPos) {
  if (!parent) {
    return { x: 24, y: 48 };
  }
  const width = Number(parent.measured?.width || parent.width || parent.style?.width || 380);
  const height = Number(parent.measured?.height || parent.height || parent.style?.height || 280);
  const originX = parent.positionAbsolute?.x ?? parent.position?.x ?? 0;
  const originY = parent.positionAbsolute?.y ?? parent.position?.y ?? 0;
  let x = 24;
  let y = 48;
  if (flowPos) {
    x = flowPos.x - originX - 40;
    y = flowPos.y - originY - 16;
  }
  return {
    x: Math.max(12, Math.min(x, width - 312)),
    y: Math.max(40, Math.min(y, height - 68)),
  };
}

function specToFlow(spec) {
  const nodes = [];
  const edges = [];
  const branches = spec.branches || [];
  const defaultType = spec.default_edge_type || DEFAULT_EDGE_TYPE;
  let createdSeq = 0;
  branches.forEach((branch, bIndex) => {
    const branchId = `b-${branch.key || bIndex}`;
    const height = Math.max(220, 80 + (branch.steps || []).length * 90);
    nodes.push({
      ...makeBranch(branchId, 40 + bIndex * 420, branch.title || `Ветка ${bIndex + 1}`),
      style: { width: 380, height, background: "rgba(70, 110, 160, 0.08)" },
      width: 380,
      height,
    });
    (branch.steps || []).forEach((step, sIndex) => {
      const first = (step.escalation || []).find((r) => r.interval_type === "первое");
      const repeat = (step.escalation || []).find((r) => r.interval_type === "повторное");
      const frequent = (step.escalation || []).find((r) => r.interval_type === "частое");
      nodes.push(
        makeStep(`s-${step.key || `${bIndex}-${sIndex}`}`, branchId, 48 + sIndex * 86, {
          title: step.title || "Шаг",
          completion: step.completion_type || FALLBACK_DEFAULTS.completion_manual,
          checklistText: (step.checklist || []).join("\n"),
          escalate: Boolean((step.escalation || []).length),
          escalationSteps: rulesToEscalationSteps(step.escalation || [], FALLBACK_DEFAULTS),
          firstHours: Math.max(1, Math.round((first?.delay_minutes || 120) / 60)),
          repeatTime: repeat?.fixed_time || "09:00",
          frequentHours: Math.max(1, Math.round((frequent?.delay_minutes || 180) / 60)),
          notifyUntil: step.notify_until || "18:00",
          notifyWeekends: Boolean(step.notify_weekends),
          checklistActiveFrom: step.checklist_active_from || "",
          manualActiveFrom: step.manual_active_from || "",
          waitSideFrom: step.wait_side_from || "",
          escalationActiveFrom: step.escalation_active_from || "",
          createdOrder: ++createdSeq,
        })
      );
    });
  });
  if ((spec.edges || []).length) {
    spec.edges.forEach((edge, index) => {
      const source = String(edge.source || "").startsWith("s-") ? edge.source : `s-${edge.source}`;
      const target = String(edge.target || "").startsWith("s-") ? edge.target : `s-${edge.target}`;
      edges.push(
        makeEdge(source, target, {
          id: edge.id || `e-${source}-${target}-${index}`,
          type: edge.type || defaultType,
          sourceHandle: edge.sourceHandle || "out",
          targetHandle: edge.targetHandle || "in",
        })
      );
    });
  } else {
    branches.forEach((branch) => {
      (branch.steps || []).forEach((step) => {
        asDependsList(step.depends_on).forEach((dep) => {
          edges.push(makeEdge(`s-${dep}`, `s-${step.key}`, { type: defaultType }));
        });
      });
    });
  }
  return { nodes, edges };
}

function flowToSpec(nodes, edges, name, defaults, defaultEdgeType) {
  const groups = nodes.filter((node) => node.type === "branch" || node.type === "group");
  const branches = (groups.length ? groups : [{ id: "b-main", data: { label: "Основная ветка" } }]).map(
    (group, index) => {
      const steps = nodes
        .filter((node) => node.type === "step" && (node.parentId || "b-main") === group.id)
        .sort((a, b) => (a.position?.y || 0) - (b.position?.y || 0))
        .map((node, stepIndex) => {
          const key = node.id.replace(/^s-/, "") || `s${index}_${stepIndex}`;
          const incoming = [];
          edges.forEach((edge) => {
            if (edge.target === node.id) {
              const dep = String(edge.source || "").replace(/^s-/, "");
              if (dep && !incoming.includes(dep)) {
                incoming.push(dep);
              }
            }
          });
          const escalate = Boolean(node.data?.escalate);
          const checklist = (node.data?.checklistText || "")
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean);
          const completion =
            node.data?.completion ||
            (checklist.length ? defaults.completion_checklist : defaults.completion_manual);
          return {
            key,
            title: node.data?.title || `Шаг ${stepIndex + 1}`,
            completion_type: completion,
            action_kind: completion === defaults.completion_checklist ? "checklist" : "",
            depends_on: incoming,
            checklist,
            notify_until: node.data?.notifyUntil || defaults.notify_until,
            notify_weekends: Boolean(node.data?.notifyWeekends),
            checklist_active_from: node.data?.checklistActiveFrom || "",
            manual_active_from: node.data?.manualActiveFrom || "",
            wait_side_from: node.data?.waitSideFrom || "",
            escalation_active_from: node.data?.escalationActiveFrom || "",
            escalation: escalate
              ? escalationStepsToRules(
                  node.data?.escalationSteps?.length
                    ? node.data.escalationSteps
                    : rulesToEscalationSteps(
                        [
                          {
                            interval_type: "первое",
                            delay_minutes: Number(node.data.firstHours || 2) * 60,
                            fixed_time: "",
                          },
                          {
                            interval_type: "повторное",
                            delay_minutes: 0,
                            fixed_time: node.data.repeatTime || defaults.repeat_time,
                          },
                          {
                            interval_type: "частое",
                            delay_minutes: Number(node.data.frequentHours || 3) * 60,
                            fixed_time: "",
                          },
                        ],
                        defaults
                      )
                )
              : [],
          };
        });
      return {
        key: group.id.replace(/^b-/, "") || `b${index}`,
        title: group.data?.label || `Ветка ${index + 1}`,
        deferred: false,
        steps,
      };
    }
  );
  return {
    default_title: name,
    default_edge_type: defaultEdgeType || DEFAULT_EDGE_TYPE,
    edges: edges.map((edge) => ({
      id: edge.id,
      source: String(edge.source || "").replace(/^s-/, ""),
      target: String(edge.target || "").replace(/^s-/, ""),
      type: edge.type || defaultEdgeType || DEFAULT_EDGE_TYPE,
      sourceHandle: edge.sourceHandle || "out",
      targetHandle: edge.targetHandle || "in",
    })),
    branches,
  };
}

async function callApi(method, ...args) {
  const api = window.pywebview?.api;
  if (!api || typeof api[method] !== "function") {
    return null;
  }
  return api[method](...args);
}

function blankCanvas(defaults) {
  return {
    nodes: [
      makeBranch("b-main", 60),
      makeStep("s-s1", "b-main", 48, { ...emptyStepData(defaults, "Первый шаг"), createdOrder: 1 }),
    ],
    edges: [],
  };
}

function nodeTitle(nodes, nodeId) {
  const node = nodes.find((item) => item.id === nodeId);
  return node?.data?.title || node?.data?.label || nodeId;
}

function branchLabelOf(nodes, nodeId) {
  const node = nodes.find((item) => item.id === nodeId);
  if (!node) {
    return "";
  }
  if (node.type === "branch" || node.type === "group") {
    return String(node.data?.label || "").trim();
  }
  if (!node.parentId) {
    return "";
  }
  const parent = nodes.find((item) => item.id === node.parentId);
  return String(parent?.data?.label || "").trim();
}

function cloneNode(node) {
  return {
    ...node,
    data: { ...node.data },
    position: { ...(node.position || { x: 0, y: 0 }) },
    style: node.style ? { ...node.style } : undefined,
  };
}

function expandSelectedNodeIds(nodes, selectedIds) {
  const ids = new Set(selectedIds);
  nodes.forEach((node) => {
    if ((node.type === "branch" || node.type === "group") && ids.has(node.id)) {
      nodes.forEach((child) => {
        if (child.parentId === node.id) {
          ids.add(child.id);
        }
      });
    }
  });
  return ids;
}

function snapshotSelection(nodes, edges, selectedIds, selectedEdgeIds) {
  const nodeIds = expandSelectedNodeIds(nodes, selectedIds);
  const copiedNodes = nodes.filter((node) => nodeIds.has(node.id)).map(cloneNode);
  const copiedIdSet = new Set(copiedNodes.map((node) => node.id));
  const copiedEdges = edges
    .filter((edge) => copiedIdSet.has(edge.source) && copiedIdSet.has(edge.target))
    .map((edge) => ({ ...edge }));
  if (copiedNodes.length) {
    return { kind: "graph", nodes: copiedNodes, edges: copiedEdges };
  }
  const edge = edges.find((item) => selectedEdgeIds.includes(item.id));
  if (edge) {
    return {
      kind: "edge",
      edge: {
        type: edge.type || "default",
        sourceHandle: edge.sourceHandle || "out",
        targetHandle: edge.targetHandle || "in",
      },
    };
  }
  return null;
}

function clipboardHasBranch(clip) {
  return Boolean(
    clip &&
      clip.kind === "graph" &&
      (clip.nodes || []).some((node) => node.type === "branch" || node.type === "group")
  );
}

function remapPastedGraph(clip, nodes, selected, flowPos) {
  let stamp = Date.now();
  const nextId = (type) => {
    stamp += 1;
    return type === "step" ? `s-${stamp}` : `b-${stamp}`;
  };
  const idMap = {};
  clip.nodes.forEach((node) => {
    idMap[node.id] = nextId(node.type === "step" ? "step" : "branch");
  });
  const hasBranch = clip.nodes.some((node) => node.type === "branch" || node.type === "group");
  const firstBranch = clip.nodes.find((node) => node.type === "branch" || node.type === "group");
  const origin = firstBranch?.position || { x: 0, y: 0 };
  const dropAt =
    flowPos && Number.isFinite(flowPos.x) && Number.isFinite(flowPos.y) ? flowPos : null;
  const branches = nodes.filter((node) => node.type === "branch" || node.type === "group");
  const dropParent =
    (selected?.type === "branch" || selected?.type === "group"
      ? selected
      : nodes.find((node) => node.id === selected?.parentId)) || branches[0];
  const pastedNodes = clip.nodes.map((node) => {
    const copy = cloneNode(node);
    copy.id = idMap[node.id];
    copy.selected = false;
    if (copy.type === "step") {
      const newParent = hasBranch ? idMap[node.parentId] : dropParent?.id;
      copy.parentId = newParent;
      copy.extent = "parent";
      copy.position = {
        x: (node.position?.x || 24) + (hasBranch ? 0 : 16),
        y: (node.position?.y || 48) + 24,
      };
    } else if (dropAt && firstBranch) {
      copy.position = {
        x: dropAt.x + ((node.position?.x || 0) - (origin.x || 0)),
        y: dropAt.y + ((node.position?.y || 0) - (origin.y || 0)),
      };
    } else {
      copy.position = {
        x: (node.position?.x || 40) + 60,
        y: (node.position?.y || 40) + 24,
      };
    }
    return copy;
  });
  const startOrder = nextCreatedOrder(nodes);
  const pastedSteps = pastedNodes
    .filter((node) => node.type === "step")
    .slice()
    .sort((a, b) => {
      const diff = stepCreatedOrder(a) - stepCreatedOrder(b);
      return diff !== 0 ? diff : String(a.id).localeCompare(String(b.id));
    });
  pastedSteps.forEach((node, index) => {
    node.data = { ...node.data, createdOrder: startOrder + index };
  });
  const pastedEdges = clip.edges
    .filter((edge) => idMap[edge.source] && idMap[edge.target])
    .map((edge) =>
      makeEdge(idMap[edge.source], idMap[edge.target], {
        type: edge.type,
        sourceHandle: edge.sourceHandle,
        targetHandle: edge.targetHandle,
      })
    );
  return { nodes: pastedNodes, edges: pastedEdges };
}

function TipProvider({ children }) {
  const [tip, setTip] = useState(null);

  const show = useCallback((text, rect) => {
    const maxWidth = 260;
    let left = rect.left;
    let top = rect.bottom + 6;
    if (left + maxWidth > window.innerWidth - 8) {
      left = Math.max(8, window.innerWidth - maxWidth - 8);
    }
    if (left < 8) {
      left = 8;
    }
    if (top > window.innerHeight - 80) {
      top = Math.max(8, rect.top - 72);
    }
    setTip({ text, left, top });
  }, []);

  const hide = useCallback(() => {
    setTip(null);
  }, []);

  useEffect(() => {
    const hideNow = () => setTip(null);
    window.addEventListener("pointerdown", hideNow, true);
    window.addEventListener("wheel", hideNow, { capture: true, passive: true });
    window.addEventListener("keydown", hideNow, true);
    window.addEventListener("blur", hideNow);
    return () => {
      window.removeEventListener("pointerdown", hideNow, true);
      window.removeEventListener("wheel", hideNow, true);
      window.removeEventListener("keydown", hideNow, true);
      window.removeEventListener("blur", hideNow);
    };
  }, []);

  return (
    <TipContext.Provider value={{ show, hide }}>
      {children}
      {tip && (
        <div className="tip-bubble" style={{ left: tip.left, top: tip.top }}>
          {tip.text}
        </div>
      )}
    </TipContext.Provider>
  );
}

function Tip({ text, children }) {
  const ctx = useContext(TipContext);
  useEffect(() => () => ctx?.hide(), [ctx]);
  const onEnter = (event) => {
    if (!ctx || !text) {
      return;
    }
    ctx.show(text, event.currentTarget.getBoundingClientRect());
  };
  return (
    <span
      className="tip"
      onMouseEnter={onEnter}
      onMouseLeave={() => ctx?.hide()}
      onPointerLeave={() => ctx?.hide()}
    >
      {children}
    </span>
  );
}

export default function App() {
  const [defaults, setDefaults] = useState(FALLBACK_DEFAULTS);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState(null);
  const [selectedIds, setSelectedIds] = useState([]);
  const [selectedEdgeIds, setSelectedEdgeIds] = useState([]);
  const [savedTemplateId, setSavedTemplateId] = useState(null);
  const [menu, setMenu] = useState(null);
  const [showTemplatePanel, setShowTemplatePanel] = useState(false);
  const [showEditPanel, setShowEditPanel] = useState(false);
  const [defaultEdgeType, setDefaultEdgeType] = useState(DEFAULT_EDGE_TYPE);
  const [templateName, setTemplateName] = useState("Новый процесс");
  const [readonly, setReadonly] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [builtins, setBuiltins] = useState([]);
  const [saved, setSaved] = useState([]);
  const [confirm, setConfirm] = useState(null);
  const [openBuiltin, setOpenBuiltin] = useState("");
  const [openSaved, setOpenSaved] = useState("");
  const edgeReconnectSuccessful = useRef(true);
  const clipboardRef = useRef(null);
  const menuRef = useRef(null);
  const flowRef = useRef(null);
  const selectedEdgeIdsRef = useRef([]);
  const suppressEdgeRfSelect = useRef(false);
  const [clipboard, setClipboard] = useState(null);
  const [panelWidth, setPanelWidth] = useState(PANEL_WIDTH_DEFAULT);
  const [canvasMenuOpen, setCanvasMenuOpen] = useState(true);

  const setCanvasMenuVisible = (open) => {
    setCanvasMenuOpen(open);
    persistCanvasMenuOpen(open);
  };

  useEffect(() => {
    setPanelWidth(readStoredPanelWidth());
    setCanvasMenuOpen(readStoredCanvasMenuOpen());
    const onResize = () => setPanelWidth((current) => clampPanelWidth(current));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const startPanelResize = (event) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panelWidth;
    const onMove = (moveEvent) => {
      setPanelWidth(clampPanelWidth(startWidth + (startX - moveEvent.clientX)));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      setPanelWidth((current) => {
        try {
          window.localStorage.setItem(PANEL_WIDTH_KEY, String(current));
        } catch (_error) {
          /* ignore */
        }
        return current;
      });
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const selected = useMemo(
    () => nodes.find((node) => node.id === selectedId) || null,
    [nodes, selectedId]
  );
  const selectedEdge = useMemo(
    () => edges.find((edge) => edge.id === selectedEdgeId) || null,
    [edges, selectedEdgeId]
  );

  const displayNodes = useMemo(
    () =>
      nodes.map((node) => ({
        ...node,
        data: {
          ...node.data,
          locked: readonly,
          onRename:
            readonly || (node.type !== "step" && node.type !== "branch" && node.type !== "group")
              ? undefined
              : (name) => {
                  setNodes((current) =>
                    current.map((item) =>
                      item.id === node.id
                        ? {
                            ...item,
                            data:
                              item.type === "step"
                                ? { ...item.data, title: name }
                                : { ...item.data, label: name },
                            ...(item.type === "step"
                              ? {
                                  ...stepBoxForTitle(name),
                                  style: {
                                    ...(item.style || STEP_STYLE),
                                    ...stepBoxForTitle(name),
                                  },
                                }
                              : {}),
                          }
                        : item
                    )
                  );
                  setDirty(true);
                },
          onTitleWidth:
            readonly || (node.type !== "branch" && node.type !== "group")
              ? undefined
              : (width) => {
                  setNodes((current) =>
                    current.map((item) =>
                      item.id === node.id
                        ? { ...item, data: { ...item.data, labelWidth: width } }
                        : item
                    )
                  );
                  setDirty(true);
                },
          onTitleOffset:
            readonly || (node.type !== "branch" && node.type !== "group")
              ? undefined
              : ({ x, y }) => {
                  setNodes((current) =>
                    current.map((item) =>
                      item.id === node.id
                        ? { ...item, data: { ...item.data, labelOffsetX: x, labelOffsetY: y } }
                        : item
                    )
                  );
                  setDirty(true);
                },
          onEditSize:
            readonly || node.type !== "step"
              ? undefined
              : ({ width, height }) => {
                  setNodes((current) =>
                    current.map((item) =>
                      item.id === node.id
                        ? {
                            ...item,
                            width,
                            height,
                            style: {
                              ...(item.style || STEP_STYLE),
                              width,
                              height,
                            },
                          }
                        : item
                    )
                  );
                },
        },
      })),
    [nodes, readonly, setNodes]
  );

  const displayEdges = useMemo(
    () =>
      edges.map((edge) => {
        const selected = Boolean(edge.selected) || selectedEdgeIds.includes(edge.id);
        return withArrow({ ...edge, selected });
      }),
    [edges, selectedEdgeIds]
  );

  const markDirty = useCallback(() => {
    if (!readonly) {
      setDirty(true);
    }
  }, [readonly]);

  const handleNodesChange = useCallback(
    (changes) => {
      onNodesChange(changes);
      const edited = changes.some(
        (item) =>
          item.type === "position" ||
          item.type === "remove" ||
          item.type === "add" ||
          item.type === "replace" ||
          item.resizing
      );
      if (edited) {
        markDirty();
      }
    },
    [onNodesChange, markDirty]
  );

  const handleEdgesChange = useCallback(
    (changes) => {
      const next = suppressEdgeRfSelect.current
        ? changes.filter((item) => item.type !== "select")
        : changes;
      if (!next.length) {
        return;
      }
      onEdgesChange(next);
      if (next.some((item) => item.type !== "select")) {
        markDirty();
      }
    },
    [onEdgesChange, markDirty]
  );

  useEffect(() => {
    let cancelled = false;
    const boot = async () => {
      const loaded = (await callApi("defaults")) || FALLBACK_DEFAULTS;
      const builtinList = (await callApi("list_builtins")) || [];
      const userList = (await callApi("list_templates")) || [];
      if (cancelled) {
        return;
      }
      setDefaults(loaded);
      setBuiltins(builtinList);
      setSaved(userList);
      const blank = blankCanvas(loaded);
      setNodes(blank.nodes);
      setEdges(blank.edges);
      setDirty(false);
    };
    const timer = setTimeout(boot, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [setNodes, setEdges]);

  const isValidConnection = useCallback(
    (connection) => isValidStepConnection(connection, nodes, edges),
    [nodes, edges]
  );

  const onConnect = useCallback(
    (connection) => {
      if (readonly || !isValidStepConnection(connection, nodes, edges)) {
        return;
      }
      setEdges((current) =>
        addEdge(
          withArrow({
            ...connection,
            type: defaultEdgeType,
            animated: false,
          }),
          current
        )
      );
      setDirty(true);
    },
    [readonly, nodes, edges, defaultEdgeType, setEdges]
  );

  const onReconnectStart = useCallback(() => {
    edgeReconnectSuccessful.current = false;
  }, []);

  const onReconnect = useCallback(
    (oldEdge, newConnection) => {
      if (readonly || !isValidStepConnection(newConnection, nodes, edges, oldEdge.id)) {
        return;
      }
      edgeReconnectSuccessful.current = true;
      setEdges((current) =>
        reconnectEdge(oldEdge, withArrow({ ...newConnection, type: oldEdge.type || defaultEdgeType }), current)
      );
      setDirty(true);
    },
    [readonly, nodes, edges, defaultEdgeType, setEdges]
  );

  const onReconnectEnd = useCallback(
    (_event, edge) => {
      if (!readonly && !edgeReconnectSuccessful.current) {
        setEdges((current) => current.filter((item) => item.id !== edge.id));
        setSelectedEdgeId(null);
        setDirty(true);
      }
      edgeReconnectSuccessful.current = true;
    },
    [readonly, setEdges]
  );

  const changeEdgeType = (value) => {
    if (readonly || !value) {
      return;
    }
    if (selectedEdgeIds.length) {
      const ids = new Set(selectedEdgeIds);
      setEdges((current) =>
        current.map((edge) => (ids.has(edge.id) ? withArrow({ ...edge, type: value }) : edge))
      );
      setDirty(true);
      return;
    }
    setDefaultEdgeType(value);
  };

  const deleteSelectedEdge = () => {
    if (readonly || !selectedEdgeIds.length) {
      return;
    }
    const ids = new Set(selectedEdgeIds);
    setEdges((current) => current.filter((edge) => !ids.has(edge.id)));
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setDirty(true);
  };

  const autoEdgePair = useMemo(
    () => findAutoEdgePair(nodes, edges, selectedIds),
    [nodes, edges, selectedIds]
  );

  const addCanvasEdge = () => {
    if (readonly || !autoEdgePair) {
      return;
    }
    setEdges((current) => [
      ...current,
      makeEdge(autoEdgePair.source, autoEdgePair.target, { type: defaultEdgeType }),
    ]);
    setDirty(true);
  };

  const closeMenu = () => setMenu(null);

  useEffect(() => {
    if (!menu) {
      return undefined;
    }
    const onPointerDown = (event) => {
      if (menuRef.current && menuRef.current.contains(event.target)) {
        return;
      }
      setMenu(null);
    };
    const onBlur = () => setMenu(null);
    const onKey = (event) => {
      if (event.key === "Escape") {
        setMenu(null);
      }
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("blur", onBlur);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [menu]);

  const closeOverlays = () => {
    setMenu(null);
    setShowTemplatePanel(false);
    setShowEditPanel(false);
  };

  const applyEdgeSelection = (ids) => {
    const unique = [...new Set(ids)];
    const idSet = new Set(unique);
    selectedEdgeIdsRef.current = unique;
    setSelectedEdgeIds(unique);
    setSelectedEdgeId(unique[unique.length - 1] || null);
    setEdges((current) =>
      current.map((edge) => ({ ...edge, selected: idSet.has(edge.id) }))
    );
  };

  const toggleEdgeActivation = (edgeId, additive = false) => {
    const current = selectedEdgeIdsRef.current;
    if (additive) {
      applyEdgeSelection(
        current.includes(edgeId) ? current.filter((id) => id !== edgeId) : [...current, edgeId]
      );
      return;
    }
    if (current.includes(edgeId)) {
      applyEdgeSelection(current.filter((id) => id !== edgeId));
      return;
    }
    applyEdgeSelection([edgeId]);
  };

  const edgeIdsForMenu = () => {
    if (menu?.kind === "edge" && menu.edgeId) {
      return selectedEdgeIds.includes(menu.edgeId) ? selectedEdgeIds : [menu.edgeId];
    }
    return selectedEdgeIds;
  };

  useEffect(() => {
    selectedEdgeIdsRef.current = selectedEdgeIds;
  }, [selectedEdgeIds]);

  const copySelection = useCallback(() => {
    if (readonly) {
      return;
    }
    const nodeIds = menu?.kind === "edge" ? [] : selectedIds;
    const clip = snapshotSelection(nodes, edges, nodeIds, edgeIdsForMenu());
    if (!clip) {
      return;
    }
    clipboardRef.current = clip;
    setClipboard(clip);
    setStatus(
      clip.kind === "edge"
        ? "Связь скопирована."
        : clipboardHasBranch(clip)
          ? "Ветка скопирована."
          : "Элементы скопированы."
    );
    closeMenu();
  }, [readonly, nodes, edges, selectedIds, selectedEdgeIds, menu]);

  const deleteSelection = useCallback(() => {
    if (readonly) {
      return;
    }
    const menuEdgeIds =
      menu?.kind === "edge" && menu.edgeId
        ? selectedEdgeIds.includes(menu.edgeId)
          ? selectedEdgeIds
          : [menu.edgeId]
        : selectedEdgeIds;
    if (menu?.kind === "edge") {
      if (!menuEdgeIds.length) {
        return;
      }
      const edgeIds = new Set(menuEdgeIds);
      setEdges((current) => current.filter((edge) => !edgeIds.has(edge.id)));
      applyEdgeSelection(selectedEdgeIdsRef.current.filter((id) => !edgeIds.has(id)));
      setDirty(true);
      setError("");
      closeMenu();
      return;
    }
    if (!selectedIds.length && !selectedEdgeIds.length) {
      return;
    }
    const nodeIds = expandSelectedNodeIds(nodes, selectedIds);
    const branches = nodes.filter((node) => node.type === "branch" || node.type === "group");
    const remainingBranches = branches.filter((node) => !nodeIds.has(node.id));
    if (!remainingBranches.length && branches.some((node) => nodeIds.has(node.id))) {
      setError("Нужна хотя бы одна ветка.");
      return;
    }
    const edgeIds = new Set(selectedEdgeIds);
    setNodes((current) => current.filter((node) => !nodeIds.has(node.id)));
    setEdges((current) =>
      current.filter(
        (edge) => !nodeIds.has(edge.source) && !nodeIds.has(edge.target) && !edgeIds.has(edge.id)
      )
    );
    setSelectedId(null);
    setSelectedIds([]);
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    selectedEdgeIdsRef.current = [];
    setDirty(true);
    setError("");
    closeMenu();
  }, [readonly, nodes, selectedIds, selectedEdgeIds, menu, setNodes, setEdges]);

  const pasteSelection = useCallback((flowPos) => {
    if (readonly) {
      return;
    }
    const clip = clipboardRef.current || clipboard;
    if (!clip) {
      return;
    }
    const dropAt =
      flowPos && Number.isFinite(flowPos.x) && Number.isFinite(flowPos.y) ? flowPos : null;
    if (clip.kind === "edge") {
      const selectedSteps = nodes.filter((node) => selectedIds.includes(node.id) && node.type === "step");
      if (selectedSteps.length >= 2) {
        const connection = {
          source: selectedSteps[0].id,
          target: selectedSteps[1].id,
          sourceHandle: clip.edge.sourceHandle,
          targetHandle: clip.edge.targetHandle,
        };
        if (isValidStepConnection(connection, nodes, edges)) {
          setEdges((current) =>
            addEdge(withArrow({ ...connection, type: clip.edge.type, animated: false }), current)
          );
          setDirty(true);
          closeMenu();
          return;
        }
      }
      if (selectedEdge) {
        setEdges((current) =>
          current.map((edge) =>
            edge.id === selectedEdge.id ? withArrow({ ...edge, type: clip.edge.type }) : edge
          )
        );
        setDirty(true);
        closeMenu();
        return;
      }
      setDefaultEdgeType(clip.edge.type || DEFAULT_EDGE_TYPE);
      setStatus("Тип линии скопирован. Выделите два шага и вставьте связь.");
      closeMenu();
      return;
    }
    const pasted = remapPastedGraph(clip, nodes, selected, dropAt);
    if (!pasted.nodes.length) {
      return;
    }
    setNodes((current) => [...current, ...pasted.nodes]);
    setEdges((current) => [...current, ...pasted.edges]);
    setSelectedIds(pasted.nodes.map((node) => node.id));
    setSelectedId(pasted.nodes[0]?.id || null);
    setDirty(true);
    closeMenu();
  }, [readonly, nodes, edges, selectedIds, selected, selectedEdge, clipboard, setNodes, setEdges]);

  const addBranch = () => {
    if (readonly) {
      return;
    }
    const id = `b-${Date.now()}`;
    setNodes((current) => [
      ...current,
      makeBranch(id, 40 + current.filter((n) => n.type === "branch" || n.type === "group").length * 420, "Новая ветка"),
    ]);
    setDirty(true);
  };

  const addStep = (parentId, flowPos) => {
    if (readonly) {
      return;
    }
    const groups = nodes.filter((node) => node.type === "branch" || node.type === "group");
    const parent =
      (parentId && groups.find((node) => node.id === parentId)) ||
      (selected?.type === "branch" || selected?.type === "group" ? selected : null) ||
      groups[0] ||
      null;
    if (!parent) {
      addBranch();
      return;
    }
    const siblings = nodes.filter((node) => node.parentId === parent.id);
    const spot = flowPos
      ? stepPositionInParent(parent, flowPos)
      : { x: 24, y: 48 + siblings.length * 86 };
    const id = `s-${Date.now()}`;
    setNodes((current) => [
      ...current,
      makeStep(id, parent.id, spot.y, { ...emptyStepData(defaults), createdOrder: nextCreatedOrder(nodes) }, spot.x),
    ]);
    setSelectedId(id);
    setSelectedIds([id]);
    setDirty(true);
  };

  const patchSelected = (patch) => {
    if (!selected || readonly) {
      return;
    }
    const extra = {};
    if (patch.escalate && !(selected.data?.escalationSteps || []).length) {
      extra.escalationSteps = DEFAULT_NEW_ESCALATION.map((item) => ({ ...item }));
    }
    setNodes((current) =>
      current.map((node) => {
        if (node.id !== selected.id) {
          return node;
        }
        const next = { ...node, data: { ...node.data, ...patch, ...extra } };
        if (node.type === "step" && Object.prototype.hasOwnProperty.call(patch, "title")) {
          const box = stepBoxForTitle(patch.title);
          next.width = box.width;
          next.height = box.height;
          next.style = { ...(node.style || STEP_STYLE), ...box };
        }
        return next;
      })
    );
    setDirty(true);
  };

  const applyBlank = () => {
    const blank = blankCanvas(defaults);
    setReadonly(false);
    setTemplateName("Новый процесс");
    setStatus("");
    setError("");
    setDirty(false);
    setOpenBuiltin("");
    setOpenSaved("");
    setSavedTemplateId(null);
    setSelectedId(null);
    setSelectedIds([]);
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setDefaultEdgeType(DEFAULT_EDGE_TYPE);
    setEdges(blank.edges);
    setNodes(blank.nodes);
  };

  const deleteCurrentTemplate = async () => {
    if (!savedTemplateId || readonly) {
      setError("Выберите сохранённый шаблон. Встроенный удалить нельзя.");
      return;
    }
    if (
      !window.confirm(
        `Удалить шаблон «${templateName}» без возможности восстановления?\nЗапущенные процессы по нему останутся.`
      )
    ) {
      return;
    }
    setError("");
    const result = await callApi("delete_template", savedTemplateId);
    if (!result?.ok) {
      setError("Не удалось удалить шаблон.");
      return;
    }
    const userList = (await callApi("list_templates")) || [];
    setSaved(userList);
    applyBlank();
    setStatus("Шаблон удалён.");
  };

  const save = async (asNew = false) => {
    setError("");
    const spec = flowToSpec(nodes, edges, templateName, defaults, defaultEdgeType);
    const currentId = !asNew && savedTemplateId && !readonly ? savedTemplateId : null;
    const result = await callApi("save_template", templateName, spec, currentId);
    if (!result) {
      throw new Error("Мост pywebview недоступен. Откройте конструктор из приложения.");
    }
    setSavedTemplateId(result.id);
    setOpenSaved(String(result.id));
    setOpenBuiltin("");
    setStatus(currentId ? `Шаблон обновлён (id ${result.id}).` : `Шаблон сохранён (id ${result.id}).`);
    setReadonly(false);
    setDirty(false);
    const userList = (await callApi("list_templates")) || [];
    setSaved(userList);
    return result;
  };

  const loadBuiltin = async (key, skipConfirm = false) => {
    if (!skipConfirm && dirty && !readonly) {
      setConfirm({ next: "builtin", payload: key });
      return;
    }
    const spec = await callApi("get_builtin", key);
    if (!spec) {
      return;
    }
    const flow = specToFlow(spec);
    setNodes(flow.nodes);
    setEdges(flow.edges);
    setDefaultEdgeType(spec.default_edge_type || DEFAULT_EDGE_TYPE);
    setTemplateName(spec.default_title || key);
    setReadonly(true);
    setDirty(false);
    setOpenBuiltin(key);
    setOpenSaved("");
    setSavedTemplateId(null);
    setSelectedId(null);
    setSelectedIds([]);
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setStatus("Встроенный шаблон только для просмотра. Сохранить можно как новый.");
    setError("");
  };

  const loadUser = async (id, skipConfirm = false) => {
    if (!skipConfirm && dirty && !readonly) {
      setConfirm({ next: "saved", payload: id });
      return;
    }
    const row = await callApi("get_user_template", id);
    if (!row) {
      return;
    }
    const spec = row.structure || {};
    const flow = specToFlow(spec);
    setNodes(flow.nodes);
    setEdges(flow.edges);
    setDefaultEdgeType(spec.default_edge_type || DEFAULT_EDGE_TYPE);
    setTemplateName(row.name);
    setReadonly(false);
    setDirty(false);
    setOpenSaved(String(id));
    setOpenBuiltin("");
    setSavedTemplateId(Number(id));
    setSelectedId(null);
    setSelectedIds([]);
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setStatus("Шаблон открыт для правки. «Сохранить» запишет изменения в него.");
    setError("");
  };

  const requestNew = () => {
    if (readonly || !dirty) {
      applyBlank();
      return;
    }
    setConfirm({ next: "new" });
  };

  const confirmSave = async () => {
    try {
      await save();
      const next = confirm?.next;
      const payload = confirm?.payload;
      setConfirm(null);
      if (next === "new") {
        applyBlank();
      } else if (next === "builtin") {
        await loadBuiltin(payload, true);
      } else if (next === "saved") {
        await loadUser(payload, true);
      }
    } catch (err) {
      setError(String(err.message || err));
    }
  };

  const confirmDiscard = async () => {
    const next = confirm?.next;
    const payload = confirm?.payload;
    setConfirm(null);
    if (next === "new") {
      applyBlank();
    } else if (next === "builtin") {
      await loadBuiltin(payload, true);
    } else if (next === "saved") {
      await loadUser(payload, true);
    }
  };

  const saveClick = async () => {
    try {
      await save(false);
    } catch (err) {
      setError(String(err.message || err));
    }
  };

  const saveAsNewClick = async () => {
    try {
      await save(true);
    } catch (err) {
      setError(String(err.message || err));
    }
  };

  useEffect(() => {
    const onKey = (event) => {
      const tag = String(event.target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") {
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.code === "KeyC") {
        event.preventDefault();
        copySelection();
      } else if ((event.ctrlKey || event.metaKey) && event.code === "KeyV") {
        event.preventDefault();
        pasteSelection();
      } else if (event.key === "Delete" || event.key === "Backspace") {
        event.preventDefault();
        deleteSelection();
      } else if (event.key === "Escape") {
        closeOverlays();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [copySelection, pasteSelection, deleteSelection]);

  const flowFromEvent = (event) =>
    flowRef.current?.screenToFlowPosition?.({ x: event.clientX, y: event.clientY }) || null;

  const openNodeMenu = (event, node) => {
    event.preventDefault();
    setShowTemplatePanel(false);
    setShowEditPanel(false);
    setSelectedId(node.id);
    setSelectedIds([node.id]);
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setMenu({
      x: event.clientX,
      y: event.clientY,
      kind: "node",
      nodeType: node.type,
      parentId: node.type === "step" ? node.parentId : node.id,
      flowPos: flowFromEvent(event),
    });
  };

  const openEdgeMenu = (event, edge) => {
    event.preventDefault();
    setShowTemplatePanel(false);
    setShowEditPanel(false);
    setMenu({ x: event.clientX, y: event.clientY, kind: "edge", edgeId: edge.id });
  };

  const activateMenuEdge = () => {
    const id = menu?.edgeId;
    if (!id) {
      closeMenu();
      return;
    }
    if (!selectedEdgeIdsRef.current.includes(id)) {
      applyEdgeSelection([...selectedEdgeIdsRef.current, id]);
    }
    closeMenu();
  };

  const onEdgeClick = (event, edge) => {
    toggleEdgeActivation(edge.id, event.ctrlKey || event.shiftKey);
    window.setTimeout(() => {
      suppressEdgeRfSelect.current = false;
    }, 0);
  };

  const openPaneMenu = (event) => {
    event.preventDefault();
    setShowTemplatePanel(false);
    setShowEditPanel(false);
    setMenu({
      x: event.clientX,
      y: event.clientY,
      kind: "pane",
      flowPos: flowFromEvent(event),
    });
  };

  const canCopy = Boolean(
    selectedIds.length || selectedEdgeIds.length || (menu?.kind === "edge" && menu.edgeId)
  );
  const canDelete = canCopy;
  const canPaste = Boolean(clipboard);
  const canPasteBranch = clipboardHasBranch(clipboard);
  const panelBranch =
    selected?.type === "branch" || selected?.type === "group" ? selected : null;
  const ownerBranchName =
    selected?.type === "step"
      ? branchLabelOf(nodes, selected.id)
      : selectedEdge
        ? branchLabelOf(nodes, selectedEdge.source)
        : "";

  const copyPanelBranch = () => {
    if (readonly || !panelBranch) {
      return;
    }
    const clip = snapshotSelection(nodes, edges, [panelBranch.id], []);
    if (!clip) {
      return;
    }
    clipboardRef.current = clip;
    setClipboard(clip);
    setStatus(clipboardHasBranch(clip) ? "Ветка скопирована." : "Элементы скопированы.");
  };

  const deletePanelBranch = () => {
    if (readonly || !panelBranch) {
      return;
    }
    const nodeIds = expandSelectedNodeIds(nodes, [panelBranch.id]);
    const branches = nodes.filter((node) => node.type === "branch" || node.type === "group");
    const remainingBranches = branches.filter((node) => !nodeIds.has(node.id));
    if (!remainingBranches.length && branches.some((node) => nodeIds.has(node.id))) {
      setError("Нужна хотя бы одна ветка.");
      return;
    }
    setNodes((current) => current.filter((node) => !nodeIds.has(node.id)));
    setEdges((current) =>
      current.filter((edge) => !nodeIds.has(edge.source) && !nodeIds.has(edge.target))
    );
    if (selectedIds.some((id) => nodeIds.has(id))) {
      setSelectedId(null);
      setSelectedIds([]);
    }
    setDirty(true);
    setError("");
  };
  const selectedEdges = useMemo(
    () => edges.filter((edge) => selectedEdgeIds.includes(edge.id)),
    [edges, selectedEdgeIds]
  );
  const edgeTypeValue =
    selectedEdges.length > 1
      ? "smoothstep"
      : selectedEdge?.type || defaultEdgeType;
  const openKey = openBuiltin ? `builtin:${openBuiltin}` : openSaved ? `user:${openSaved}` : "";

  const openCombinedTemplate = (value) => {
    if (!value) {
      return;
    }
    if (value.startsWith("builtin:")) {
      loadBuiltin(value.slice("builtin:".length));
      return;
    }
    if (value.startsWith("user:")) {
      loadUser(value.slice("user:".length));
    }
  };

  return (
    <TipProvider>
    <div className="app">
      <div className="toolbar">
        <div className={`tool-group ${showTemplatePanel ? "" : "is-collapsed"}`.trim()}>
          <Tip text="Создать пустой процесс или сохранить копию шаблона. Нажмите название, чтобы открыть.">
            <button
              type="button"
              className={showTemplatePanel ? "tool-group-toggle is-open" : "tool-group-toggle"}
              onClick={() => {
                setShowEditPanel(false);
                setShowTemplatePanel((open) => !open);
              }}
            >
              Шаблон
            </button>
          </Tip>
          {showTemplatePanel && (
            <>
              <Tip text="Встроенный шаблон только для просмотра. Сохранённый можно править.">
                <select
                  value={openKey}
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value.startsWith("builtin:")) {
                      setOpenBuiltin(value.slice("builtin:".length));
                      setOpenSaved("");
                    } else if (value.startsWith("user:")) {
                      setOpenSaved(value.slice("user:".length));
                      setOpenBuiltin("");
                    } else {
                      setOpenBuiltin("");
                      setOpenSaved("");
                    }
                    openCombinedTemplate(value);
                  }}
                >
                  <option value="">Открыть…</option>
                  <optgroup label="Встроенные">
                    {builtins.map((item) => (
                      <option key={`tpl-builtin:${item.key}`} value={`builtin:${item.key}`}>
                        {item.name}
                      </option>
                    ))}
                  </optgroup>
                  <optgroup label="Сохранённые">
                    {saved.map((item) => (
                      <option key={`tpl-user:${item.id}`} value={`user:${item.id}`}>
                        {item.name}
                      </option>
                    ))}
                  </optgroup>
                </select>
              </Tip>
              <Tip text="Пустой холст. Если есть несохранённые правки, программа спросит.">
                <button type="button" onClick={requestNew}>
                  Создать
                </button>
              </Tip>
              <Tip text="Сделать отдельную копию. Исходный шаблон не меняется.">
                <button type="button" onClick={saveAsNewClick}>
                  Сохранить
                </button>
              </Tip>
              <Tip text="Удалить выбранный сохранённый шаблон без восстановления. Встроенные нельзя.">
                <button
                  type="button"
                  onClick={deleteCurrentTemplate}
                  disabled={!savedTemplateId || readonly}
                >
                  Удалить шаблон
                </button>
              </Tip>
            </>
          )}
        </div>
        <div className="tool-group tool-group-process">
          <Tip text="Имя процесса и сохранение правок в текущий шаблон.">
            <span className="tool-group-title">Процесс</span>
          </Tip>
          <Tip text="Это название процесса, его можно переименовать.">
            <input
              value={templateName}
              onChange={(event) => {
                setTemplateName(event.target.value);
                if (!readonly) {
                  setDirty(true);
                }
              }}
              disabled={readonly}
              placeholder="Название"
            />
          </Tip>
          <Tip text="Записать изменения в текущий шаблон. Уже запущенные процессы не меняются.">
            <button type="button" onClick={saveClick}>
              Сохранить изменения
            </button>
          </Tip>
        </div>
        <div className={`tool-group ${showEditPanel ? "" : "is-collapsed"}`.trim()}>
          <Tip text="Копировать, вставить и удалить выделенное. Нажмите название, чтобы открыть.">
            <button
              type="button"
              className={showEditPanel ? "tool-group-toggle is-open" : "tool-group-toggle"}
              onClick={() => {
                setShowTemplatePanel(false);
                setShowEditPanel((open) => !open);
              }}
            >
              Правка
            </button>
          </Tip>
          {showEditPanel && (
            <>
              <Tip text="Скопировать выделенную ветку, шаг или связь. Ctrl+C.">
                <button type="button" onClick={copySelection} disabled={readonly || !canCopy}>
                  Копировать
                </button>
              </Tip>
              <Tip text="Вставить скопированное. Ctrl+V. Связь вставляется между двумя выделенными шагами.">
                <button type="button" onClick={pasteSelection} disabled={readonly || !canPaste}>
                  Вставить
                </button>
              </Tip>
              <Tip text="Удалить выделенное. Delete. Последнюю ветку удалить нельзя.">
                <button type="button" onClick={deleteSelection} disabled={readonly || !canDelete}>
                  Удалить
                </button>
              </Tip>
            </>
          )}
        </div>
        <span className={error ? "status error" : "status"}>{error || status}</span>
      </div>
      {confirm && (
        <div className="modal-backdrop">
          <div className="modal">
            <p>
              Сохранить текущий шаблон «{templateName || "без названия"}» перед тем, как
              продолжить?
            </p>
            <div className="modal-actions">
              <Tip text="Сохранить текущий шаблон, затем продолжить.">
                <button type="button" onClick={confirmSave}>
                  Сохранить
                </button>
              </Tip>
              <Tip text="Продолжить без сохранения. Правки на холсте пропадут.">
                <button type="button" onClick={confirmDiscard}>
                  Не сохранять
                </button>
              </Tip>
              <Tip text="Остаться на текущем шаблоне.">
                <button type="button" onClick={() => setConfirm(null)}>
                  Отмена
                </button>
              </Tip>
            </div>
          </div>
        </div>
      )}
      <div
        className="workspace"
        style={{ gridTemplateColumns: `minmax(0, 1fr) ${panelWidth}px` }}
      >
        <div
          className="canvas"
          onPointerDownCapture={(event) => {
            if (event.button !== 0) {
              return;
            }
            if (event.target.closest?.(".react-flow__edge")) {
              suppressEdgeRfSelect.current = true;
            }
          }}
        >
          <div className={canvasMenuOpen ? "canvas-float-slot" : "canvas-float-slot is-collapsed"}>
            {canvasMenuOpen ? (
              <aside className="canvas-float">
                <div className="canvas-float-head">
                  <span className="tool-group-title">Холст</span>
                  <Tip text="Скрыть меню. Холст освободится, открыть можно снова.">
                    <button
                      type="button"
                      className="canvas-float-hide"
                      onClick={() => setCanvasMenuVisible(false)}
                    >
                      Скрыть
                    </button>
                  </Tip>
                </div>
                <Tip text="Новая ветка — отдельная рамка для своих шагов.">
                  <button type="button" onClick={addBranch} disabled={readonly}>
                    Добавить ветку
                  </button>
                </Tip>
                <Tip text="Новый шаг в выделенную ветку. Если ветка не выбрана — в первую.">
                  <button type="button" onClick={() => addStep()} disabled={readonly}>
                    Добавить шаг
                  </button>
                </Tip>
                <div className="canvas-float-edges">
                  <span className="canvas-float-label">Тип связи</span>
                  <Tip text="Форма линии: гибкая, плавная, ломаная или прямая. Несколько связей: Ctrl+клик добавляет к выбору, тип меняется у всех, по умолчанию плавная.">
                    <select
                      value={edgeTypeValue}
                      disabled={readonly}
                      onChange={(event) => changeEdgeType(event.target.value)}
                    >
                      {EDGE_TYPE_OPTIONS.map((item) => (
                        <option key={item.value} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                  </Tip>
                  <Tip text="Соединить шаги: от более раннего по созданию к более позднему. Нужны хотя бы два шага.">
                    <button type="button" onClick={addCanvasEdge} disabled={readonly || !autoEdgePair}>
                      Добавить связь
                    </button>
                  </Tip>
                  <Tip text="Убрать выделенную стрелку между шагами. Если выбрано несколько — все сразу. Delete тоже удаляет.">
                    <button type="button" onClick={deleteSelectedEdge} disabled={readonly || !selectedEdges.length}>
                      Удалить связь
                    </button>
                  </Tip>
                </div>
              </aside>
            ) : (
              <Tip text="Показать меню инструментов холста.">
                <button
                  type="button"
                  className="canvas-float-show"
                  onClick={() => setCanvasMenuVisible(true)}
                >
                  Холст
                </button>
              </Tip>
            )}
          </div>
          <ReactFlow
            nodes={displayNodes}
            edges={displayEdges}
            nodeTypes={nodeTypes}
            onInit={(instance) => {
              flowRef.current = instance;
            }}
            nodesDraggable={!readonly}
            nodesConnectable={!readonly}
            edgesReconnectable={!readonly}
            multiSelectionKeyCode={["Control", "Shift"]}
            deleteKeyCode={null}
            defaultEdgeOptions={withArrow({ type: defaultEdgeType })}
            connectionLineType={CONNECTION_LINE_TYPES[defaultEdgeType] || ConnectionLineType.Bezier}
            isValidConnection={isValidConnection}
            onNodesChange={readonly ? undefined : handleNodesChange}
            onEdgesChange={readonly ? undefined : handleEdgesChange}
            onConnect={onConnect}
            onReconnect={onReconnect}
            onReconnectStart={onReconnectStart}
            onReconnectEnd={onReconnectEnd}
            onPaneClick={closeOverlays}
            onMoveStart={closeOverlays}
            onNodeContextMenu={readonly ? undefined : openNodeMenu}
            onEdgeClick={onEdgeClick}
            onEdgeContextMenu={readonly ? undefined : openEdgeMenu}
            onPaneContextMenu={readonly ? undefined : openPaneMenu}
            onSelectionChange={({ nodes: selectedNodes, edges: selectedEdges }) => {
              setSelectedIds(selectedNodes.map((node) => node.id));
              setSelectedId(selectedNodes[0]?.id || null);
              if (suppressEdgeRfSelect.current) {
                return;
              }
              setSelectedEdgeIds(selectedEdges.map((edge) => edge.id));
              setSelectedEdgeId(selectedEdges[0]?.id || null);
            }}
            fitView
          >
            <Background />
            <Controls />
            <MiniMap />
          </ReactFlow>
          {menu && (
            <div ref={menuRef} className="ctx-menu" style={{ left: menu.x, top: menu.y }}>
              {menu.kind === "pane" && (
                <Tip text="Новая ветка на пустом месте холста.">
                  <button type="button" onClick={() => { addBranch(); closeMenu(); }}>
                    Добавить ветку
                  </button>
                </Tip>
              )}
              {menu.kind === "pane" && canPasteBranch && (
                <Tip text="Вставить скопированную ветку на холст.">
                  <button
                    type="button"
                    onClick={() => {
                      pasteSelection(menu.flowPos);
                    }}
                  >
                    Вставить
                  </button>
                </Tip>
              )}
              {(menu.nodeType === "branch" ||
                menu.nodeType === "group" ||
                menu.nodeType === "step") && (
                <Tip text="Новый шаг в этом месте ветки.">
                  <button
                    type="button"
                    onClick={() => {
                      addStep(menu.parentId, menu.flowPos);
                      closeMenu();
                    }}
                  >
                    Добавить шаг
                  </button>
                </Tip>
              )}
              {menu.kind === "edge" && (
                <Tip text="Выделить эту связь. Повторный левый клик снимает выделение.">
                  <button type="button" onClick={activateMenuEdge}>
                    Активировать
                  </button>
                </Tip>
              )}
              {menu.kind !== "pane" && (
                <Tip text="Скопировать этот элемент. Ctrl+C.">
                  <button type="button" onClick={copySelection} disabled={!canCopy}>
                    Копировать
                  </button>
                </Tip>
              )}
              {menu.kind !== "pane" && (
                <Tip text="Вставить скопированное сюда. Ctrl+V.">
                  <button type="button" onClick={pasteSelection} disabled={!canPaste}>
                    Вставить
                  </button>
                </Tip>
              )}
              {menu.kind !== "pane" && (
                <Tip text="Удалить этот элемент. Delete.">
                  <button type="button" onClick={deleteSelection} disabled={!canDelete}>
                    Удалить
                  </button>
                </Tip>
              )}
            </div>
          )}
        </div>
        <aside className="panel">
          <div
            className="panel-resizer"
            title="Потяните, чтобы изменить ширину"
            onPointerDown={startPanelResize}
          />
          <h2>Свойства</h2>
          {selected?.type === "step" && (
            <>
              {ownerBranchName ? <p className="panel-owner-branch">Ветка: {ownerBranchName}</p> : null}
              <Tip text="Текст на карточке шага.">
                <label>Название шага</label>
                <textarea
                  rows={3}
                  value={selected.data?.title || ""}
                  disabled={readonly}
                  onChange={(event) => patchSelected({ title: event.target.value })}
                />
              </Tip>
              <Tip text="Как закрыть шаг: вручную, по чек-листу или ожидая другую сторону.">
                <label>Тип завершения</label>
                <select
                  value={selected.data?.completion || defaults.completion_manual}
                  disabled={readonly}
                  onChange={(event) => patchSelected({ completion: event.target.value })}
                >
                  <option value={defaults.completion_manual}>{defaults.completion_manual}</option>
                  <option value={defaults.completion_checklist}>{defaults.completion_checklist}</option>
                  <option value={defaults.completion_wait}>{defaults.completion_wait}</option>
                </select>
              </Tip>
              {selected.data?.completion === defaults.completion_manual && (
                <Tip text="Пусто — отметку можно поставить сразу.">
                  <label>Действует с</label>
                  <div className="date-row">
                    <input
                      type="date"
                      value={selected.data?.manualActiveFrom || ""}
                      disabled={readonly}
                      onChange={(event) =>
                        patchSelected({ manualActiveFrom: event.target.value })
                      }
                    />
                    <button
                      type="button"
                      disabled={readonly}
                      onClick={() => patchSelected({ manualActiveFrom: "" })}
                    >
                      ×
                    </button>
                  </div>
                </Tip>
              )}
              {selected.data?.completion === defaults.completion_wait && (
                <Tip text="Пусто — ожидание стороны действует сразу.">
                  <label>Действует с</label>
                  <div className="date-row">
                    <input
                      type="date"
                      value={selected.data?.waitSideFrom || ""}
                      disabled={readonly}
                      onChange={(event) =>
                        patchSelected({ waitSideFrom: event.target.value })
                      }
                    />
                    <button
                      type="button"
                      disabled={readonly}
                      onClick={() => patchSelected({ waitSideFrom: "" })}
                    >
                      ×
                    </button>
                  </div>
                </Tip>
              )}
              {selected.data?.completion === defaults.completion_checklist && (
                <>
                  <Tip text="Каждый пункт с новой строки. Шаг закроется, когда отмечены все.">
                    <label>Пункты чек-листа (по одному в строке)</label>
                    <textarea
                      rows={5}
                      value={selected.data?.checklistText || ""}
                      disabled={readonly}
                      onChange={(event) => patchSelected({ checklistText: event.target.value })}
                    />
                  </Tip>
                  <Tip text="Пусто — чек-лист действует сразу.">
                    <label>Чек-лист действует с</label>
                    <div className="date-row">
                      <input
                        type="date"
                        value={selected.data?.checklistActiveFrom || ""}
                        disabled={readonly}
                        onChange={(event) =>
                          patchSelected({ checklistActiveFrom: event.target.value })
                        }
                      />
                      <button
                        type="button"
                        disabled={readonly}
                        onClick={() => patchSelected({ checklistActiveFrom: "" })}
                      >
                        ×
                      </button>
                    </div>
                  </Tip>
                </>
              )}
              <Tip text="Напоминать, пока шаг не выполнен. Шаги эскалации идут по очереди.">
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={Boolean(selected.data?.escalate)}
                    disabled={readonly}
                    onChange={(event) => patchSelected({ escalate: event.target.checked })}
                  />
                  Эскалация напоминаний
                </label>
              </Tip>
              {selected.data?.escalate && (
                <>
                  <p className="hint">
                    Каждый следующий шаг начинается после предыдущего. Последний повторяется,
                    пока шаг не выполнен.
                  </p>
                  {(selected.data?.escalationSteps || []).map((item, index, list) => (
                    <div key={`esc-${index}`} className="escalation-row">
                      <div className="escalation-row-fields">
                      <span>Шаг {index + 1}</span>
                      <select
                        value={item.kind}
                        disabled={readonly}
                        onChange={(event) => {
                          const next = [...(selected.data.escalationSteps || [])];
                          next[index] = { ...item, kind: event.target.value };
                          patchSelected({ escalationSteps: next });
                        }}
                      >
                        <option value="after">Через это время напоминать</option>
                        <option value="at">В это время начать напоминать</option>
                      </select>
                      {item.kind === "at" ? (
                        <input
                          value={item.time || "09:00"}
                          disabled={readonly}
                          onChange={(event) => {
                            const next = [...(selected.data.escalationSteps || [])];
                            next[index] = { ...item, time: event.target.value };
                            patchSelected({ escalationSteps: next });
                          }}
                        />
                      ) : (
                        <>
                          <input
                            type="number"
                            min="0"
                            max="168"
                            value={Math.floor((item.minutes || 180) / 60)}
                            disabled={readonly}
                            onChange={(event) => {
                              const next = [...(selected.data.escalationSteps || [])];
                              const mins = (item.minutes || 0) % 60;
                              next[index] = {
                                ...item,
                                minutes: Number(event.target.value || 0) * 60 + mins,
                              };
                              patchSelected({ escalationSteps: next });
                            }}
                          />
                          <span>ч</span>
                          <input
                            type="number"
                            min="0"
                            max="59"
                            value={(item.minutes || 0) % 60}
                            disabled={readonly}
                            onChange={(event) => {
                              const next = [...(selected.data.escalationSteps || [])];
                              const hours = Math.floor((item.minutes || 0) / 60);
                              next[index] = {
                                ...item,
                                minutes: hours * 60 + Number(event.target.value || 0),
                              };
                              patchSelected({ escalationSteps: next });
                            }}
                          />
                          <span>мин</span>
                        </>
                      )}
                      <button
                        type="button"
                        disabled={readonly || index === 0}
                        onClick={() => {
                          const next = [...(selected.data.escalationSteps || [])];
                          [next[index - 1], next[index]] = [next[index], next[index - 1]];
                          patchSelected({ escalationSteps: next });
                        }}
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        disabled={readonly || index === (selected.data.escalationSteps || []).length - 1}
                        onClick={() => {
                          const next = [...(selected.data.escalationSteps || [])];
                          [next[index + 1], next[index]] = [next[index], next[index + 1]];
                          patchSelected({ escalationSteps: next });
                        }}
                      >
                        ↓
                      </button>
                      <button
                        type="button"
                        disabled={readonly}
                        onClick={() => {
                          const next = (selected.data.escalationSteps || []).filter((_, i) => i !== index);
                          patchSelected({ escalationSteps: next });
                        }}
                      >
                        Удалить
                      </button>
                      </div>
                      <p className="hint">{escalationStepHint(item, index, list.length)}</p>
                    </div>
                  ))}
                  <button
                    type="button"
                    disabled={readonly}
                    onClick={() =>
                      patchSelected({
                        escalationSteps: [
                          ...(selected.data.escalationSteps || []),
                          { kind: "after", minutes: 180, time: "" },
                        ],
                      })
                    }
                  >
                    Добавить шаг эскалации
                  </button>
                  <Tip text="После этого времени напоминания переносятся на следующий день.">
                    <label>Уведомлять до</label>
                    <input
                      value={selected.data?.notifyUntil || "18:00"}
                      disabled={readonly}
                      onChange={(event) => patchSelected({ notifyUntil: event.target.value })}
                    />
                  </Tip>
                  <label className="check-label">
                    <input
                      type="checkbox"
                      checked={Boolean(selected.data?.notifyWeekends)}
                      disabled={readonly}
                      onChange={(event) =>
                        patchSelected({ notifyWeekends: event.target.checked })
                      }
                    />
                    Уведомлять в выходные
                  </label>
                </>
              )}
              <Tip text="Скопировать шаг со всеми настройками. Ctrl+C.">
                <button type="button" onClick={copySelection} disabled={readonly}>
                  Копировать шаг
                </button>
              </Tip>
              <Tip text="Удалить шаг и его стрелки. Delete.">
                <button type="button" onClick={deleteSelection} disabled={readonly}>
                  Удалить шаг
                </button>
              </Tip>
            </>
          )}
          {selected?.type !== "step" && panelBranch && (
            <>
              <label>Ветка</label>
              <Tip text="Подпись рамки ветки на холсте.">
                <label>Название ветки</label>
                <input
                  value={panelBranch.data?.label || ""}
                  disabled={readonly}
                  onChange={(event) => {
                    const next = event.target.value;
                    setDirty(true);
                    setNodes((current) =>
                      current.map((node) =>
                        node.id === panelBranch.id
                          ? { ...node, data: { ...node.data, label: next } }
                          : node
                      )
                    );
                  }}
                />
              </Tip>
              <Tip text="Скопировать ветку вместе с шагами и внутренними стрелками.">
                <button type="button" onClick={copyPanelBranch} disabled={readonly}>
                  Копировать ветку
                </button>
              </Tip>
              <Tip text="Удалить ветку и все шаги в ней. Последнюю ветку удалить нельзя.">
                <button type="button" onClick={deletePanelBranch} disabled={readonly}>
                  Удалить ветку
                </button>
              </Tip>
            </>
          )}
          {selected?.type !== "step" && !panelBranch && selectedEdge && (
            <>
              {ownerBranchName ? <p className="panel-owner-branch">Ветка: {ownerBranchName}</p> : null}
              <label>Связь</label>
              <p>
                {nodeTitle(nodes, selectedEdge.source)} → {nodeTitle(nodes, selectedEdge.target)}
              </p>
              <Tip text="Как выглядит эта стрелка: гибкая, плавная, ломаная или прямая.">
                <label>Тип линии</label>
                <select
                  value={selectedEdge.type || "default"}
                  disabled={readonly}
                  onChange={(event) => changeEdgeType(event.target.value)}
                >
                  {EDGE_TYPE_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </Tip>
              <Tip text="Запомнить тип и направление этой связи. Ctrl+C.">
                <button type="button" onClick={copySelection} disabled={readonly}>
                  Копировать связь
                </button>
              </Tip>
              <Tip text="Убрать эту стрелку. Delete.">
                <button type="button" onClick={deleteSelectedEdge} disabled={readonly}>
                  Удалить связь
                </button>
              </Tip>
            </>
          )}
          {selected?.type !== "step" && !panelBranch && !selectedEdge && (
            <p className="hint">Ничего не выбрано.</p>
          )}
        </aside>
      </div>
    </div>
    </TipProvider>
  );
}
