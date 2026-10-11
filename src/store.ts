/** Pluggable spend storage. Implement this with Redis/Postgres/etc. for multi-instance apps. */
export interface UsageStore {
  /** Current spend in USD for a customer in a period (e.g. "2026-10"). */
  getSpend(customerId: string, period: string): Promise<number>;
  /** Atomically add USD and return the new total. */
  addSpend(customerId: string, period: string, usd: number): Promise<number>;
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
