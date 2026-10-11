export class BudgetExceededError extends Error {
  readonly code = "LLMBRAKE_BUDGET_EXCEEDED" as const;
  constructor(
    readonly customerId: string,
    readonly spentUsd: number,
    readonly budgetUsd: number,
    readonly period: string,
  ) {
    super(`Customer ${customerId} has used $${spentUsd.toFixed(4)} of a $${budgetUsd.toFixed(2)} AI budget for ${period}.`);
    this.name = "BudgetExceededError";
  }
}

export class UnknownModelPriceError extends Error {
  readonly code = "LLMBRAKE_UNKNOWN_MODEL" as const;
  constructor(readonly model: string) {
    super(`No price configured for model "${model}". Add it to the price table.`);
    this.name = "UnknownModelPriceError";
  }
}

export function isBudgetExceededError(e: unknown): e is BudgetExceededError {
  return e instanceof BudgetExceededError;
}
