import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConditionBuilder } from "@/features/health/ConditionBuilder";
import type { Condition, HealthMetricResponse } from "@/types/health";

const INT_METRIC: HealthMetricResponse = {
  name: "cpu.socket_count",
  type: "INT",
  category: "cpu",
  description: "Number of populated CPU sockets",
  enum_values: null,
  provider: "core",
};

const LIST_STRING_METRIC: HealthMetricResponse = {
  name: "network.interface_link_states",
  type: "LIST_STRING",
  category: "network",
  description: "Link state reported per network interface",
  enum_values: null,
  provider: "core",
};

const METRICS = [INT_METRIC, LIST_STRING_METRIC];

function getOptionValues(select: HTMLElement): string[] {
  return [...(select as HTMLSelectElement).options].map((o) => o.value).filter((v) => v !== "");
}

describe("ConditionBuilder", () => {
  it("filters the operator dropdown to numeric operators for an INT metric", () => {
    render(<ConditionBuilder metrics={METRICS} initialCondition={{}} onChange={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "cpu.socket_count" } });

    const operators = getOptionValues(screen.getByLabelText("Operator"));
    expect(operators).toEqual(expect.arrayContaining(["EQ", "NE", "GT", "GTE", "LT", "LTE", "IN", "NOT_IN"]));
    expect(operators).not.toContain("ANY");
    expect(operators).not.toContain("COUNT_EQ");
  });

  it("filters the operator dropdown to list operators for a LIST_STRING metric, excluding GT", () => {
    render(<ConditionBuilder metrics={METRICS} initialCondition={{}} onChange={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Metric"), {
      target: { value: "network.interface_link_states" },
    });

    const operators = getOptionValues(screen.getByLabelText("Operator"));
    expect(operators).toEqual(
      expect.arrayContaining(["ANY", "ALL", "COUNT_EQ", "COUNT_GT", "EXISTS", "NOT_EXISTS"]),
    );
    expect(operators).not.toContain("GT");
    expect(operators).not.toContain("IN");
  });

  it("resets the operator when switching to a metric that doesn't support it", () => {
    const initialCondition: Condition = { metric: "cpu.socket_count", operator: "GT", value: 2 };
    render(<ConditionBuilder metrics={METRICS} initialCondition={initialCondition} onChange={vi.fn()} />);

    expect(screen.getByLabelText("Operator")).toHaveValue("GT");

    fireEvent.change(screen.getByLabelText("Metric"), {
      target: { value: "network.interface_link_states" },
    });

    // GT is not valid for LIST_STRING, so the operator resets to unset.
    expect(screen.getByLabelText("Operator")).toHaveValue("");
  });

  it("renders a JSON textarea with the current condition when switching to advanced mode", () => {
    const initialCondition: Condition = { metric: "cpu.socket_count", operator: "GT", value: 2 };
    render(<ConditionBuilder metrics={METRICS} initialCondition={initialCondition} onChange={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Advanced: edit as JSON" }));

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    const parsed = JSON.parse(textarea.value) as Condition;
    expect(parsed.metric).toBe("cpu.socket_count");
    expect(parsed.operator).toBe("GT");
    expect(parsed.value).toBe(2);
  });

  it("auto-switches to JSON mode for a condition using `not`, with an explanatory note", () => {
    const initialCondition: Condition = { not: { metric: "cpu.socket_count", operator: "GT", value: 2 } };
    render(<ConditionBuilder metrics={METRICS} initialCondition={initialCondition} onChange={vi.fn()} />);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(
      screen.getByText(
        'This condition uses nesting or "not" beyond what the visual builder supports, so it opened in JSON mode.',
      ),
    ).toBeInTheDocument();
  });
});
