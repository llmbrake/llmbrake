"""The guard: per-customer monthly USD budgets around chat.completions.create."""

from __future__ import annotations

import functools
import inspect
import math
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Literal,
    Mapping,
    Optional,
    TypeVar,
    Union,
)

from ._errors import BudgetExceededError, UnknownModelPriceError
from ._prices import DEFAULT_PRICES, PriceTable, _field, compute_cost_usd, usage_tokens
from ._store import AsyncUsageStore, MemoryStore, UsageStore, is_reserving_store
from ._stream import GuardedAsyncStream, GuardedStream

__all__ = [
    "CallContext",
    "UsageEvent",
    "Decision",
    "BudgetStatus",
    "Guard",
    "create_guard",
    "period_of",
    "estimate_call_usd",
]

R = TypeVar("R")


@dataclass(frozen=True)
class CallContext:
    """Who a call is for. Passed to your ``budget_usd`` function."""

    customer_id: str
    #: Optional plan name, e.g. "pro".
    plan: Optional[str] = None
    #: Optional feature tag for analytics, e.g. "summarize".
    feature: Optional[str] = None


@dataclass(frozen=True)
class UsageEvent:
    """Metadata-only record of one metered call (never prompts or completions)."""

    customer_id: str
    plan: Optional[str]
    feature: Optional[str]
    period: str
    requested_model: str
    model: str
    downgraded: bool
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    #: None when the model had no price configured
    cost_usd: Optional[float]
    spent_usd: float
    budget_usd: float
    #: ISO 8601 UTC, millisecond precision, e.g. "2026-10-11T20:00:00.000Z"
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    """Result of :meth:`Guard.check`: whether and how a call may proceed."""

    period: str
    budget_usd: float
    spent_usd: float
    #: The model to actually call (the downgrade target if ``downgraded``).
    model: str
    downgraded: bool
    #: Reservation id when the store is a reserving store (e.g. HostedStore); settled after the call.
    hold_id: Optional[str] = None


@dataclass(frozen=True)
class BudgetStatus:
    period: str
    spent_usd: float
    budget_usd: float
    remaining_usd: float


BudgetFn = Callable[[CallContext], Union[float, Awaitable[float]]]
EstimateFn = Callable[[CallContext, Mapping[str, Any]], float]
OnUsage = Callable[[UsageEvent], Union[None, Awaitable[None]]]
UnknownModelPolicy = Literal["throw", "allow"]


def period_of(d: datetime) -> str:
    """Budget period (UTC calendar month) for a datetime, e.g. ``"2026-10"``.

    Naive datetimes are treated as UTC.
    """
    d = _to_utc(d)
    return f"{d.year:04d}-{d.month:02d}"


def estimate_call_usd(model: str, params: Mapping[str, Any], prices: Optional[PriceTable] = None) -> float:
    """Rough cost estimate used only to size reservations: prompt ~4 chars/token, plus
    ``max_completion_tokens``/``max_tokens`` (default 1,000) output tokens at ``model``'s price."""
    import json

    try:
        chars = len(
            json.dumps(params.get("messages") or params.get("input") or params.get("prompt") or "", default=str)
        )
    except Exception:
        chars = 0
    max_out: Any = params.get("max_completion_tokens") or params.get("max_tokens") or 1000
    if not _is_number(max_out) or max_out <= 0:
        max_out = 1000
    cost = compute_cost_usd(
        model, {"prompt_tokens": math.ceil(chars / 4), "completion_tokens": int(max_out)}, prices or DEFAULT_PRICES
    )
    return cost or 0.0


def _to_utc(d: datetime) -> datetime:
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _iso(d: datetime) -> str:
    d = _to_utc(d)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _discard(awaitable: Any) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


def _is_async_callable(fn: Any) -> bool:
    """Detect ``async def`` functions, including ones hidden behind functools.wraps decorators
    (openai's ``AsyncCompletions.create`` is one) and ``functools.partial``."""
    target = fn
    while isinstance(target, functools.partial):
        target = target.func
    if inspect.iscoroutinefunction(target):
        return True
    try:
        unwrapped = inspect.unwrap(target)
    except ValueError:  # pragma: no cover - cyclic __wrapped__
        return False
    if inspect.iscoroutinefunction(unwrapped):
        return True
    call = getattr(target, "__call__", None)  # noqa: B004
    return inspect.iscoroutinefunction(call)


def _make_ctx(customer_id: Any, plan: Optional[str], feature: Optional[str]) -> CallContext:
    if not isinstance(customer_id, str) or not customer_id:
        raise TypeError("llmbrake: customer_id is required (a non-empty string)")
    return CallContext(customer_id=customer_id, plan=plan, feature=feature)


def _requested_model(params: Mapping[str, Any]) -> str:
    model = params.get("model")
    if not isinstance(model, str) or not model:
        raise TypeError("llmbrake: model is required (a non-empty string)")
    return model


_ASYNC_IN_SYNC = (
    "llmbrake: {what} returned an awaitable in a synchronous call. "
    "Use an async guarded call (guard.wrap_async / acheck / astatus) with async stores, "
    "budget functions and providers."
)


class Guard:
    """Created by :func:`create_guard`. Thread-safe as long as the store is."""

    def __init__(
        self,
        budget_usd: Union[float, BudgetFn],
        *,
        store: Union[UsageStore, AsyncUsageStore, None] = None,
        prices: Optional[PriceTable] = None,
        downgrade_at: float = 0.8,
        downgrade_map: Optional[Mapping[str, str]] = None,
        on_usage: Optional[OnUsage] = None,
        unknown_model: UnknownModelPolicy = "throw",
        fail_open: bool = True,
        now: Optional[Callable[[], datetime]] = None,
        estimate_usd: Union[float, EstimateFn, None] = None,
    ) -> None:
        if not _is_number(downgrade_at) or not (0 <= downgrade_at <= 1):
            raise ValueError(f"llmbrake: downgrade_at must be a number between 0 and 1, got {downgrade_at!r}")
        if unknown_model not in ("throw", "allow"):
            raise ValueError(f'llmbrake: unknown_model must be "throw" or "allow", got {unknown_model!r}')
        self._budget = budget_usd
        self._store: Any = store if store is not None else MemoryStore()
        self._prices: PriceTable = prices if prices is not None else DEFAULT_PRICES
        self._downgrade_at = float(downgrade_at)
        self._downgrade_map: Mapping[str, str] = dict(downgrade_map or {})
        self._on_usage = on_usage
        self._unknown_model = unknown_model
        self._fail_open = fail_open
        self._now = now or _utcnow
        self._estimate = estimate_usd

    # ------------------------------------------------------------------ pure helpers
    def _validate_budget(self, b: Any, ctx: CallContext) -> float:
        # NaN or a negative budget would silently disable the cap (spent >= NaN is always False).
        if not _is_number(b) or math.isnan(b) or b < 0:
            raise TypeError(
                f"llmbrake: budget_usd for customer {ctx.customer_id} must be a number >= 0 (or math.inf), got {b!r}"
            )
        return float(b)

    @staticmethod
    def _validate_spend(spent: Any) -> float:
        # A store returning NaN/garbage would also silently disable the cap; treat it as a store failure.
        if not _is_number(spent) or not math.isfinite(spent):
            raise TypeError(f"llmbrake: store.get_spend returned a non-numeric value ({spent!r})")
        return float(spent)

    def _decide(self, ctx: CallContext, model: str, period: str, budget: float, spent: float) -> Decision:
        if spent >= budget:
            raise BudgetExceededError(ctx.customer_id, spent, budget, period)
        effective = model
        if budget != math.inf and spent >= budget * self._downgrade_at:
            target = self._downgrade_map.get(model)
            if target:
                effective = target
        if self._unknown_model == "throw" and compute_cost_usd(effective, None, self._prices) is None:
            raise UnknownModelPriceError(effective)
        return Decision(period, budget, spent, effective, effective != model)

    # ------------------------------------------------------------------ reserving stores
    @property
    def _reserving(self) -> bool:
        return is_reserving_store(self._store)

    def _hold_usd(self, ctx: CallContext, params: Mapping[str, Any]) -> float:
        try:
            if callable(self._estimate):
                v: Any = self._estimate(ctx, params)
            elif self._estimate is not None:
                v = self._estimate
            else:
                v = estimate_call_usd(str(params.get("model")), params, self._prices)
            return float(v) if _is_number(v) and math.isfinite(v) and v >= 0 else 0.0
        except Exception:
            return 0.0

    def _after_reserve(self, ctx: CallContext, model: str, period: str, budget: float, r: Any) -> Decision:
        spent = self._validate_spend(getattr(r, "spent_usd", None))
        eff = getattr(r, "budget_usd", budget)
        eff = float(eff) if _is_number(eff) and not math.isnan(eff) else budget
        if not getattr(r, "allowed", False):
            raise BudgetExceededError(ctx.customer_id, spent, eff, period)
        effective = model
        if eff != math.inf and spent >= eff * self._downgrade_at:
            effective = self._downgrade_map.get(model) or model
        if self._unknown_model == "throw" and compute_cost_usd(effective, None, self._prices) is None:
            raise _ReleaseAndRaise(UnknownModelPriceError(effective), getattr(r, "hold_id", None), period)
        return Decision(period, eff, spent, effective, effective != model, getattr(r, "hold_id", None))

    def _reserve_check(self, ctx: CallContext, params: Mapping[str, Any], requested: str) -> Decision:
        period = period_of(self._now())
        budget = self._budget_for(ctx)
        try:
            r = self._store.reserve(
                ctx.customer_id,
                period,
                hold_usd=self._hold_usd(ctx, params),
                budget_usd=budget,
                downgrade_at=self._downgrade_at,
                stream=bool(params.get("stream")),
            )
            if inspect.isawaitable(r):
                _discard(r)
                raise TypeError(_ASYNC_IN_SYNC.format(what="store.reserve"))
            self._validate_spend(getattr(r, "spent_usd", None))
        except TypeError as e:
            if "awaitable" in str(e):
                raise
            if self._fail_open:
                return self._decide(ctx, requested, period, budget, 0.0)
            raise
        except Exception:
            if self._fail_open:
                return self._decide(ctx, requested, period, budget, 0.0)
            raise
        try:
            return self._after_reserve(ctx, requested, period, budget, r)
        except _ReleaseAndRaise as e:
            self._release(ctx, e.period, e.hold_id)
            raise e.error from None

    async def _areserve_check(self, ctx: CallContext, params: Mapping[str, Any], requested: str) -> Decision:
        period = period_of(self._now())
        budget = await self._abudget_for(ctx)
        try:
            r = self._store.reserve(
                ctx.customer_id,
                period,
                hold_usd=self._hold_usd(ctx, params),
                budget_usd=budget,
                downgrade_at=self._downgrade_at,
                stream=bool(params.get("stream")),
            )
            if inspect.isawaitable(r):
                r = await r
            self._validate_spend(getattr(r, "spent_usd", None))
        except Exception:
            if self._fail_open:
                return self._decide(ctx, requested, period, budget, 0.0)
            raise
        try:
            return self._after_reserve(ctx, requested, period, budget, r)
        except _ReleaseAndRaise as e:
            await self._arelease(ctx, e.period, e.hold_id)
            raise e.error from None

    def _release(self, ctx: CallContext, period: str, hold_id: Optional[str]) -> None:
        if not hold_id or not self._reserving:
            return
        try:
            r = self._store.settle(hold_id, ctx.customer_id, period, cost_usd=0.0, downgraded=False)
            if inspect.isawaitable(r):
                _discard(r)
        except Exception:
            pass  # the hold expires on its own

    async def _arelease(self, ctx: CallContext, period: str, hold_id: Optional[str]) -> None:
        if not hold_id or not self._reserving:
            return
        try:
            r = self._store.settle(hold_id, ctx.customer_id, period, cost_usd=0.0, downgraded=False)
            if inspect.isawaitable(r):
                await r
        except Exception:
            pass

    def _cost(self, d: Decision, resp_model: Optional[str], usage: Any) -> Optional[float]:
        # Price by the model the provider reports (dated snapshots resolve via lookup_price);
        # fall back to the model we sent.
        price_model = d.model
        if isinstance(resp_model, str) and resp_model and compute_cost_usd(resp_model, None, self._prices) is not None:
            price_model = resp_model
        return compute_cost_usd(price_model, usage, self._prices)

    def _event(
        self, ctx: CallContext, d: Decision, requested: str, usage: Any, cost: Optional[float], spent: float
    ) -> UsageEvent:
        prompt, completion, cached = usage_tokens(usage)
        return UsageEvent(
            customer_id=ctx.customer_id,
            plan=ctx.plan,
            feature=ctx.feature,
            period=d.period,
            requested_model=requested,
            model=d.model,
            downgraded=d.downgraded,
            prompt_tokens=int(prompt),
            completion_tokens=int(completion),
            cached_tokens=int(cached),
            cost_usd=cost,
            spent_usd=spent,
            budget_usd=d.budget_usd,
            timestamp=_iso(self._now()),
        )

    # ------------------------------------------------------------------ sync IO
    def _budget_for(self, ctx: CallContext) -> float:
        b: Any = self._budget(ctx) if callable(self._budget) else self._budget
        if inspect.isawaitable(b):
            _discard(b)
            raise TypeError(_ASYNC_IN_SYNC.format(what="budget_usd"))
        return self._validate_budget(b, ctx)

    def _safe_get_spend(self, customer_id: str, period: str) -> float:
        try:
            raw = self._store.get_spend(customer_id, period)
        except Exception:
            if self._fail_open:
                return 0.0
            raise
        if inspect.isawaitable(raw):
            _discard(raw)
            raise TypeError(_ASYNC_IN_SYNC.format(what="store.get_spend"))
        try:
            return self._validate_spend(raw)
        except TypeError:
            if self._fail_open:
                return 0.0
            raise

    def _check(self, ctx: CallContext, model: str) -> Decision:
        period = period_of(self._now())
        budget = self._budget_for(ctx)
        spent = self._safe_get_spend(ctx.customer_id, period)
        return self._decide(ctx, model, period, budget, spent)

    def _record(
        self, ctx: CallContext, d: Decision, requested: str, resp_model: Optional[str], usage: Any
    ) -> UsageEvent:
        cost = self._cost(d, resp_model, usage)
        spent: Any = d.spent_usd
        if d.hold_id and self._reserving:
            try:
                res = self._store.settle(
                    d.hold_id, ctx.customer_id, d.period, cost_usd=cost or 0.0, downgraded=d.downgraded
                )
            except Exception:
                if not self._fail_open:
                    raise
            else:
                if inspect.isawaitable(res):
                    _discard(res)
                    raise TypeError(_ASYNC_IN_SYNC.format(what="store.settle"))
                spent = res
        elif cost is not None and cost > 0:
            try:
                res = self._store.add_spend(ctx.customer_id, d.period, cost)
            except Exception:
                if not self._fail_open:
                    raise
            else:
                if inspect.isawaitable(res):
                    _discard(res)
                    raise TypeError(_ASYNC_IN_SYNC.format(what="store.add_spend"))
                spent = res
        event = self._event(ctx, d, requested, usage, cost, spent)
        if self._on_usage is not None:
            try:
                r = self._on_usage(event)
                if inspect.isawaitable(r):
                    _discard(r)
                    warnings.warn(
                        "llmbrake: on_usage returned an awaitable in a synchronous call; it was not awaited. "
                        "Use a sync on_usage, or async guarded calls.",
                        RuntimeWarning,
                        stacklevel=2,
                    )
            except Exception:
                pass  # never break the caller
        return event

    # ------------------------------------------------------------------ async IO
    async def _abudget_for(self, ctx: CallContext) -> float:
        b: Any = self._budget(ctx) if callable(self._budget) else self._budget
        if inspect.isawaitable(b):
            b = await b
        return self._validate_budget(b, ctx)

    async def _asafe_get_spend(self, customer_id: str, period: str) -> float:
        try:
            raw = self._store.get_spend(customer_id, period)
            if inspect.isawaitable(raw):
                raw = await raw
            return self._validate_spend(raw)
        except Exception:
            if self._fail_open:
                return 0.0
            raise

    async def _acheck(self, ctx: CallContext, model: str) -> Decision:
        period = period_of(self._now())
        budget = await self._abudget_for(ctx)
        spent = await self._asafe_get_spend(ctx.customer_id, period)
        return self._decide(ctx, model, period, budget, spent)

    async def _arecord(
        self, ctx: CallContext, d: Decision, requested: str, resp_model: Optional[str], usage: Any
    ) -> UsageEvent:
        cost = self._cost(d, resp_model, usage)
        spent: Any = d.spent_usd
        if d.hold_id and self._reserving:
            try:
                res = self._store.settle(
                    d.hold_id, ctx.customer_id, d.period, cost_usd=cost or 0.0, downgraded=d.downgraded
                )
                if inspect.isawaitable(res):
                    res = await res
                spent = res
            except Exception:
                if not self._fail_open:
                    raise
        elif cost is not None and cost > 0:
            try:
                res = self._store.add_spend(ctx.customer_id, d.period, cost)
                if inspect.isawaitable(res):
                    res = await res
                spent = res
            except Exception:
                if not self._fail_open:
                    raise
        event = self._event(ctx, d, requested, usage, cost, spent)
        if self._on_usage is not None:
            try:
                r = self._on_usage(event)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                pass  # never break the caller
        return event

    # ------------------------------------------------------------------ public API
    def check(
        self, customer_id: str, model: str, *, plan: Optional[str] = None, feature: Optional[str] = None
    ) -> Decision:
        """Decide whether a call may proceed, and with which model, without making it.

        Raises :class:`BudgetExceededError` at the cap and :class:`UnknownModelPriceError`
        for unpriced models (unless ``unknown_model="allow"``).
        """
        return self._check(_make_ctx(customer_id, plan, feature), model)

    async def acheck(
        self, customer_id: str, model: str, *, plan: Optional[str] = None, feature: Optional[str] = None
    ) -> Decision:
        """Async :meth:`check`; supports async stores and budget functions."""
        return await self._acheck(_make_ctx(customer_id, plan, feature), model)

    def status(self, customer_id: str, *, plan: Optional[str] = None, feature: Optional[str] = None) -> BudgetStatus:
        """Current spend/budget for a customer this period."""
        ctx = _make_ctx(customer_id, plan, feature)
        period = period_of(self._now())
        budget = self._budget_for(ctx)
        spent = self._safe_get_spend(ctx.customer_id, period)
        return BudgetStatus(period, spent, budget, max(0.0, budget - spent))

    async def astatus(
        self, customer_id: str, *, plan: Optional[str] = None, feature: Optional[str] = None
    ) -> BudgetStatus:
        """Async :meth:`status`; supports async stores and budget functions."""
        ctx = _make_ctx(customer_id, plan, feature)
        period = period_of(self._now())
        budget = await self._abudget_for(ctx)
        spent = await self._asafe_get_spend(ctx.customer_id, period)
        return BudgetStatus(period, spent, budget, max(0.0, budget - spent))

    def wrap(self, create: Callable[..., R]) -> Callable[..., R]:
        """Wrap an OpenAI-compatible ``chat.completions.create``.

        The returned function takes the same arguments plus keyword-only ``customer_id``
        (required), ``plan`` and ``feature``, which are *not* forwarded to the provider::

            guarded = guard.wrap(client.chat.completions.create)
            res = guarded(customer_id="cus_123", model="gpt-5.5", messages=[...])

        Async functions (``AsyncOpenAI().chat.completions.create``) are detected and get an
        async wrapper; use :meth:`wrap_async` to force that.
        """
        if _is_async_callable(create):
            return self.wrap_async(create)  # type: ignore[arg-type, return-value]

        @functools.wraps(create)
        def guarded(
            *args: Any,
            customer_id: Optional[str] = None,
            plan: Optional[str] = None,
            feature: Optional[str] = None,
            **params: Any,
        ) -> Any:
            ctx = _make_ctx(customer_id, plan, feature)
            requested = _requested_model(params)
            d = self._reserve_check(ctx, params, requested) if self._reserving else self._check(ctx, requested)
            final: Dict[str, Any] = {**params, "model": d.model}
            if params.get("stream"):
                final["stream_options"] = {**(params.get("stream_options") or {}), "include_usage": True}
                try:
                    stream = create(*args, **final)
                    _ensure_sync(stream)
                except BaseException:
                    self._release(ctx, d.period, d.hold_id)
                    raise

                def on_done(model: Optional[str], usage: Any) -> None:
                    self._record(ctx, d, requested, model, usage)

                return GuardedStream(stream, on_done)
            try:
                res = create(*args, **final)
                _ensure_sync(res)
            except BaseException:
                self._release(ctx, d.period, d.hold_id)
                raise
            self._record(ctx, d, requested, _field(res, "model"), _field(res, "usage"))
            return res

        return guarded

    def wrap_async(self, create: Callable[..., Awaitable[R]]) -> Callable[..., Awaitable[R]]:
        """Async variant of :meth:`wrap`, e.g. for ``AsyncOpenAI().chat.completions.create``."""

        @functools.wraps(create)
        async def guarded(
            *args: Any,
            customer_id: Optional[str] = None,
            plan: Optional[str] = None,
            feature: Optional[str] = None,
            **params: Any,
        ) -> Any:
            ctx = _make_ctx(customer_id, plan, feature)
            requested = _requested_model(params)
            d = (
                await self._areserve_check(ctx, params, requested)
                if self._reserving
                else await self._acheck(ctx, requested)
            )
            final: Dict[str, Any] = {**params, "model": d.model}
            if params.get("stream"):
                final["stream_options"] = {**(params.get("stream_options") or {}), "include_usage": True}
                try:
                    stream = await create(*args, **final)
                except BaseException:
                    await self._arelease(ctx, d.period, d.hold_id)
                    raise

                async def on_done(model: Optional[str], usage: Any) -> None:
                    await self._arecord(ctx, d, requested, model, usage)

                return GuardedAsyncStream(stream, on_done)
            try:
                res = await create(*args, **final)
            except BaseException:
                await self._arelease(ctx, d.period, d.hold_id)
                raise
            await self._arecord(ctx, d, requested, _field(res, "model"), _field(res, "usage"))
            return res

        return guarded


class _ReleaseAndRaise(Exception):
    def __init__(self, error: Exception, hold_id: Optional[str], period: str) -> None:
        super().__init__(str(error))
        self.error = error
        self.hold_id = hold_id
        self.period = period


def _ensure_sync(res: Any) -> None:
    # A coroutine here means an async provider was wrapped with the sync wrapper; recording it
    # would meter $0 and silently disable the cap, so refuse instead (the request was not sent).
    if inspect.isawaitable(res):
        _discard(res)
        raise TypeError(
            "llmbrake: the wrapped create() returned an awaitable. Use guard.wrap_async() for async clients."
        )


def create_guard(
    budget_usd: Union[float, BudgetFn],
    *,
    store: Union[UsageStore, AsyncUsageStore, None] = None,
    prices: Optional[PriceTable] = None,
    downgrade_at: float = 0.8,
    downgrade_map: Optional[Mapping[str, str]] = None,
    on_usage: Optional[OnUsage] = None,
    unknown_model: UnknownModelPolicy = "throw",
    fail_open: bool = True,
    now: Optional[Callable[[], datetime]] = None,
    estimate_usd: Union[float, EstimateFn, None] = None,
) -> Guard:
    """Create a :class:`Guard`.

    :param budget_usd: Monthly USD budget per customer: a number, or a function of
        :class:`CallContext` returning one (may be ``async`` for async calls). Must be ``>= 0``;
        use ``math.inf`` for unlimited.
    :param store: Where spend is kept. Default: a new :class:`MemoryStore`.
    :param prices: Model -> :class:`ModelPrice`. Default: :data:`DEFAULT_PRICES` (verify them!).
    :param downgrade_at: Fraction of the budget (0-1) at which ``downgrade_map`` applies. ``1`` disables.
    :param downgrade_map: ``{"expensive-model": "cheaper-model"}``.
    :param on_usage: Called with a :class:`UsageEvent` after each metered call. Errors are swallowed.
    :param unknown_model: ``"throw"`` (default) raises :class:`UnknownModelPriceError` before calling
        for unpriced models; ``"allow"`` lets them through and records ``cost_usd=None``.
    :param fail_open: If the store errors: ``True`` (default) lets calls through, ``False`` re-raises.
    :param now: Clock override for tests. Naive datetimes are treated as UTC.
    :param estimate_usd: Reserving stores only (e.g. ``HostedStore``): the USD to hold while a call is in
        flight, as a number or ``fn(ctx, params)``. Default: :func:`estimate_call_usd`.
    """
    return Guard(
        budget_usd,
        store=store,
        prices=prices,
        downgrade_at=downgrade_at,
        downgrade_map=downgrade_map,
        on_usage=on_usage,
        unknown_model=unknown_model,
        fail_open=fail_open,
        now=now,
        estimate_usd=estimate_usd,
    )
