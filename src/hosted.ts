import type { Reservation, ReserveRequest, ReservingStore, SettleRequest } from "./store.js";

export const DEFAULT_HOSTED_URL = "https://app.llmbrake.com";

export interface HostedStoreOptions {
  /** Your project's SDK API key (lbsk_...). */
  apiKey: string;
  /** API base URL. Default https://app.llmbrake.com */
  baseUrl?: string;
  /** Per-request timeout in ms. Default 3000. On timeout the guard follows `failOpen`. */
  timeoutMs?: number;
  /** fetch implementation (defaults to global fetch; Node 18+). */
  fetch?: typeof fetch;
}

export class HostedStoreError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "HostedStoreError";
  }
}

/**
 * Shared, hosted spend counters (LLMBrake Cloud). One counter per customer per month across every
 * server using the same API key; budgets edited in the dashboard override the guard's `budgetUsd`.
 *
 *   const guard = createGuard({ budgetUsd: 5, store: new HostedStore({ apiKey: process.env.LLMBRAKE_API_KEY! }) });
 */
export class HostedStore implements ReservingStore {
  private readonly base: string;
  private readonly key: string;
  private readonly timeoutMs: number;
  private readonly f: typeof fetch;

  constructor(opts: HostedStoreOptions) {
    if (!opts?.apiKey) throw new TypeError("llmbrake: HostedStore needs an apiKey");
    this.key = opts.apiKey;
    this.base = (opts.baseUrl ?? DEFAULT_HOSTED_URL).replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? 3000;
    const f = opts.fetch ?? (globalThis as { fetch?: typeof fetch }).fetch;
    if (!f) throw new TypeError("llmbrake: HostedStore needs fetch (Node 18+), or pass opts.fetch");
    this.f = f;
  }

  private async call(method: "GET" | "POST", path: string, body?: unknown): Promise<any> {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), this.timeoutMs);
    try {
      const r = await this.f(this.base + path, {
        method,
        headers: { authorization: `Bearer ${this.key}`, "content-type": "application/json", "user-agent": "llmbrake-js" },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctl.signal,
      });
      const data: any = await r.json().catch(() => ({}));
      if (!r.ok) throw new HostedStoreError(`llmbrake: hosted API ${r.status} ${data?.error ?? ""}`.trim(), r.status);
      return data;
    } catch (e) {
      if (e instanceof HostedStoreError) throw e;
      throw new HostedStoreError(`llmbrake: hosted API unreachable (${(e as Error)?.name === "AbortError" ? `timeout after ${this.timeoutMs}ms` : String((e as Error)?.message ?? e)})`);
    } finally {
      clearTimeout(t);
    }
  }

  async getSpend(customerId: string, period: string): Promise<number> {
    const q = new URLSearchParams({ customerId, period });
    return (await this.call("GET", `/v1/spend?${q}`)).spentUsd;
  }

  async addSpend(customerId: string, period: string, usd: number): Promise<number> {
    return (await this.call("POST", "/v1/spend/add", { customerId, period, usd })).spentUsd;
  }

  async reserve(req: ReserveRequest): Promise<Reservation> {
    const r = await this.call("POST", "/v1/reserve", {
      customerId: req.customerId, period: req.period, holdUsd: req.holdUsd,
      budgetUsd: Number.isFinite(req.budgetUsd) ? req.budgetUsd : null,
      downgradeAt: req.downgradeAt, ttlSec: req.stream ? 900 : 120,
    });
    return { allowed: !!r.allowed, spentUsd: r.spentUsd, budgetUsd: r.budgetUsd === null ? Infinity : r.budgetUsd, holdId: r.holdId ?? null };
  }

  async settle(req: SettleRequest): Promise<number> {
    return (await this.call("POST", "/v1/settle", req)).spentUsd;
  }

  /** Server view of a customer this period (spend, in-flight holds, effective budget, status). */
  async customer(customerId: string, period: string): Promise<{ spentUsd: number; pendingUsd: number; budgetUsd: number | null; budgetSource: string; status: string }> {
    const q = new URLSearchParams({ customerId, period });
    return this.call("GET", `/v1/spend?${q}`);
  }
}
