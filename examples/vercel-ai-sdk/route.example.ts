/**
 * Sketch of a Next.js App Router handler (app/api/chat/route.ts) using the helper.
 * Not executed by the tests; it's type-checked only. Replace getUser() with your auth.
 */
import { convertToModelMessages, type UIMessage } from "ai";
import { isBudgetExceededError } from "llmbrake";
import { createAiSdkBudget } from "./budget.ts";

const budget = createAiSdkBudget({
  budgetUsd: (ctx) => (ctx.plan === "pro" ? 20 : 2),
  downgradeAt: 0.8,
  downgradeMap: { "gpt-5.5": "gpt-5.4-mini" },
  // store: redisStore, // use a shared store on serverless (see the README)
});

declare function getUser(req: Request): Promise<{ id: string; plan: string }>;

export async function POST(req: Request) {
  const user = await getUser(req);
  const { messages }: { messages: UIMessage[] } = await req.json();
  const ctx = { customerId: user.id, plan: user.plan, feature: "chat" };
  try {
    const { result } = await budget.budgetedStreamText(ctx, "gpt-5.5", { messages: await convertToModelMessages(messages) });
    return result.toUIMessageStreamResponse();
  } catch (e) {
    if (isBudgetExceededError(e)) {
      return Response.json({ error: "budget_exceeded", spentUsd: e.spentUsd, budgetUsd: e.budgetUsd }, { status: 402 });
    }
    throw e;
  }
}
