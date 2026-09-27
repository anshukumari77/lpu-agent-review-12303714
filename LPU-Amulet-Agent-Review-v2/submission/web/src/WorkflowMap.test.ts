import { describe, it, expect } from "vitest";
import { buildWorkflowGraph } from "./WorkflowMap";
const steps = [
  {
    id: "a",
    title: "Inspect request",
    actor: "Coordinator",
    system: "Inbox",
    description: "Read the request",
    evidence_ids: ["e1"],
  },
  {
    id: "b",
    title: "Approve",
    actor: "Owner",
    system: "Approval register",
    description: "Review the decision",
    evidence_ids: [],
  },
];
describe("workflow data, not execution code", () => {
  it("renders stable nodes and typed rework edges without evaluating content", () => {
    const graph = buildWorkflowGraph(steps, [
      { id: "ab", source: "a", target: "b", kind: "next", label: "Send" },
      { id: "ba", source: "b", target: "a", kind: "rework", label: "Revise" },
    ]);
    expect(graph.nodes).toHaveLength(2);
    expect(graph.edges).toHaveLength(2);
    expect(graph.nodes[0].data.step.evidence_ids).toEqual(["e1"]);
    expect(graph.edges.find((edge) => edge.id === "ba")?.label).toBe("Revise");
    expect(
      graph.nodes.every(
        (node) =>
          Number.isFinite(node.position.x) && Number.isFinite(node.position.y),
      ),
    ).toBe(true);
  });
});
