"""Shared LLM call context and usage persistence.

The provider layer does not know which feature initiated a request.  Callers
can set a short-lived context so token usage can be tied back to a session and
stage without putting session arguments into every provider API.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class LLMContext:
    session_id: int | None = None
    stage: str = "unknown"


_current: ContextVar[LLMContext] = ContextVar(
    "replay_llm_context", default=LLMContext()
)


def current_llm_context() -> LLMContext:
    return _current.get()


@contextmanager
def llm_context(session_id: int | None = None, stage: str = "unknown") -> Iterator[None]:
    token = _current.set(LLMContext(session_id=session_id, stage=stage or "unknown"))
    try:
        yield
    finally:
        _current.reset(token)
