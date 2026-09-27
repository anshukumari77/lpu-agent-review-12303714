import { useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  MarkerType,
  type Node,
  type Edge,
  type NodeProps,
} from "@xyflow/react";
import dagre from "@dagrejs/dagre";
import "@xyflow/react/dist/style.css";
import type { WorkflowStep, WorkflowEdge, Evidence } from "./types";
import { EmptyState } from "./ui";
import { EvidenceList } from "./ReportView";
type StepNode = Node<{ step: WorkflowStep }, "step">;
function StepCard({ data, selected }: NodeProps<StepNode>) {
  return (
    <div className={`workflow-node ${selected ? "selected" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <span className="node-actor">
        {data.step.actor || "Actor not established"}
      </span>
      <strong>{data.step.title}</strong>
      <span className="node-system">
        {data.step.system || "System not established"}
      </span>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
const nodeTypes = { step: StepCard };
export function buildWorkflowGraph(
  steps: WorkflowStep[],
  edges: WorkflowEdge[],
): { nodes: StepNode[]; edges: Edge[] } {
  const graph = new dagre.graphlib.Graph({
    multigraph: true,
  }).setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: "LR",
    ranksep: 100,
    nodesep: 48,
    marginx: 24,
    marginy: 24,
  });
  const ids = new Set(steps.map((step) => step.id));
  steps.forEach((step) => graph.setNode(step.id, { width: 244, height: 130 }));
  const valid = edges.filter(
    (edge) => ids.has(edge.source) && ids.has(edge.target),
  );
  valid.forEach((edge) => graph.setEdge(edge.source, edge.target, {}, edge.id));
  dagre.layout(graph);
  return {
    nodes: steps.map((step) => ({
      id: step.id,
      type: "step",
      position: {
        x: graph.node(step.id).x - 122,
        y: graph.node(step.id).y - 65,
      },
      data: { step },
      ariaLabel: `${step.title}, ${step.actor}, ${step.system}`,
    })),
    edges: valid.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: "smoothstep",
      label: edge.label || edge.kind,
      markerEnd: {
        type: MarkerType.ArrowClosed,
        color: edge.kind === "rework" ? "#D94A8E" : "#596064",
      },
      style: {
        stroke: edge.kind === "rework" ? "#D94A8E" : "#596064",
        strokeWidth: 1.5,
        ...(edge.kind === "conditional" ? { strokeDasharray: "5 4" } : {}),
      },
      labelStyle: { fill: "#2D3436", fontSize: 12 },
      labelBgStyle: { fill: "#FFFDF9" },
      ariaLabel: `${edge.kind}: ${edge.label}`,
    })),
  };
}
export default function WorkflowMap({
  steps,
  edges,
  evidence,
}: {
  steps: WorkflowStep[];
  edges: WorkflowEdge[];
  evidence: Evidence[];
}) {
  const graph = useMemo(() => buildWorkflowGraph(steps, edges), [steps, edges]);
  const [selected, setSelected] = useState<string | null>(null);
  const step = steps.find((step) => step.id === selected);
  if (steps.length === 0)
    return (
      <EmptyState title="A workflow has not been established.">
        A map will appear when the review has evidence-linked steps. No generic
        process is substituted.
      </EmptyState>
    );
  return (
    <div className="workflow-view">
      <div className="map-instruction">
        Select a step to inspect its evidence. Pan or use the zoom controls.
        Rework routes are magenta.
      </div>
      <div className="flow-canvas" aria-label="Business workflow map">
        <ReactFlow
          nodes={graph.nodes.map((node) => ({
            ...node,
            selected: node.id === selected,
          }))}
          onNodesChange={(changes) => {
            for (const change of changes) {
              if (change.type === "select" && change.selected)
                setSelected(change.id);
            }
          }}
          edges={graph.edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.25 }}
          minZoom={0.15}
          maxZoom={1.5}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesReconnectable={false}
          onNodeClick={(_event, node) => setSelected(node.id)}
        >
          <Background
            variant={BackgroundVariant.Dots}
            color="#D5D4D0"
            gap={24}
          />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <details className="map-text-alternative">
        <summary>Read the workflow as a list</summary>
        <ol>
          {steps.map((item) => (
            <li key={item.id}>
              <button
                className="text-button"
                onClick={() => setSelected(item.id)}
              >
                {item.title}
              </button>
              <p>{item.description}</p>
              <span className="fine-print">
                {item.actor} · {item.system}
              </span>
            </li>
          ))}
        </ol>
        <h4>Routes</h4>
        <ul>
          {edges.map((edge) => (
            <li key={edge.id}>
              {steps.find((s) => s.id === edge.source)?.title} →{" "}
              {steps.find((s) => s.id === edge.target)?.title} · {edge.kind}:{" "}
              {edge.label}
            </li>
          ))}
        </ul>
      </details>
      {step && (
        <section className="step-inspector" aria-label="Selected workflow step">
          <div className="section-heading">
            <div>
              <div className="eyebrow">Selected step</div>
              <h3>{step.title}</h3>
            </div>
            <button className="button" onClick={() => setSelected(null)}>
              Close details
            </button>
          </div>
          <p>{step.description}</p>
          <p className="muted">
            {step.actor} · {step.system}
          </p>
          <EvidenceList evidence={evidence} selectedIds={step.evidence_ids} />
        </section>
      )}
    </div>
  );
}
