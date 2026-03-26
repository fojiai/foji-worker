import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


def with_retry(max_attempts: int = 3, delay_seconds: float = 2.0, backoff: float = 2.0):
    """Decorator: retry a function on exception with exponential backoff."""
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: Exception | None = None
            wait = delay_seconds
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "%s attempt %d/%d failed: %s", fn.__name__, attempt, max_attempts, exc
                    )
                    if attempt < max_attempts:
                        time.sleep(wait)
                        wait *= backoff
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator
