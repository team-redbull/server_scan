/**
 * Thin fetch wrapper. Every non-2xx response from this API is an RFC 9457
 * problem-details body (see backend `app.exception_handlers`); this parses
 * it into a typed `ApiError` so callers never hand-parse `error.detail`
 * strings themselves.
 */

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  code: string;
  request_id: string | null;
  details: Record<string, unknown>;
}

export class ApiError extends Error {
  readonly problem: ProblemDetails;

  constructor(problem: ProblemDetails) {
    super(problem.detail);
    this.name = "ApiError";
    this.problem = problem;
  }
}

// Empty string: same-origin in production (the API is served behind the
// same host/Route as the SPA). In dev, Vite's proxy (vite.config.ts) sends
// /api/* to the local backend, so this stays empty there too.
const API_BASE = "";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // `RequestInit["headers"]` (HeadersInit) can be a plain object, a
  // `Headers` instance, or a `string[][]` of tuples — only one of those
  // three shapes is safe to object-spread, so build the merged set through
  // the `Headers` API instead, which accepts and normalizes all three.
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    const problem = (await response.json().catch(() => null)) as ProblemDetails | null;
    if (problem) {
      throw new ApiError(problem);
    }
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}
