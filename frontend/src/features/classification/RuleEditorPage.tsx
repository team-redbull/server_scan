import type { FormEvent } from "react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError } from "@/api/client";
import { HistoryPanel } from "@/features/events/HistoryPanel";
import {
  useClassificationRuleQuery,
  useCreateClassificationRuleMutation,
  useUpdateClassificationRuleMutation,
} from "@/features/classification/hooks";
import { PreviewPanel } from "@/features/classification/PreviewPanel";
import { useDebouncedValue } from "@/lib/useDebouncedValue";
import type { InstallationType, Vendor } from "@/types/server";
import {
  CLASSIFIABLE_FIELDS,
  PRIORITY_BANDS,
  RULE_SOURCES_FOR_CREATE,
  defaultRuleFlags,
  emptyRuleScope,
} from "@/types/classification";
import type {
  ClassificationPreviewRequest,
  ClassificationRuleCreate,
  ClassificationRuleResponse,
  ClassificationRuleUpdate,
  ManagerType,
  RuleFlags,
  RuleScope,
  RuleSource,
} from "@/types/classification";
import { CLASSIFICATION_EVENT_TYPES } from "@/types/events";

const INSTALLATION_TYPES: InstallationType[] = ["HOSTED_CLUSTER", "UPI", "UNCLASSIFIED"];
const VENDORS: Vendor[] = ["dell", "cisco", "hp", "standalone"];
const MANAGER_TYPES: ManagerType[] = [
  "OPENMANAGE",
  "UCS_MANAGER",
  "UCS_CENTRAL",
  "INTERSIGHT",
  "ONEVIEW",
];

interface RuleFormState {
  name: string;
  description: string;
  enabled: boolean;
  installation_type: InstallationType;
  source: RuleSource;
  priority: number;
  order: number;
  field: string;
  pattern: string;
  flags: RuleFlags;
  scope: RuleScope;
}

function initialFormState(): RuleFormState {
  return {
    name: "",
    description: "",
    enabled: true,
    installation_type: "HOSTED_CLUSTER",
    source: "GLOBAL_CUSTOM",
    priority: PRIORITY_BANDS.GLOBAL_CUSTOM.low,
    order: 0,
    field: "name",
    pattern: "",
    flags: defaultRuleFlags(),
    scope: emptyRuleScope(),
  };
}

function formStateFromRule(rule: ClassificationRuleResponse): RuleFormState {
  return {
    name: rule.name,
    description: rule.description,
    enabled: rule.enabled,
    installation_type: rule.installation_type,
    source: rule.source,
    priority: rule.priority,
    order: rule.order,
    field: rule.field,
    pattern: rule.pattern,
    flags: rule.flags,
    scope: rule.scope,
  };
}

/** Which single scope field a source requires (mirrors
 * `app.application.services.classification_service.validate_rule_write`).
 * `null` means the source must have an entirely empty scope. */
function requiredScopeField(source: RuleSource): "vendor" | "manager_type" | "site_id" | null {
  if (source === "SITE_CUSTOM") return "site_id";
  if (source === "MANAGER_CUSTOM") return "manager_type";
  if (source === "VENDOR_CUSTOM") return "vendor";
  return null;
}

function scopeForSource(source: RuleSource, previous: RuleScope): RuleScope {
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

export function RuleEditorPage() {
  const { id } = useParams<{ id: string }>();
  const isEdit = id !== undefined;
  const navigate = useNavigate();

  const {
    data: existingRule,
    isPending: isLoadingRule,
    isError: isRuleError,
    error: ruleError,
  } = useClassificationRuleQuery(id ?? "");

  const createMutation = useCreateClassificationRuleMutation();
  const updateMutation = useUpdateClassificationRuleMutation();

  const [form, setForm] = useState<RuleFormState>(initialFormState());

  // Sync form state once the existing rule loads (edit mode only). Keyed
  // on the rule's id/revision so a background refetch after a save doesn't
  // clobber in-progress edits with the server's echoed-back copy.
  useEffect(() => {
    if (existingRule) {
      setForm(formStateFromRule(existingRule));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existingRule?.id, existingRule?.revision]);

  const isSystem = existingRule?.system ?? false;
  const locked = isEdit && isSystem;

  const debouncedField = useDebouncedValue(form.field, 400);
  const debouncedPattern = useDebouncedValue(form.pattern, 400);

  const rawPreviewRequest = useMemo<ClassificationPreviewRequest | null>(() => {
    if (!debouncedField || !debouncedPattern) {
      return null;
    }
    return {
      installation_type: form.installation_type,
      scope: form.scope,
      field: debouncedField,
      pattern: debouncedPattern,
      flags: form.flags,
    };
  }, [debouncedField, debouncedPattern, form.installation_type, form.scope, form.flags]);

  const requiredScope = requiredScopeField(form.source);
  const band = PRIORITY_BANDS[form.source];

  function updateField<K extends keyof RuleFormState>(key: K, value: RuleFormState[K]) {
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

  function handleSubmit(e: FormEvent) {
    e.preventDefault();

    if (locked) {
      if (id) {
        updateMutation.mutate(
          { id, body: { enabled: form.enabled } },
          { onSuccess: () => navigate("/classification-rules") },
        );
      }
      return;
    }

    if (isEdit && id) {
      const body: ClassificationRuleUpdate = {
        name: form.name,
        description: form.description,
        enabled: form.enabled,
        installation_type: form.installation_type,
        scope: form.scope,
        field: form.field,
        pattern: form.pattern,
        flags: form.flags,
        source: form.source,
        priority: form.priority,
        order: form.order,
      };
      updateMutation.mutate({ id, body }, { onSuccess: () => navigate("/classification-rules") });
    } else {
      const body: ClassificationRuleCreate = {
        name: form.name,
        description: form.description,
        enabled: form.enabled,
        installation_type: form.installation_type,
        scope: form.scope,
        field: form.field,
        pattern: form.pattern,
        flags: form.flags,
        source: form.source,
        priority: form.priority,
        order: form.order,
      };
      createMutation.mutate(body, { onSuccess: () => navigate("/classification-rules") });
    }
  }

  const saveMutation = isEdit ? updateMutation : createMutation;
  const inputClass =
    "mt-1 w-full rounded border border-gray-300 px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-900";
  const labelClass = "flex flex-col text-xs font-medium text-gray-500";

  return (
    <main className="mx-auto max-w-5xl p-8">
      <Link
        to="/classification-rules"
        className="text-sm text-blue-600 hover:underline dark:text-blue-400"
      >
        ← Back to classification rules
      </Link>

      <h1 className="mt-4 text-2xl font-semibold">
        {isEdit ? "Edit Classification Rule" : "New Classification Rule"}
      </h1>

      {isEdit && isLoadingRule && <p className="mt-4 text-gray-500">Loading rule…</p>}

      {isEdit && isRuleError && (
        <p className="mt-4 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
          {formatApiError(ruleError)}
        </p>
      )}

      {(!isEdit || existingRule) && (
        <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[2fr_1fr]">
          <form
            onSubmit={handleSubmit}
            className="space-y-4 rounded-lg border border-gray-200 p-4 dark:border-gray-700"
          >
            {locked && (
              <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300">
                This is a system rule. Only "Enabled" can be changed — every other field is
                locked.
              </p>
            )}

            {saveMutation.isError && (
              <p className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
                {formatApiError(saveMutation.error)}
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

            <div className="grid grid-cols-2 gap-4">
              <label className={labelClass}>
                Installation type
                <select
                  disabled={locked}
                  value={form.installation_type}
                  onChange={(e) => {
                    updateField("installation_type", e.target.value as InstallationType);
                  }}
                  className={inputClass}
                >
                  {INSTALLATION_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>

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
            </div>

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

            <div>
              <p className="text-xs font-medium text-gray-500">Scope</p>
              {requiredScope === null ? (
                <p className="mt-1 text-sm text-gray-400">
                  {form.source} rules have no scope (unscoped).
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
                          updateField("scope", { ...form.scope, vendor: e.target.value as Vendor });
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

            <div className="grid grid-cols-2 gap-4">
              <label className={labelClass}>
                Field
                <select
                  disabled={locked}
                  value={form.field}
                  onChange={(e) => {
                    updateField("field", e.target.value);
                  }}
                  className={inputClass}
                >
                  {CLASSIFIABLE_FIELDS.map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>
              </label>

              <label className={labelClass}>
                Pattern (regex)
                <input
                  type="text"
                  required
                  disabled={locked}
                  value={form.pattern}
                  onChange={(e) => {
                    updateField("pattern", e.target.value);
                  }}
                  className={`${inputClass} font-mono`}
                />
              </label>
            </div>

            <div>
              <p className="text-xs font-medium text-gray-500">Flags</p>
              <div className="mt-1 flex gap-4">
                <label className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    disabled={locked}
                    checked={form.flags.ignore_case}
                    onChange={(e) => {
                      updateField("flags", { ...form.flags, ignore_case: e.target.checked });
                    }}
                  />
                  ignore_case
                </label>
                <label className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    disabled={locked}
                    checked={form.flags.multiline}
                    onChange={(e) => {
                      updateField("flags", { ...form.flags, multiline: e.target.checked });
                    }}
                  />
                  multiline
                </label>
                <label className="flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    disabled={locked}
                    checked={form.flags.dotall}
                    onChange={(e) => {
                      updateField("flags", { ...form.flags, dotall: e.target.checked });
                    }}
                  />
                  dotall
                </label>
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
            <PreviewPanel request={rawPreviewRequest} />
          </div>
        </div>
      )}

      {isEdit && id && existingRule && (
        <HistoryPanel eventTypes={CLASSIFICATION_EVENT_TYPES} idField="rule_id" entityId={id} />
      )}
    </main>
  );
}
