import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
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

const EDGE_MARKER = { type: MarkerType.ArrowClosed, width: 18, height: 18, color: "#35557a" };

const EDGE_TYPE_OPTIONS = [
  { value: "default", label: "Гибкая" },
  { value: "smoothstep", label: "Сглаженная" },
  { value: "step", label: "Ломаная" },
  { value: "straight", label: "Прямая" },
];

const CONNECTION_LINE_TYPES = {
  default: ConnectionLineType.Bezier,
  smoothstep: ConnectionLineType.SmoothStep,
  step: ConnectionLineType.Step,
  straight: ConnectionLineType.Straight,
};

function emptyStepData(defaults, title = "Новый шаг") {
  return {
    title,
    completion: defaults.completion_manual,
    checklistText: "",
    escalate: false,
    firstHours: 2,
    repeatTime: defaults.repeat_time,
    frequentHours: 3,
    notifyUntil: defaults.notify_until,
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
  return {
    ...edge,
    markerEnd: EDGE_MARKER,
    style: { ...(edge.style || {}), stroke: "#35557a", strokeWidth: 2 },
  };
}

function makeEdge(source, target, extra = {}) {
  return withArrow({
    id: extra.id || `e-${source}-${target}-${extra.sourceHandle || "out"}-${extra.targetHandle || "in"}`,
    source,
    target,
    sourceHandle: extra.sourceHandle || "out",
    targetHandle: extra.targetHandle || "in",
    type: extra.type || "default",
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

function BranchNode({ data, selected }) {
  return (
    <div className="branch-node">
      <NodeResizer
        minWidth={240}
        minHeight={160}
        isVisible={selected && !data.locked}
        lineClassName="resize-line"
        handleClassName="resize-handle"
      />
      <div className="branch-title">{data.label || "Ветка"}</div>
    </div>
  );
}

function StepNode({ data, selected }) {
  return (
    <div className="step-node">
      <NodeResizer
        minWidth={160}
        minHeight={48}
        isVisible={selected && !data.locked}
        lineClassName="resize-line"
        handleClassName="resize-handle"
      />
      <Handle type="target" id="in" position={Position.Top} />
      <Handle type="target" id="in-left" position={Position.Left} />
      <div className="step-title">{data.title || data.label || "Шаг"}</div>
      <Handle type="source" id="out" position={Position.Bottom} />
      <Handle type="source" id="out-right" position={Position.Right} />
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

function makeStep(id, parentId, y, data) {
  return {
    id,
    type: "step",
    parentId,
    extent: "parent",
    position: { x: 24, y },
    data,
    style: { ...STEP_STYLE, width: 300, height: 56 },
    width: 300,
    height: 56,
  };
}

function specToFlow(spec) {
  const nodes = [];
  const edges = [];
  const branches = spec.branches || [];
  const defaultType = spec.default_edge_type || "default";
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
          firstHours: Math.max(1, Math.round((first?.delay_minutes || 120) / 60)),
          repeatTime: repeat?.fixed_time || "09:00",
          frequentHours: Math.max(1, Math.round((frequent?.delay_minutes || 180) / 60)),
          notifyUntil: step.notify_until || "18:00",
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
            escalation: escalate
              ? [
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
                ]
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
    default_edge_type: defaultEdgeType || "default",
    edges: edges.map((edge) => ({
      id: edge.id,
      source: String(edge.source || "").replace(/^s-/, ""),
      target: String(edge.target || "").replace(/^s-/, ""),
      type: edge.type || defaultEdgeType || "default",
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
    nodes: [makeBranch("b-main", 60), makeStep("s-s1", "b-main", 48, emptyStepData(defaults, "Первый шаг"))],
    edges: [],
  };
}

function nodeTitle(nodes, nodeId) {
  const node = nodes.find((item) => item.id === nodeId);
  return node?.data?.title || node?.data?.label || nodeId;
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

function remapPastedGraph(clip, nodes, selected) {
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
    } else {
      copy.position = {
        x: (node.position?.x || 40) + 60,
        y: (node.position?.y || 40) + 24,
      };
    }
    return copy;
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

const TipContext = createContext(null);

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
  const onEnter = (event) => {
    if (!ctx || !text) {
      return;
    }
    ctx.show(text, event.currentTarget.getBoundingClientRect());
  };
  return (
    <span className="tip" onMouseEnter={onEnter} onMouseLeave={() => ctx?.hide()}>
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
  const [showOpenPanel, setShowOpenPanel] = useState(false);
  const [showEditPanel, setShowEditPanel] = useState(false);
  const [defaultEdgeType, setDefaultEdgeType] = useState("default");
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
  const [clipboard, setClipboard] = useState(null);

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
        data: { ...node.data, locked: readonly },
      })),
    [nodes, readonly]
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
      onEdgesChange(changes);
      if (changes.some((item) => item.type !== "select")) {
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
    if (readonly) {
      return;
    }
    if (selectedEdge) {
      setEdges((current) =>
        current.map((edge) => (edge.id === selectedEdge.id ? withArrow({ ...edge, type: value }) : edge))
      );
      setDirty(true);
      return;
    }
    setDefaultEdgeType(value);
  };

  const deleteSelectedEdge = () => {
    if (readonly || !selectedEdge) {
      return;
    }
    setEdges((current) => current.filter((edge) => edge.id !== selectedEdge.id));
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setDirty(true);
  };

  const closeMenu = () => setMenu(null);

  const closeOverlays = () => {
    setMenu(null);
    setShowTemplatePanel(false);
    setShowOpenPanel(false);
    setShowEditPanel(false);
  };

  const copySelection = useCallback(() => {
    if (readonly) {
      return;
    }
    const clip = snapshotSelection(nodes, edges, selectedIds, selectedEdgeIds);
    if (!clip) {
      return;
    }
    clipboardRef.current = clip;
    setClipboard(clip);
    setStatus(clip.kind === "edge" ? "Связь скопирована." : "Элементы скопированы.");
    closeMenu();
  }, [readonly, nodes, edges, selectedIds, selectedEdgeIds]);

  const deleteSelection = useCallback(() => {
    if (readonly) {
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
    setDirty(true);
    setError("");
    closeMenu();
  }, [readonly, nodes, selectedIds, selectedEdgeIds, setNodes, setEdges]);

  const pasteSelection = useCallback(() => {
    if (readonly) {
      return;
    }
    const clip = clipboardRef.current || clipboard;
    if (!clip) {
      return;
    }
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
      setDefaultEdgeType(clip.edge.type || "default");
      setStatus("Тип линии скопирован. Выделите два шага и вставьте связь.");
      closeMenu();
      return;
    }
    const pasted = remapPastedGraph(clip, nodes, selected);
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

  const addStep = () => {
    if (readonly) {
      return;
    }
    const groups = nodes.filter((node) => node.type === "branch" || node.type === "group");
    const parent = (selected?.type === "branch" || selected?.type === "group" ? selected : groups[0]) || null;
    if (!parent) {
      addBranch();
      return;
    }
    const siblings = nodes.filter((node) => node.parentId === parent.id);
    setNodes((current) => [
      ...current,
      makeStep(`s-${Date.now()}`, parent.id, 48 + siblings.length * 86, emptyStepData(defaults)),
    ]);
    setDirty(true);
  };

  const patchSelected = (patch) => {
    if (!selected || readonly) {
      return;
    }
    setNodes((current) =>
      current.map((node) =>
        node.id === selected.id ? { ...node, data: { ...node.data, ...patch } } : node
      )
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
    setEdges(blank.edges);
    setNodes(blank.nodes);
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
    setDefaultEdgeType(spec.default_edge_type || "default");
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
    setDefaultEdgeType(spec.default_edge_type || "default");
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

  const openNodeMenu = (event, node) => {
    event.preventDefault();
    setShowTemplatePanel(false);
    setShowOpenPanel(false);
    setShowEditPanel(false);
    setSelectedId(node.id);
    setSelectedIds([node.id]);
    setSelectedEdgeId(null);
    setSelectedEdgeIds([]);
    setMenu({ x: event.clientX, y: event.clientY, kind: "node" });
  };

  const openEdgeMenu = (event, edge) => {
    event.preventDefault();
    setShowTemplatePanel(false);
    setShowOpenPanel(false);
    setShowEditPanel(false);
    setSelectedEdgeId(edge.id);
    setSelectedEdgeIds([edge.id]);
    setSelectedId(null);
    setSelectedIds([]);
    setMenu({ x: event.clientX, y: event.clientY, kind: "edge" });
  };

  const openPaneMenu = (event) => {
    event.preventDefault();
    setShowTemplatePanel(false);
    setShowOpenPanel(false);
    setShowEditPanel(false);
    setMenu({ x: event.clientX, y: event.clientY, kind: "pane" });
  };

  const canCopy = Boolean(selectedIds.length || selectedEdgeIds.length);
  const canDelete = canCopy;
  const canPaste = Boolean(clipboard);
  const edgeTypeValue = selectedEdge?.type || defaultEdgeType;
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
        <div className={`tool-group ${showOpenPanel ? "" : "is-collapsed"}`.trim()}>
          <Tip text="Открыть встроенный или свой шаблон. Нажмите название, чтобы открыть список.">
            <button
              type="button"
              className={showOpenPanel ? "tool-group-toggle is-open" : "tool-group-toggle"}
              onClick={() => {
                setShowTemplatePanel(false);
                setShowEditPanel(false);
                setShowOpenPanel((open) => !open);
              }}
            >
              Открыть
            </button>
          </Tip>
          {showOpenPanel && (
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
                <option value="">Выберите шаблон…</option>
                <optgroup label="Встроенные">
                  {builtins.map((item) => (
                    <option key={`builtin:${item.key}`} value={`builtin:${item.key}`}>
                      {item.name}
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Сохранённые">
                  {saved.map((item) => (
                    <option key={`user:${item.id}`} value={`user:${item.id}`}>
                      {item.name}
                    </option>
                  ))}
                </optgroup>
              </select>
            </Tip>
          )}
        </div>
        <div className={`tool-group ${showTemplatePanel ? "" : "is-collapsed"}`.trim()}>
          <Tip text="Создать пустой процесс или сохранить копию шаблона. Нажмите название, чтобы открыть.">
            <button
              type="button"
              className={showTemplatePanel ? "tool-group-toggle is-open" : "tool-group-toggle"}
              onClick={() => {
                setShowOpenPanel(false);
                setShowEditPanel(false);
                setShowTemplatePanel((open) => !open);
              }}
            >
              Шаблон
            </button>
          </Tip>
          {showTemplatePanel && (
            <>
              <Tip text="Пустой холст. Если есть несохранённые правки, программа спросит.">
                <button type="button" onClick={requestNew}>
                  Создать новый
                </button>
              </Tip>
              <Tip text="Сделать отдельную копию. Исходный шаблон не меняется.">
                <button type="button" onClick={saveAsNewClick}>
                  Сохранить новый
                </button>
              </Tip>
            </>
          )}
        </div>
        <div className="tool-group">
          <Tip text="Имя процесса и сохранение правок в текущий шаблон.">
            <span className="tool-group-title">Процесс</span>
          </Tip>
          <Tip text="Имя шаблона. Так он будет виден в списке сохранённых.">
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
        <div className="tool-group">
          <Tip text="Добавить ветку или шаг и настроить стрелку между шагами.">
            <span className="tool-group-title">Холст</span>
          </Tip>
          <Tip text="Новая ветка — отдельная рамка для своих шагов.">
            <button type="button" onClick={addBranch} disabled={readonly}>
              Добавить ветку
            </button>
          </Tip>
          <Tip text="Новый шаг в выделенную ветку. Если ветка не выбрана — в первую.">
            <button type="button" onClick={addStep} disabled={readonly}>
              Добавить шаг
            </button>
          </Tip>
          <Tip text="Форма линии: гибкая, сглаженная, ломаная или прямая. Если линия выделена — меняется она, иначе тип для новых.">
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
          <Tip text="Убрать выделенную стрелку между шагами. Delete тоже удаляет.">
            <button type="button" onClick={deleteSelectedEdge} disabled={readonly || !selectedEdge}>
              Удалить связь
            </button>
          </Tip>
        </div>
        <div className={`tool-group ${showEditPanel ? "" : "is-collapsed"}`.trim()}>
          <Tip text="Копировать, вставить и удалить выделенное. Нажмите название, чтобы открыть.">
            <button
              type="button"
              className={showEditPanel ? "tool-group-toggle is-open" : "tool-group-toggle"}
              onClick={() => {
                setShowOpenPanel(false);
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
      <div className="workspace">
        <div className="canvas">
          <ReactFlow
            nodes={displayNodes}
            edges={edges}
            nodeTypes={nodeTypes}
            nodesDraggable={!readonly}
            nodesConnectable={!readonly}
            edgesReconnectable={!readonly}
            multiSelectionKeyCode="Shift"
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
            onEdgeContextMenu={readonly ? undefined : openEdgeMenu}
            onPaneContextMenu={readonly ? undefined : openPaneMenu}
            onSelectionChange={({ nodes: selectedNodes, edges: selectedEdges }) => {
              setSelectedIds(selectedNodes.map((node) => node.id));
              setSelectedId(selectedNodes[0]?.id || null);
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
            <div className="ctx-menu" style={{ left: menu.x, top: menu.y }}>
              {menu.kind !== "pane" && (
                <Tip text="Скопировать этот элемент. Ctrl+C.">
                  <button type="button" onClick={copySelection} disabled={!canCopy}>
                    Копировать
                  </button>
                </Tip>
              )}
              <Tip text="Вставить скопированное сюда. Ctrl+V.">
                <button type="button" onClick={pasteSelection} disabled={!canPaste}>
                  Вставить
                </button>
              </Tip>
              {menu.kind !== "pane" && (
                <Tip text="Удалить этот элемент. Delete.">
                  <button type="button" onClick={deleteSelection} disabled={!canDelete}>
                    Удалить
                  </button>
                </Tip>
              )}
              {menu.kind === "pane" && (
                <>
                  <Tip text="Новая ветка на пустом месте холста.">
                    <button type="button" onClick={() => { addBranch(); closeMenu(); }}>
                      Добавить ветку
                    </button>
                  </Tip>
                  <Tip text="Новый шаг в текущую или первую ветку.">
                    <button type="button" onClick={() => { addStep(); closeMenu(); }}>
                      Добавить шаг
                    </button>
                  </Tip>
                </>
              )}
            </div>
          )}
        </div>
        <aside className="panel">
          <h2>Свойства</h2>
          <p className="hint">
            Наведите на кнопку — коротко, что она делает. Правая кнопка на холсте тоже открывает
            команды.
          </p>
          {selectedEdge && (
            <>
              <label>Связь</label>
              <p>
                {nodeTitle(nodes, selectedEdge.source)} → {nodeTitle(nodes, selectedEdge.target)}
              </p>
              <Tip text="Как выглядит эта стрелка: гибкая, сглаженная, ломаная или прямая.">
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
          {!selected && !selectedEdge && <p className="hint">Ничего не выбрано.</p>}
          {(selected?.type === "branch" || selected?.type === "group") && (
            <>
              <Tip text="Подпись рамки ветки на холсте.">
                <label>Название ветки</label>
                <input
                  value={selected.data?.label || ""}
                  disabled={readonly}
                  onChange={(event) => {
                    setDirty(true);
                    setNodes((current) =>
                      current.map((node) =>
                        node.id === selected.id
                          ? { ...node, data: { ...node.data, label: event.target.value } }
                          : node
                      )
                    );
                  }}
                />
              </Tip>
              <Tip text="Скопировать ветку вместе с шагами и внутренними стрелками.">
                <button type="button" onClick={copySelection} disabled={readonly}>
                  Копировать ветку
                </button>
              </Tip>
              <Tip text="Удалить ветку и все шаги в ней. Последнюю ветку удалить нельзя.">
                <button type="button" onClick={deleteSelection} disabled={readonly}>
                  Удалить ветку
                </button>
              </Tip>
            </>
          )}
          {selected?.type === "step" && (
            <>
              <Tip text="Текст на карточке шага.">
                <label>Название шага</label>
                <input
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
              {selected.data?.completion === defaults.completion_checklist && (
                <Tip text="Каждый пункт с новой строки. Шаг закроется, когда отмечены все.">
                  <label>Пункты чек-листа (по одному в строке)</label>
                  <textarea
                    rows={5}
                    value={selected.data?.checklistText || ""}
                    disabled={readonly}
                    onChange={(event) => patchSelected({ checklistText: event.target.value })}
                  />
                </Tip>
              )}
              <Tip text="Напоминать, пока шаг не выполнен: первое, повтор утром и дальше через интервал.">
                <label>
                  <input
                    type="checkbox"
                    checked={Boolean(selected.data?.escalate)}
                    disabled={readonly}
                    onChange={(event) => patchSelected({ escalate: event.target.checked })}
                  />{" "}
                  Эскалация напоминаний
                </label>
              </Tip>
              {selected.data?.escalate && (
                <>
                  <Tip text="Через сколько часов придёт первое напоминание.">
                    <label>Первое через, часов</label>
                    <input
                      type="number"
                      min="1"
                      max="48"
                      value={selected.data?.firstHours || 2}
                      disabled={readonly}
                      onChange={(event) => patchSelected({ firstHours: event.target.value })}
                    />
                  </Tip>
                  <Tip text="Во сколько на следующий рабочий день придёт повтор.">
                    <label>Повтор (ЧЧ:ММ)</label>
                    <input
                      value={selected.data?.repeatTime || "09:00"}
                      disabled={readonly}
                      onChange={(event) => patchSelected({ repeatTime: event.target.value })}
                    />
                  </Tip>
                  <Tip text="Как часто напоминать после повтора.">
                    <label>Дальше каждые, часов</label>
                    <input
                      type="number"
                      min="1"
                      max="12"
                      value={selected.data?.frequentHours || 3}
                      disabled={readonly}
                      onChange={(event) => patchSelected({ frequentHours: event.target.value })}
                    />
                  </Tip>
                  <Tip text="После этого времени напоминания переносятся на следующий рабочий день.">
                    <label>Уведомлять до</label>
                    <input
                      value={selected.data?.notifyUntil || "18:00"}
                      disabled={readonly}
                      onChange={(event) => patchSelected({ notifyUntil: event.target.value })}
                    />
                  </Tip>
                </>
              )}
              <p className="hint">
                Один шаг может вести на несколько следующих, и в один шаг могут входить несколько
                предыдущих. Целевой шаг откроется, когда выполнены все входящие.
              </p>
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
        </aside>
      </div>
    </div>
    </TipProvider>
  );
}
