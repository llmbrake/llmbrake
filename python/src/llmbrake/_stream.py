"""Stream proxies that record usage when a streamed response ends."""

from __future__ import annotations

from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    Iterator,
    Optional,
    TypeVar,
)

from ._prices import _field

T = TypeVar("T")

OnDone = Callable[[Optional[str], Any], None]
AsyncOnDone = Callable[[Optional[str], Any], Awaitable[None]]


class _Tracker:
    _llmbrake_usage: Any
    _llmbrake_model: Optional[str]

    def _observe(self, chunk: Any) -> None:
        usage = _field(chunk, "usage")
        if usage:
            self._llmbrake_usage = usage
        model = _field(chunk, "model")
        if model:
            self._llmbrake_model = model


class GuardedStream(_Tracker, Generic[T]):
    """Proxy around a sync stream (e.g. ``openai.Stream``).

    Yields the provider's chunks unchanged, remembers the last ``usage`` and ``model``
    it sees, and records the cost exactly once when the stream ends: on exhaustion, on an
    error, on ``close()`` / leaving a ``with`` block, or when a ``for`` loop over it is
    exited early with ``break`` (the loop's generator is closed by CPython at that point).
    Any other attribute (``response``, ...) is read from the wrapped stream.
    """

    def __init__(self, stream: Any, on_done: OnDone) -> None:
        self._llmbrake_stream = stream
        self._llmbrake_on_done = on_done
        self._llmbrake_iter: Optional[Iterator[T]] = None
        self._llmbrake_usage: Any = None
        self._llmbrake_model: Optional[str] = None
        self._llmbrake_done = False

    # -- internals -------------------------------------------------------------
    def _source(self) -> Iterator[T]:
        if self._llmbrake_iter is None:
            self._llmbrake_iter = iter(self._llmbrake_stream)
        return self._llmbrake_iter

    def _finish(self) -> None:
        if self._llmbrake_done:
            return
        self._llmbrake_done = True
        self._llmbrake_on_done(self._llmbrake_model, self._llmbrake_usage)

    def _generate(self) -> Iterator[T]:
        source = self._source()
        try:
            for chunk in source:
                self._observe(chunk)
                yield chunk
        finally:
            self._finish()

    # -- public ----------------------------------------------------------------
    def __iter__(self) -> Iterator[T]:
        return self._generate()

    def __next__(self) -> T:
        try:
            chunk = next(self._source())
        except BaseException:
            self._finish()
            raise
        self._observe(chunk)
        return chunk

    def close(self) -> None:
        """Close the underlying stream (if it can be closed) and record usage seen so far."""
        try:
            close = getattr(self._llmbrake_stream, "close", None)
            if callable(close):
                close()
        finally:
            self._finish()

    def __enter__(self) -> GuardedStream[T]:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_llmbrake_"):
            raise AttributeError(name)
        return getattr(self._llmbrake_stream, name)


class GuardedAsyncStream(_Tracker, Generic[T]):
    """Async counterpart of :class:`GuardedStream` (wraps e.g. ``openai.AsyncStream``).

    Usage is recorded on exhaustion, on an error, on ``await close()`` / ``aclose()`` or
    leaving an ``async with`` block. After an early ``break`` out of ``async for``, the
    event loop finalizes the iterator asynchronously, so use ``async with`` (or call
    ``await stream.close()``) when you need the usage recorded deterministically.
    """

    def __init__(self, stream: Any, on_done: AsyncOnDone) -> None:
        self._llmbrake_stream = stream
        self._llmbrake_on_done = on_done
        self._llmbrake_iter: Optional[AsyncIterator[T]] = None
        self._llmbrake_usage: Any = None
        self._llmbrake_model: Optional[str] = None
        self._llmbrake_done = False

    def _source(self) -> AsyncIterator[T]:
        if self._llmbrake_iter is None:
            self._llmbrake_iter = self._llmbrake_stream.__aiter__()
        return self._llmbrake_iter

    async def _finish(self) -> None:
        if self._llmbrake_done:
            return
        self._llmbrake_done = True
        await self._llmbrake_on_done(self._llmbrake_model, self._llmbrake_usage)

    async def _generate(self) -> AsyncIterator[T]:
        source = self._source()
        try:
            async for chunk in source:
                self._observe(chunk)
                yield chunk
        finally:
            await self._finish()

    def __aiter__(self) -> AsyncIterator[T]:
        return self._generate()

    async def __anext__(self) -> T:
        try:
            chunk = await self._source().__anext__()
        except BaseException:
            await self._finish()
            raise
        self._observe(chunk)
        return chunk

    async def close(self) -> None:
        """Close the underlying stream (if it can be closed) and record usage seen so far."""
        try:
            close = getattr(self._llmbrake_stream, "close", None) or getattr(self._llmbrake_stream, "aclose", None)
            if callable(close):
                res = close()
                if hasattr(res, "__await__"):
                    await res
        finally:
            await self._finish()

    aclose = close

    async def __aenter__(self) -> GuardedAsyncStream[T]:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_llmbrake_"):
            raise AttributeError(name)
        return getattr(self._llmbrake_stream, name)
