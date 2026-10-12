/** Pluggable spend storage. Implement this with Redis/Postgres/etc. for multi-instance apps. */
export interface UsageStore {
  /** Current spend in USD for a customer in a period (e.g. "2026-10"). */
  getSpend(customerId: string, period: string): Promise<number>;
  /** Atomically add USD and return the new total. */
  addSpend(customerId: string, period: string, usd: number): Promise<number>;
}

/** What a {@link ReservingStore} answers before a call. */
export interface Reservation {
  /** false = the customer is at/over budget (counting other in-flight calls); the call must not be made. */
  allowed: boolean;
  /** Settled spend this period (USD). */
  spentUsd: number;
  /** Effective budget (USD). Infinity = unlimited. May differ from the one you sent if a dashboard override is set. */
  budgetUsd: number;
  /** Pass back to `settle`. null when not allowed (or when the store failed open). */
  holdId: string | null;
}

export interface ReserveRequest {
  customerId: string;
  period: string;
  /** Estimated cost of the call, held until it settles. */
  holdUsd: number;
  /** Budget from your guard config. Infinity = unlimited. */
  budgetUsd: number;
  downgradeAt: number;
  /** Streams get a longer hold before it expires on its own. */
  stream: boolean;
}

export interface SettleRequest {
  holdId: string | null;
  customerId: string;
  period: string;
  /** Actual cost; 0 releases the hold without charging (e.g. the provider call failed). */
  costUsd: number;
  downgraded: boolean;
}

/**
 * Optional extension of {@link UsageStore} for shared stores (e.g. {@link HostedStore}).
 * When the guard's store implements it, wrapped calls *check-and-reserve* atomically before the
 * provider call and *settle* after it, so parallel calls across servers can't overshoot the cap together.
 */
export interface ReservingStore extends UsageStore {
  reserve(req: ReserveRequest): Promise<Reservation>;
  settle(req: SettleRequest): Promise<number>;
}

export function isReservingStore(s: UsageStore): s is ReservingStore {
  return typeof (s as Partial<ReservingStore>).reserve === "function" && typeof (s as Partial<ReservingStore>).settle === "function";
}

/** In-memory store. Fine for a single process, tests and demos; state is lost on restart. */
export class MemoryStore implements UsageStore {
  private data = new Map<string, number>();
  private key(c: string, p: string) { return `${p}:${c}`; }
  async getSpend(customerId: string, period: string): Promise<number> {
    return this.data.get(this.key(customerId, period)) ?? 0;
  }
  async addSpend(customerId: string, period: string, usd: number): Promise<number> {
    const k = this.key(customerId, period);
    const next = (this.data.get(k) ?? 0) + usd;
    this.data.set(k, next);
    return next;
  }
}

/**
 * Example Redis adapter shape (not bundled, to keep zero runtime deps):
 *
 *   const redisStore: UsageStore = {
 *     async getSpend(c, p) { return Number(await redis.get(`llmbrake:${p}:${c}`) ?? 0); },
 *     async addSpend(c, p, usd) { return Number(await redis.incrbyfloat(`llmbrake:${p}:${c}`, usd)); },
 *   };
 */
