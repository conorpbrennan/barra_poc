// Same-origin API base under the un-stripped nginx prefix (docs/vite-ui-plan.md §8). In dev,
// vite.config proxies this to :8010; in prod nginx proxies it (basic-auth inherited, no CORS).
export const API_BASE = "/flexagg2++/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

function qs(params?: Record<string, string | number | boolean | null | undefined>): string {
  if (!params) return "";
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      return typeof d === "string" ? d : JSON.stringify(d);
    }
    return JSON.stringify(body);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

export async function apiGet<T>(
  path: string,
  params?: Record<string, string | number | boolean | null | undefined>,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}${qs(params)}`);
  if (!res.ok) throw new ApiError(res.status, await parseError(res));
  return res.json() as Promise<T>;
}

export async function apiSend<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new ApiError(res.status, await parseError(res));
  // DELETE/PUT may return empty
  const text = await res.text();
  return (text ? JSON.parse(text) : null) as T;
}
