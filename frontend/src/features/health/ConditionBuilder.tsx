import { useEffect, useState } from "react";

import {
  COUNT_OPERATORS,
  EXISTENCE_OPERATORS,
  LIST_ELEMENT_OPERATORS,
  SET_OPERATORS,
  isAllOf,
  isAnyOf,
  isNot,
  operatorsForMetricType,
} from "@/types/health";
import type { Condition, HealthMetricResponse, MetricType, Operator } from "@/types/health";

/**
 * MVP visual condition builder: either ONE leaf, or a single top-level
 * `all_of`/`any_of` group of leaves — no deep nesting, no `not`. The full
 * recursive grammar is still reachable via the "edit as JSON" escape
 * hatch below; this only covers the shapes admins build day to day.
 */

interface LeafDraft {
  metric: string;
  operator: Operator | "";
  /** Raw text as typed; parsed into the right JS type (number/string/
   * array/boolean) only when building the outgoing `Condition`. */
  value: string;
  /** COUNT_* only: optional "where equals" element filter. */
  equals: string;
}

type BuilderState =
  | { kind: "single"; leaf: LeafDraft }
  | { kind: "group"; group_op: "all_of" | "any_of"; leaves: LeafDraft[] };

function emptyLeaf(defaultMetric = ""): LeafDraft {
  return { metric: defaultMetric, operator: "", value: "", equals: "" };
}

function formatForInput(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.join(", ");
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}

function leafDraftFromCondition(c: Condition): LeafDraft {
  return {
    metric: c.metric ?? "",
    operator: (c.operator as Operator | null) ?? "",
    value: formatForInput(c.value),
    equals: formatForInput(c.equals),
  };
}

/** A node this builder can render as one row: a real leaf (`metric` set),
 * or an entirely-empty/unset node (`{}`) — the sentinel a brand-new
 * policy's draft condition starts as, before the author has picked
 * anything. Anything that's itself a group or a `not` is NOT simple. */
function isSimpleNode(c: Condition): boolean {
  return !isAllOf(c) && !isAnyOf(c) && !isNot(c);
}

/** Whether `condition` fits this builder's MVP shape: a single leaf (or
 * the empty "nothing picked yet" sentinel), or a one-level `all_of`/
 * `any_of` group whose children are all simple leaves — no deep nesting,
 * no `not`. Used both to derive the initial builder draft and to decide
 * whether an existing policy's condition must open in JSON mode instead. */
function conditionFitsVisualShape(condition: Condition): boolean {
  if (isNot(condition)) {
    return false;
  }
  if (isAllOf(condition)) {
    return condition.all_of.every(isSimpleNode);
  }
  if (isAnyOf(condition)) {
    return condition.any_of.every(isSimpleNode);
  }
  return true;
}

function draftFromCondition(condition: Condition): BuilderState {
  if (isAllOf(condition) && condition.all_of.every(isSimpleNode)) {
    return { kind: "group", group_op: "all_of", leaves: condition.all_of.map(leafDraftFromCondition) };
  }
  if (isAnyOf(condition) && condition.any_of.every(isSimpleNode)) {
    return { kind: "group", group_op: "any_of", leaves: condition.any_of.map(leafDraftFromCondition) };
  }
  if (isSimpleNode(condition)) {
    return { kind: "single", leaf: leafDraftFromCondition(condition) };
  }
  return { kind: "single", leaf: emptyLeaf() };
}

function isLeafComplete(leaf: LeafDraft): boolean {
  if (leaf.metric === "" || leaf.operator === "") {
    return false;
  }
  if (EXISTENCE_OPERATORS.has(leaf.operator)) {
    return true;
  }
  return leaf.value.trim() !== "";
}

function isBuilderComplete(state: BuilderState): boolean {
  if (state.kind === "single") {
    return isLeafComplete(state.leaf);
  }
  return state.leaves.length > 0 && state.leaves.every(isLeafComplete);
}

/** The element type to parse a single scalar against: for list metrics
 * this is the *element* type (LIST_INT's elements are numbers), for
 * scalar metrics it's the metric's own type. */
function scalarParseKind(metricType: MetricType | undefined): "number" | "boolean" | "string" {
  if (metricType === "LIST_INT" || metricType === "INT" || metricType === "FLOAT") {
    return "number";
  }
  if (metricType === "BOOL") {
    return "boolean";
  }
  return "string";
}

function parseScalar(raw: string, kind: "number" | "boolean" | "string"): unknown {
  if (kind === "number") {
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  if (kind === "boolean") {
    return raw === "true";
  }
  return raw;
}

function parseList(raw: string, kind: "number" | "boolean" | "string"): unknown[] {
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => parseScalar(s, kind));
}

function buildLeafCondition(leaf: LeafDraft, metrics: HealthMetricResponse[]): Condition {
  const metric = metrics.find((m) => m.name === leaf.metric);
  const operator = leaf.operator as Operator;
  const condition: Condition = { metric: leaf.metric, operator };

  if (EXISTENCE_OPERATORS.has(operator)) {
    return condition;
  }
  if (COUNT_OPERATORS.has(operator)) {
    condition.value = parseScalar(leaf.value, "number");
    if (leaf.equals.trim() !== "") {
      condition.equals = parseScalar(leaf.equals, scalarParseKind(metric?.type));
    }
    return condition;
  }
  if (SET_OPERATORS.has(operator)) {
    const kind = metric?.type === "INT" || metric?.type === "FLOAT" ? "number" : "string";
    condition.value = parseList(leaf.value, kind);
    return condition;
  }
  if (LIST_ELEMENT_OPERATORS.has(operator)) {
    condition.value = parseScalar(leaf.value, scalarParseKind(metric?.type));
    return condition;
  }
  condition.value = parseScalar(leaf.value, scalarParseKind(metric?.type));
  return condition;
}

function builderStateToCondition(state: BuilderState, metrics: HealthMetricResponse[]): Condition {
  if (state.kind === "single") {
    return buildLeafCondition(state.leaf, metrics);
  }
  const leaves = state.leaves.map((leaf) => buildLeafCondition(leaf, metrics));
  return state.group_op === "all_of" ? { all_of: leaves } : { any_of: leaves };
}

function groupMetricsByCategory(metrics: HealthMetricResponse[]): [string, HealthMetricResponse[]][] {
  const byCategory = new Map<string, HealthMetricResponse[]>();
  for (const metric of metrics) {
    const bucket = byCategory.get(metric.category) ?? [];
    bucket.push(metric);
    byCategory.set(metric.category, bucket);
  }
  return [...byCategory.entries()].sort(([a], [b]) => a.localeCompare(b));
}

interface LeafRowProps {
  leaf: LeafDraft;
  metrics: HealthMetricResponse[];
  onChange: (leaf: LeafDraft) => void;
  onRemove?: () => void;
}

function LeafRow({ leaf, metrics, onChange, onRemove }: LeafRowProps) {
  const metric = metrics.find((m) => m.name === leaf.metric);
  const availableOperators = metric ? operatorsForMetricType(metric.type) : [];
  const categories = groupMetricsByCategory(metrics);

  function handleMetricChange(name: string) {
    const nextMetric = metrics.find((m) => m.name === name);
    const validOperators = nextMetric ? operatorsForMetricType(nextMetric.type) : [];
    const nextOperator = validOperators.includes(leaf.operator as Operator) ? leaf.operator : "";
    onChange({ metric: name, operator: nextOperator, value: "", equals: "" });
  }

  const operator = leaf.operator;
  const showValue = operator !== "" && !EXISTENCE_OPERATORS.has(operator);
  const showEquals = operator !== "" && COUNT_OPERATORS.has(operator);
  const isSetOperator = operator !== "" && SET_OPERATORS.has(operator);
  const isNumericValue =
    operator !== "" &&
    (COUNT_OPERATORS.has(operator) ||
      metric?.type === "INT" ||
      metric?.type === "FLOAT" ||
      (LIST_ELEMENT_OPERATORS.has(operator) && metric?.type === "LIST_INT"));

  return (
    <div className="flex flex-wrap items-center gap-2 rounded border border-gray-200 p-2 dark:border-gray-700">
      <select
        aria-label="Metric"
        value={leaf.metric}
        onChange={(e) => {
          handleMetricChange(e.target.value);
        }}
        className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
      >
        <option value="" disabled>
          Select metric…
        </option>
        {categories.map(([category, categoryMetrics]) => (
          <optgroup key={category} label={category}>
            {categoryMetrics.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name}
              </option>
            ))}
          </optgroup>
        ))}
      </select>

      <select
        aria-label="Operator"
        value={leaf.operator}
        disabled={!metric}
        onChange={(e) => {
          onChange({ ...leaf, operator: e.target.value as Operator, value: "", equals: "" });
        }}
        className="rounded border border-gray-300 px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-900"
      >
        <option value="" disabled>
          Operator…
        </option>
        {availableOperators.map((op) => (
          <option key={op} value={op}>
            {op}
          </option>
        ))}
      </select>

      {showValue && !isSetOperator && metric?.type === "ENUM" && metric.enum_values ? (
        <select
          aria-label="Value"
          value={leaf.value}
          onChange={(e) => {
            onChange({ ...leaf, value: e.target.value });
          }}
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
        >
          <option value="" disabled>
            Value…
          </option>
          {metric.enum_values.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      ) : (
        showValue && (
          <input
            aria-label={isSetOperator ? "Values (comma-separated)" : "Value"}
            type={!isSetOperator && isNumericValue ? "number" : "text"}
            placeholder={isSetOperator ? "comma,separated,values" : undefined}
            value={leaf.value}
            onChange={(e) => {
              onChange({ ...leaf, value: e.target.value });
            }}
            className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          />
        )
      )}

      {showEquals && (
        <input
          aria-label="Where equals (optional)"
          type="text"
          placeholder="where equals (optional)"
          value={leaf.equals}
          onChange={(e) => {
            onChange({ ...leaf, equals: e.target.value });
          }}
          className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
        />
      )}

      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 dark:border-red-800 dark:text-red-400"
        >
          Remove
        </button>
      )}
    </div>
  );
}

interface VisualBuilderProps {
  state: BuilderState;
  metrics: HealthMetricResponse[];
  onChange: (state: BuilderState) => void;
}

function VisualBuilder({ state, metrics, onChange }: VisualBuilderProps) {
  function switchToGroup(op: "all_of" | "any_of") {
    if (state.kind === "single") {
      onChange({ kind: "group", group_op: op, leaves: [state.leaf, emptyLeaf()] });
    } else {
      onChange({ ...state, group_op: op });
    }
  }

  function switchToSingle() {
    const first = state.kind === "group" ? state.leaves[0] : state.leaf;
    onChange({ kind: "single", leaf: first ?? emptyLeaf() });
  }

  return (
    <div className="mt-2 space-y-2">
      <div className="flex items-center gap-4 text-xs text-gray-500">
        <label className="flex items-center gap-1">
          <input type="radio" checked={state.kind === "single"} onChange={switchToSingle} />
          Single condition
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            checked={state.kind === "group" && state.group_op === "all_of"}
            onChange={() => {
              switchToGroup("all_of");
            }}
          />
          All of (AND)
        </label>
        <label className="flex items-center gap-1">
          <input
            type="radio"
            checked={state.kind === "group" && state.group_op === "any_of"}
            onChange={() => {
              switchToGroup("any_of");
            }}
          />
          Any of (OR)
        </label>
      </div>

      {state.kind === "single" ? (
        <LeafRow
          leaf={state.leaf}
          metrics={metrics}
          onChange={(leaf) => {
            onChange({ kind: "single", leaf });
          }}
        />
      ) : (
        <div className="space-y-2">
          {state.leaves.map((leaf, index) => (
            // eslint-disable-next-line react/no-array-index-key -- leaves have no stable id in this MVP builder
            <LeafRow
              key={index}
              leaf={leaf}
              metrics={metrics}
              onChange={(next) => {
                const leaves = state.leaves.map((l, i) => (i === index ? next : l));
                onChange({ ...state, leaves });
              }}
              {...(state.leaves.length > 1
                ? {
                    onRemove: () => {
                      onChange({ ...state, leaves: state.leaves.filter((_, i) => i !== index) });
                    },
                  }
                : {})}
            />
          ))}
          <button
            type="button"
            onClick={() => {
              onChange({ ...state, leaves: [...state.leaves, emptyLeaf()] });
            }}
            className="text-xs text-blue-600 hover:underline dark:text-blue-400"
          >
            + Add condition
          </button>
        </div>
      )}
    </div>
  );
}

interface ConditionBuilderProps {
  metrics: HealthMetricResponse[];
  initialCondition: Condition;
  onChange: (condition: Condition | null) => void;
}

/** Controlled-on-mount, then self-managed: the draft lives entirely in
 * local state after the initial render (matching `RuleEditorPage`'s
 * approach of syncing from a prop only via `key`-based remount, not a
 * live-diffing effect) — `onChange` is called directly from every
 * mutation site instead of via a `useEffect` watching `onChange` itself,
 * since `onChange` is typically a fresh closure every parent render and
 * depending on it would either re-fire spuriously or need an
 * exhaustive-deps suppression on every change, not just mount. */
export function ConditionBuilder({ metrics, initialCondition, onChange }: ConditionBuilderProps) {
  const [fitsVisual] = useState(() => conditionFitsVisualShape(initialCondition));
  const [mode, setMode] = useState<"visual" | "json">(fitsVisual ? "visual" : "json");
  const [builderState, setBuilderState] = useState<BuilderState>(() =>
    draftFromCondition(initialCondition),
  );
  const [jsonText, setJsonText] = useState<string>(() => JSON.stringify(initialCondition, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  // Sync the parent's condition state with this component's derived
  // initial draft exactly once on mount. Every subsequent change is
  // emitted directly from the mutation site (`emitFromBuilder`,
  // `handleJsonBlur`, `switchToVisual`) rather than re-derived here, so
  // this intentionally does not depend on `onChange`/`builderState`.
  useEffect(() => {
    onChange(isBuilderComplete(builderState) ? builderStateToCondition(builderState, metrics) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function emitFromBuilder(next: BuilderState) {
    setBuilderState(next);
    onChange(isBuilderComplete(next) ? builderStateToCondition(next, metrics) : null);
  }

  function handleJsonBlur() {
    try {
      const parsed = JSON.parse(jsonText) as Condition;
      setJsonError(null);
      onChange(parsed);
    } catch {
      setJsonError("Invalid JSON — this condition will not be saved until it's fixed.");
      onChange(null);
    }
  }

  function switchToJson() {
    const complete = isBuilderComplete(builderState);
    const current = complete ? builderStateToCondition(builderState, metrics) : initialCondition;
    setJsonText(JSON.stringify(current, null, 2));
    setJsonError(null);
    setMode("json");
  }

  function switchToVisual() {
    try {
      const parsed = JSON.parse(jsonText) as Condition;
      if (conditionFitsVisualShape(parsed)) {
        const next = draftFromCondition(parsed);
        setBuilderState(next);
        onChange(isBuilderComplete(next) ? builderStateToCondition(next, metrics) : null);
      }
      // If it doesn't fit the visual shape, keep the existing visual draft
      // (deliberately not overwritten with a lossy conversion) and let the
      // JSON textarea remain the source of truth until the mode is
      // switched again.
    } catch {
      // Invalid JSON: nothing to convert, keep the existing visual draft.
    }
    setJsonError(null);
    setMode("visual");
  }

  return (
    <div>
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium text-gray-500">Condition</p>
        <button
          type="button"
          onClick={mode === "visual" ? switchToJson : switchToVisual}
          className="text-xs text-blue-600 hover:underline dark:text-blue-400"
        >
          {mode === "visual" ? "Advanced: edit as JSON" : "Back to visual builder"}
        </button>
      </div>

      {!fitsVisual && mode === "json" && (
        <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
          This condition uses nesting or "not" beyond what the visual builder supports, so it
          opened in JSON mode.
        </p>
      )}

      {mode === "visual" ? (
        <VisualBuilder state={builderState} metrics={metrics} onChange={emitFromBuilder} />
      ) : (
        <div className="mt-2">
          <textarea
            value={jsonText}
            onChange={(e) => {
              setJsonText(e.target.value);
            }}
            onBlur={handleJsonBlur}
            rows={10}
            className="w-full rounded border border-gray-300 px-2 py-1 font-mono text-xs dark:border-gray-600 dark:bg-gray-900"
          />
          {jsonError && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{jsonError}</p>}
        </div>
      )}
    </div>
  );
}
