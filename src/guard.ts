import { BudgetExceededError, UnknownModelPriceError } from "./errors.js";
import { ChatUsage, DEFAULT_PRICES, PriceTable, computeCostUsd } from "./prices.js";
import { MemoryStore, UsageStore, isReservingStore } from "./store.js";

export interface CallContext {
  customerId: string;
  /** Optional plan name, passed to your budget function. */
  plan?: string;
  /** Optional feature tag for analytics, e.g. "summarize". */
  feature?: string;
}

export interface UsageEvent {
  customerId: string;
  plan?: string;
  feature?: string;
  period: string;
  requestedModel: string;
  model: string;
  downgraded: boolean;
  promptTokens: number;
  completionTokens: number;
  cachedTokens: number;
  /** null when the model had no price configured */
  costUsd: number | null;
  spentUsd: number;
  budgetUsd: number;
  timestamp: string;
}

export interface GuardOptions {
  /** Monthly USD budget for a customer (number or lookup). Return Infinity for unlimited. */
  budgetUsd: number | ((ctx: CallContext) => number | Promise<number>);
  store?: UsageStore;
  prices?: PriceTable;
  /** Fraction of budget (0–1) at which to switch to a cheaper model. Default 0.8. Set to 1 to disable. */
  downgradeAt?: number;
  /** Map of expensive model -> cheaper model to use once past `downgradeAt`. */
  downgradeMap?: Record<string, string>;
  /** Called after each successful, metered call. Errors thrown here are swallowed. */
  onUsage?: (e: UsageEvent) => void | Promise<void>;
  /** What to do when a model has no price: "throw" before calling, or "allow" and record cost null. Default "throw". */
  unknownModel?: "throw" | "allow";
  /** If the store fails, let the call through (true, default) or rethrow (false). */
  failOpen?: boolean;
  /** Clock override for tests. */
  now?: () => Date;
  /**
   * Only used with a reserving store (e.g. HostedStore): the cost to hold while a call is in flight.
   * Default: estimated from the prompt size (~4 chars/token) plus `max_completion_tokens`/`max_tokens`
   * (or 1,000 output tokens) at the requested model's price.
   */
  estimateUsd?: number | ((ctx: CallContext, params: Record<string, unknown>) => number);
}

/** Rough upper-ish estimate of a call's cost, used only to size reservations. */
export function estimateCallUsd(model: string, params: Record<string, unknown>, prices: PriceTable = DEFAULT_PRICES): number {
  let chars = 0;
  try { chars = JSON.stringify(params.messages ?? params.input ?? params.prompt ?? "").length; } catch { /* circular: ignore */ }
  const maxOut = Number(params.max_completion_tokens ?? params.max_tokens ?? 1000);
  const cost = computeCostUsd(model, { prompt_tokens: Math.ceil(chars / 4), completion_tokens: Number.isFinite(maxOut) && maxOut > 0 ? maxOut : 1000 }, prices);
  return cost ?? 0;
}

type ChatParams = { model: string; stream?: boolean | null; stream_options?: Record<string, unknown> | null; [k: string]: unknown };
type CreateFn<P, R> = (params: P, ...rest: any[]) => Promise<R>;

export function periodOf(d: Date): string {
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function createGuard(opts: GuardOptions) {
  const store = opts.store ?? new MemoryStore();
  const prices = opts.prices ?? DEFAULT_PRICES;
  const downgradeAt = opts.downgradeAt ?? 0.8;
  const downgradeMap = opts.downgradeMap ?? {};
  const unknownModel = opts.unknownModel ?? "throw";
  const failOpen = opts.failOpen ?? true;
  const now = opts.now ?? (() => new Date());
  if (typeof downgradeAt !== "number" || !(downgradeAt >= 0 && downgradeAt <= 1)) {
    throw new RangeError(`llmbrake: downgradeAt must be a number between 0 and 1, got ${String(downgradeAt)}`);
  }

  async function budgetFor(ctx: CallContext): Promise<number> {
    const b = typeof opts.budgetUsd === "function" ? await opts.budgetUsd(ctx) : opts.budgetUsd;
    // NaN or a negative budget would silently disable the cap (spent >= NaN is always false), so reject it.
    if (typeof b !== "number" || Number.isNaN(b) || b < 0) {
      throw new TypeError(`llmbrake: budgetUsd for customer ${ctx.customerId} must be a number >= 0 (or Infinity), got ${String(b)}`);
    }
    return b;
  }

  async function safeGetSpend(c: string, p: string): Promise<number> {
    try {
      const spent = await store.getSpend(c, p);
      // A store returning NaN/garbage would also silently disable the cap; treat it as a store failure.
      if (typeof spent !== "number" || !Number.isFinite(spent)) throw new TypeError(`llmbrake: store.getSpend returned a non-numeric value (${String(spent)})`);
      return spent;
    } catch (e) { if (failOpen) return 0; throw e; }
  }

  /** Decide whether a call may proceed, and with which model. Throws BudgetExceededError at the cap. */
  async function check(ctx: CallContext, model: string) {
    const period = periodOf(now());
    const budget = await budgetFor(ctx);
    const spent = await safeGetSpend(ctx.customerId, period);
    if (spent >= budget) throw new BudgetExceededError(ctx.customerId, spent, budget, period);
    let effective = model;
    if (budget !== Infinity && spent >= budget * downgradeAt && downgradeMap[model]) effective = downgradeMap[model];
    if (unknownModel === "throw" && computeCostUsd(effective, {}, prices) === null) throw new UnknownModelPriceError(effective);
    return { period, budget, spent, model: effective, downgraded: effective !== model, holdId: null as string | null };
  }

  function decide(ctx: CallContext, model: string, period: string, budget: number, spent: number) {
    if (spent >= budget) throw new BudgetExceededError(ctx.customerId, spent, budget, period);
    let effective = model;
    if (budget !== Infinity && spent >= budget * downgradeAt && downgradeMap[model]) effective = downgradeMap[model];
    if (unknownModel === "throw" && computeCostUsd(effective, {}, prices) === null) throw new UnknownModelPriceError(effective);
    return { period, budget, spent, model: effective, downgraded: effective !== model, holdId: null as string | null };
  }

  /** Reserving stores: atomic check-and-reserve before the call (see ReservingStore). */
  async function reserveCheck(ctx: CallContext, params: ChatParams) {
    const period = periodOf(now());
    const budget = await budgetFor(ctx);
    if (!isReservingStore(store)) throw new Error("unreachable");
    let holdUsd = 0;
    try {
      holdUsd = typeof opts.estimateUsd === "function" ? opts.estimateUsd(ctx, params) : opts.estimateUsd ?? estimateCallUsd(params.model, params, prices);
      if (!Number.isFinite(holdUsd) || holdUsd < 0) holdUsd = 0;
    } catch { holdUsd = 0; }
    let r: { allowed: boolean; spentUsd: number; budgetUsd: number; holdId: string | null };
    try {
      r = await store.reserve({ customerId: ctx.customerId, period, holdUsd, budgetUsd: budget, downgradeAt, stream: !!params.stream });
      if (typeof r?.spentUsd !== "number" || !Number.isFinite(r.spentUsd)) throw new TypeError("llmbrake: store.reserve returned a non-numeric spentUsd");
    } catch (e) {
      if (!failOpen) throw e;
      return decide(ctx, params.model, period, budget, 0);
    }
    const effBudget = typeof r.budgetUsd === "number" && !Number.isNaN(r.budgetUsd) ? r.budgetUsd : budget;
    if (!r.allowed) throw new BudgetExceededError(ctx.customerId, r.spentUsd, effBudget, period);
    // The server already admitted the call; only the downgrade and unknown-model rules apply here.
    const model = params.model;
    const effective = effBudget !== Infinity && r.spentUsd >= effBudget * downgradeAt && downgradeMap[model] ? downgradeMap[model] : model;
    if (unknownModel === "throw" && computeCostUsd(effective, {}, prices) === null) {
      await release(ctx, period, r.holdId);
      throw new UnknownModelPriceError(effective);
    }
    return { period, budget: effBudget, spent: r.spentUsd, model: effective, downgraded: effective !== model, holdId: r.holdId };
  }

  async function release(ctx: CallContext, period: string, holdId: string | null) {
    if (!holdId || !isReservingStore(store)) return;
    try { await store.settle({ holdId, customerId: ctx.customerId, period, costUsd: 0, downgraded: false }); } catch { /* hold expires on its own */ }
  }

  async function record(ctx: CallContext, d: Awaited<ReturnType<typeof check>>, requestedModel: string, respModel: string | undefined, usage: ChatUsage | undefined) {
    const u = usage ?? {};
    // Price by the model the provider reports (dated snapshots resolve via lookupPrice); fall back to the model we sent.
    const priceModel = respModel && computeCostUsd(respModel, {}, prices) !== null ? respModel : d.model;
    const cost = computeCostUsd(priceModel, u, prices);
    let spent = d.spent;
    if (d.holdId && isReservingStore(store)) {
      try { spent = await store.settle({ holdId: d.holdId, customerId: ctx.customerId, period: d.period, costUsd: cost ?? 0, downgraded: d.downgraded }); }
      catch (e) { if (!failOpen) throw e; }
    } else if (cost !== null && cost > 0) {
      try { spent = await store.addSpend(ctx.customerId, d.period, cost); }
      catch (e) { if (!failOpen) throw e; }
    }
    const event: UsageEvent = {
      customerId: ctx.customerId, plan: ctx.plan, feature: ctx.feature, period: d.period,
      requestedModel, model: d.model, downgraded: d.downgraded,
      promptTokens: u.prompt_tokens ?? 0, completionTokens: u.completion_tokens ?? 0,
      cachedTokens: u.prompt_tokens_details?.cached_tokens ?? 0,
      costUsd: cost, spentUsd: spent, budgetUsd: d.budget, timestamp: now().toISOString(),
    };
    if (opts.onUsage) { try { await opts.onUsage(event); } catch { /* never break the caller */ } }
    return event;
  }

  /**
   * Wrap an OpenAI-compatible `chat.completions.create` function.
   *   const create = guard.wrap(openai.chat.completions.create.bind(openai.chat.completions));
   *   await create({ model, messages }, { customerId: "cus_123" });
   */
  function wrap<P extends ChatParams, R>(createFn: CreateFn<P, R>) {
    return async function guardedCreate(params: P, ctx: CallContext, ...rest: any[]): Promise<R> {
      if (!ctx?.customerId) throw new TypeError("llmbrake: ctx.customerId is required");
      const requestedModel = params.model;
      const d = isReservingStore(store) ? await reserveCheck(ctx, params) : await check(ctx, requestedModel);
      const finalParams = { ...params, model: d.model } as P;
      if (params.stream) {
        (finalParams as ChatParams).stream_options = { ...(params.stream_options ?? {}), include_usage: true };
        let stream: AsyncIterable<any>;
        try { stream = (await createFn(finalParams, ...rest)) as unknown as AsyncIterable<any>; }
        catch (e) { await release(ctx, d.period, d.holdId); throw e; }
        return wrapStream(stream, (model, usage) => record(ctx, d, requestedModel, model, usage)) as unknown as R;
      }
      let res: any;
      try { res = await createFn(finalParams, ...rest); }
      catch (e) { await release(ctx, d.period, d.holdId); throw e; }
      await record(ctx, d, requestedModel, res?.model, res?.usage);
      return res as R;
    };
  }

  /** Current spend/budget for a customer this period. */
  async function status(ctx: CallContext) {
    const period = periodOf(now());
    const budget = await budgetFor(ctx);
    const spent = await safeGetSpend(ctx.customerId, period);
    return { period, spentUsd: spent, budgetUsd: budget, remainingUsd: Math.max(0, budget - spent) };
  }

  return { wrap, check, status };
}

function wrapStream(stream: AsyncIterable<any>, onDone: (model: string | undefined, usage: ChatUsage | undefined) => Promise<unknown>) {
  const proxy: AsyncIterable<any> = {
    async *[Symbol.asyncIterator]() {
      let usage: ChatUsage | undefined;
      let model: string | undefined;
      try {
        for await (const chunk of stream) {
          if (chunk?.usage) usage = chunk.usage;
          if (chunk?.model) model = chunk.model;
          yield chunk;
        }
      } finally {
        await onDone(model, usage);
      }
    },
  };
  // Preserve other properties (e.g. controller) of SDK stream objects.
  return Object.assign(Object.create(Object.getPrototypeOf(stream) ?? null), stream, { [Symbol.asyncIterator]: proxy[Symbol.asyncIterator] });
}
