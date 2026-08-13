import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError } from "@/api/client";
import { ConditionBuilder } from "@/features/health/ConditionBuilder";
import { HistoryPanel } from "@/features/events/HistoryPanel";
import {
  useCreateHealthPolicyMutation,
  useHealthMetricsQuery,
  useHealthPoliciesQuery,
  useHealthPolicyQuery,
  useUpdateHealthPolicyMutation,
} from "@/features/health/hooks";
import { PreviewPanel } from "@/features/health/PreviewPanel";
import { ShadowPanel } from "@/features/health/ShadowPanel";
import { useDebouncedValue } from "@/lib/useDebouncedValue";
import { PRIORITY_BANDS, RULE_SOURCES_FOR_CREATE } from "@/types/classification";
import type { ManagerType, RuleSource } from "@/types/classification";
import {
  POLICY_CATEGORIES,
  emptyPolicyScope,
} from "@/types/health";
import type {
  Condition,
  EvidenceField,
  HealthPolicyCreate,
  HealthPolicyPreviewRequest,
  HealthPolicyResponse,
  HealthPolicyUpdate,
  PolicyMode,
  PolicyScope,
} from "@/types/health";
import type { HealthSeverity, Vendor } from "@/types/server";
import { HEALTH_POLICY_EVENT_TYPES } from "@/types/events";

const SEVERITIES: HealthSeverity[] = ["UNKNOWN", "HEALTHY", "INFO", "WARNING", "CRITICAL"];
const MODES: PolicyMode[] = ["EVALUATE", "SUPPRESS"];
const VENDORS: Vendor[] = ["dell", "cisco", "hpe", "unknown"];
const MANAGER_TYPES: ManagerType[] = [
  "OPENMANAGE",
  "UCS_MANAGER",
  "UCS_CENTRAL",
  "INTERSIGHT",
  "ONEVIEW",
];

interface PolicyFormState {
  name: string;
  description: string;
  enabled: boolean;
  policy_key: string;
  mode: PolicyMode;
  category: string;
  severity: HealthSeverity;
  evidence: EvidenceField[];
  message_template: string;
  scope: PolicyScope;
  source: RuleSource;
  priority: number;
  order: number;
}

function initialFormState(): PolicyFormState {
  return {
    name: "",
    description: "",
    enabled: true,
    policy_key: "",
    mode: "EVALUATE",
    category: POLICY_CATEGORIES[0],
    severity: "WARNING",
    evidence: [],
    message_template: "",
    scope: emptyPolicyScope(),
    source: "GLOBAL_CUSTOM",
    priority: PRIORITY_BANDS.GLOBAL_CUSTOM.low,
    order: 0,
  };
}

function formStateFromPolicy(policy: HealthPolicyResponse): PolicyFormState {
  return {
    name: policy.name,
    description: policy.description,
    enabled: policy.enabled,
    policy_key: policy.policy_key,
    mode: policy.mode === "SUPPRESS" ? "SUPPRESS" : "EVALUATE",
    category: policy.category,
    severity: policy.severity,
    evidence: policy.evidence,
    message_template: policy.message_template,
    scope: policy.scope,
    source: policy.source,
    priority: policy.priority,
    order: policy.order,
  };
}

function requiredScopeField(source: RuleSource): "vendor" | "manager_type" | "site_id" | null {
  if (source === "SITE_CUSTOM") return "site_id";
  if (source === "MANAGER_CUSTOM") return "manager_type";
  if (source === "VENDOR_CUSTOM") return "vendor";
  return null;
}

function scopeForSource(source: RuleSource, previous: PolicyScope): PolicyScope {
  const required = requiredScopeField(source);
  return {
    vendor: required === "vendor" ? previous.vendor : null,
    manager_type: required === "manager_type" ? previous.manager_type : null,
    site_id: required === "site_id" ? previous.site_id : null,
  };
}

function formatApiError(error: unknown): string {
  if (error instanceof ApiError) {
    const detailParts = Object.entries(error.problem.details)
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
      .join("; ");
    return detailParts ? `${error.problem.detail} (${detailParts})` : error.problem.detail;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "The request failed.";
}

export function PolicyEditorPage() {
  const { id } = useParams<{ id: string }>();
  const isEdit = id !== undefined;
  const navigate = useNavigate();

  const {
    data: existingPolicy,
    isPending: isLoadingPolicy,
    isError: isPolicyError,
    error: policyError,
  } = useHealthPolicyQuery(id ?? "");
  const { data: metricsData, isPending: isLoadingMetrics } = useHealthMetricsQuery();
  const { data: allPoliciesData } = useHealthPoliciesQuery({});

  const createMutation = useCreateHealthPolicyMutation();
  const updateMutation = useUpdateHealthPolicyMutation();

  const [form, setForm] = useState<PolicyFormState>(initialFormState());
  const [condition, setCondition] = useState<Condition | null>(null);
  const [conditionError, setConditionError] = useState<string | null>(null);

  useEffect(() => {
    if (existingPolicy) {
      setForm(formStateFromPolicy(existingPolicy));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingPolicy?.id, existingPolicy?.revision]);

  const isSystem = existingPolicy?.system ?? false;
  const locked = isEdit && isSystem;
  const metrics = metricsData?.items ?? [];
  const allPolicies = useMemo(() => allPoliciesData?.items ?? [], [allPoliciesData]);

  const requiredScope = requiredScopeField(form.source);
  const band = PRIORITY_BANDS[form.source];

  function updateField<K extends keyof PolicyFormState>(key: K, value: PolicyFormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function handleSourceChange(source: RuleSource) {
    setForm((prev) => ({
      ...prev,
      source,
      scope: scopeForSource(source, prev.scope),
      priority: PRIORITY_BANDS[source].low,
    }));
  }

  function addEvidenceRow() {
    setForm((prev) => ({
      ...prev,
      evidence: [...prev.evidence, { key: "", metric: metrics[0]?.name ?? "" }],
    }));
  }

  function updateEvidenceRow(index: number, next: EvidenceField) {
    setForm((prev) => ({
      ...prev,
      evidence: prev.evidence.map((e, i) => (i === index ? next : e)),
    }));
  }

  function removeEvidenceRow(index: number) {
    setForm((prev) => ({ ...prev, evidence: prev.evidence.filter((_, i) => i !== index) }));
  }

  const rawPreviewRequest = useMemo<HealthPolicyPreviewRequest | null>(() => {
    if (condition === null || !form.message_template.trim() || !form.category) {
      return null;
    }
    const request: HealthPolicyPreviewRequest = {
      name: form.name || "(draft)",
      description: form.description,
      enabled: form.enabled,
      mode: form.mode,
      category: form.category,
      severity: form.severity,
      condition,
      evidence: form.evidence,
      message_template: form.message_template,
      scope: form.scope,
      source: form.source,
      priority: form.priority,
      order: form.order,
    };
    if (form.policy_key) {
      request.policy_key = form.policy_key;
    }
    if (isEdit && id) {
      request.policy_id = id;
    }
    return request;
  }, [condition, form, isEdit, id]);

  const debouncedPreviewRequest = useDebouncedValue(rawPreviewRequest, 400);

  const existingPolicyKeys = useMemo(() => {
    const keys = new Set(allPolicies.map((p) => p.policy_key));
    return [...keys].sort((a, b) => a.localeCompare(b));
  }, [allPolicies]);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();

    if (locked) {
      if (id) {
        updateMutation.mutate(
          { id, body: { enabled: form.enabled } },
          { onSuccess: () => navigate("/health-policies") },
        );
      }
      return;
    }

    if (condition === null) {
      setConditionError("The condition is incomplete — fill in every leaf before saving.");
      return;
    }
    setConditionError(null);

    const basePayload = {
      name: form.name,
      description: form.description,
      enabled: form.enabled,
      policy_key: form.policy_key || null,
      mode: form.mode,
      category: form.category,
      severity: form.severity,
      condition,
      evidence: form.evidence,
      message_template: form.message_template,
      scope: form.scope,
      source: form.source,
      priority: form.priority,
      order: form.order,
    };

    if (isEdit && id) {
      const body: HealthPolicyUpdate = basePayload;
      updateMutation.mutate({ id, body }, { onSuccess: () => navigate("/health-policies") });
    } else {
      const body: HealthPolicyCreate = basePayload;
      createMutation.mutate(body, { onSuccess: () => navigate("/health-policies") });
    }
  }

  const saveMutation = isEdit ? updateMutation : createMutation;
  const inputClass =
    "mt-1 w-full rounded border border-gray-300 px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-900";
  const labelClass = "flex flex-col text-xs font-medium text-gray-500";

  return (
    <main className="mx-auto max-w-5xl p-8">
      <Link to="/health-policies" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
        ← Back to health policies
      </Link>

      <h1 className="mt-4 text-2xl font-semibold">
        {isEdit ? "Edit Health Policy" : "New Health Policy"}
      </h1>

      {isEdit && isLoadingPolicy && <p className="mt-4 text-gray-500">Loading policy…</p>}

      {isEdit && isPolicyError && (
        <p className="mt-4 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
          {formatApiError(policyError)}
        </p>
      )}

      {isLoadingMetrics && <p className="mt-4 text-gray-500">Loading metric registry…</p>}

      {(!isEdit || existingPolicy) && !isLoadingMetrics && (
        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[2fr_1fr]">
          <form
            onSubmit={handleSubmit}
            className="space-y-4 rounded-lg border border-gray-200 p-4 dark:border-gray-700"
          >
            {locked && (
              <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
                This is a system policy. Only "Enabled" can be changed — every other field is
                locked.
              </p>
            )}

            {saveMutation.isError && (
              <p className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
                {formatApiError(saveMutation.error)}
              </p>
            )}

            {conditionError && (
              <p className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
                {conditionError}
              </p>
            )}

            <label className={labelClass}>
              Name
              <input
                type="text"
                required
                disabled={locked}
                value={form.name}
                onChange={(e) => {
                  updateField("name", e.target.value);
                }}
                className={inputClass}
              />
            </label>

            <label className={labelClass}>
              Description
              <textarea
                disabled={locked}
                value={form.description}
                onChange={(e) => {
                  updateField("description", e.target.value);
                }}
                className={inputClass}
                rows={2}
              />
            </label>

            <label className="flex items-center gap-2 text-xs font-medium text-gray-500">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => {
                  updateField("enabled", e.target.checked);
                }}
              />
              Enabled
            </label>

            <label className={labelClass}>
              policy_key
              <input
                type="text"
                disabled={locked}
                list="policy-key-suggestions"
                placeholder="leave blank to start a new family (defaults to this policy's own id)"
                value={form.policy_key}
                onChange={(e) => {
                  updateField("policy_key", e.target.value);
                }}
                className={inputClass}
              />
              <datalist id="policy-key-suggestions">
                {existingPolicyKeys.map((key) => (
                  <option key={key} value={key} />
                ))}
              </datalist>
            </label>

            <ShadowPanel
              policies={allPolicies}
              policyKey={form.policy_key}
              {...(existingPolicy ? { currentId: existingPolicy.id } : {})}
            />

            <div className="grid grid-cols-3 gap-4">
              <label className={labelClass}>
                Mode
                <select
                  disabled={locked}
                  value={form.mode}
                  onChange={(e) => {
                    updateField("mode", e.target.value as PolicyMode);
                  }}
                  className={inputClass}
                >
                  {MODES.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>

              <label className={labelClass}>
                Category
                <select
                  disabled={locked}
                  value={form.category}
                  onChange={(e) => {
                    updateField("category", e.target.value);
                  }}
                  className={inputClass}
                >
                  {POLICY_CATEGORIES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>

              <label className={labelClass}>
                Severity
                <select
                  disabled={locked}
                  value={form.severity}
                  onChange={(e) => {
                    updateField("severity", e.target.value as HealthSeverity);
                  }}
                  className={inputClass}
                >
                  {SEVERITIES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <label className={labelClass}>
                Source
                <select
                  disabled={locked}
                  value={form.source}
                  onChange={(e) => {
                    handleSourceChange(e.target.value as RuleSource);
                  }}
                  className={inputClass}
                >
                  {RULE_SOURCES_FOR_CREATE.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                  {isSystem && <option value="SYSTEM_DEFAULT">SYSTEM_DEFAULT</option>}
                </select>
              </label>

              <label className={labelClass}>
                Priority
                <input
                  type="number"
                  required
                  disabled={locked}
                  value={form.priority}
                  onChange={(e) => {
                    updateField("priority", Number(e.target.value));
                  }}
                  className={inputClass}
                />
                <span className="mt-1 text-xs text-gray-400">
                  {form.source}: {band.low}-{band.high}
                </span>
              </label>
            </div>

            <div>
              <p className="text-xs font-medium text-gray-500">Scope</p>
              {requiredScope === null ? (
                <p className="mt-1 text-sm text-gray-400">
                  {form.source} policies have no scope (unscoped).
                </p>
              ) : (
                <div className="mt-1">
                  {requiredScope === "vendor" && (
                    <label className={labelClass}>
                      Vendor
                      <select
                        required
                        disabled={locked}
                        value={form.scope.vendor ?? ""}
                        onChange={(e) => {
                          updateField("scope", { ...form.scope, vendor: e.target.value });
                        }}
                        className={inputClass}
                      >
                        <option value="" disabled>
                          Select a vendor…
                        </option>
                        {VENDORS.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {requiredScope === "manager_type" && (
                    <label className={labelClass}>
                      Manager type
                      <select
                        required
                        disabled={locked}
                        value={form.scope.manager_type ?? ""}
                        onChange={(e) => {
                          updateField("scope", {
                            ...form.scope,
                            manager_type: e.target.value as ManagerType,
                          });
                        }}
                        className={inputClass}
                      >
                        <option value="" disabled>
                          Select a manager type…
                        </option>
                        {MANAGER_TYPES.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {requiredScope === "site_id" && (
                    <label className={labelClass}>
                      Site ID
                      <input
                        type="text"
                        required
                        disabled={locked}
                        placeholder="site_..."
                        value={form.scope.site_id ?? ""}
                        onChange={(e) => {
                          updateField("scope", { ...form.scope, site_id: e.target.value });
                        }}
                        className={inputClass}
                      />
                    </label>
                  )}
                </div>
              )}
            </div>

            <div>
              {locked ? (
                <>
                  <p className="text-xs font-medium text-gray-500">Condition</p>
                  <pre className="mt-2 overflow-x-auto rounded border border-gray-200 bg-gray-50 p-2 text-xs dark:border-gray-700 dark:bg-gray-900">
                    {JSON.stringify(existingPolicy?.condition, null, 2)}
                  </pre>
                </>
              ) : (
                <ConditionBuilder
                  key={existingPolicy?.id ?? "new"}
                  metrics={metrics}
                  initialCondition={existingPolicy?.condition ?? {}}
                  onChange={setCondition}
                />
              )}
            </div>

            <label className={labelClass}>
              Message template
              <input
                type="text"
                required
                disabled={locked}
                placeholder="e.g. {down} UCS fabric path is down"
                value={form.message_template}
                onChange={(e) => {
                  updateField("message_template", e.target.value);
                }}
                className={inputClass}
              />
            </label>

            <div>
              <p className="text-xs font-medium text-gray-500">Evidence</p>
              <div className="mt-1 space-y-2">
                {form.evidence.map((row, index) => (
                  // eslint-disable-next-line react/no-array-index-key -- evidence rows have no stable id
                  <div key={index} className="flex items-center gap-2">
                    <input
                      type="text"
                      disabled={locked}
                      placeholder="key (used in {template})"
                      value={row.key}
                      onChange={(e) => {
                        updateEvidenceRow(index, { ...row, key: e.target.value });
                      }}
                      className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
                    />
                    <select
                      disabled={locked}
                      value={row.metric}
                      onChange={(e) => {
                        updateEvidenceRow(index, { ...row, metric: e.target.value });
                      }}
                      className="rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
                    >
                      {metrics.map((m) => (
                        <option key={m.name} value={m.name}>
                          {m.name}
                        </option>
                      ))}
                    </select>
                    {!locked && (
                      <button
                        type="button"
                        onClick={() => {
                          removeEvidenceRow(index);
                        }}
                        className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 dark:border-red-800 dark:text-red-400"
                      >
                        Remove
                      </button>
                    )}
                  </div>
                ))}
                {!locked && (
                  <button
                    type="button"
                    onClick={addEvidenceRow}
                    className="text-xs text-blue-600 hover:underline dark:text-blue-400"
                  >
                    + Add evidence field
                  </button>
                )}
              </div>
            </div>

            <button
              type="submit"
              disabled={saveMutation.isPending}
              className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saveMutation.isPending ? "Saving…" : "Save"}
            </button>
          </form>

          <div className="space-y-6">
            <PreviewPanel request={debouncedPreviewRequest} />
          </div>
        </div>
      )}

      {isEdit && id && existingPolicy && (
        <HistoryPanel eventTypes={HEALTH_POLICY_EVENT_TYPES} idField="policy_id" entityId={id} />
      )}
    </main>
  );
}
