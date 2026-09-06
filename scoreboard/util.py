from __future__ import annotations
import logging, random, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def setup_logging(log_dir: Path | None = None, name: str = "scoreboard") -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{utcnow():%Y-%m-%d}.log")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    # quiet noisy libs
    for n in ("herbie", "cfgrib", "urllib3", "botocore", "s3fs", "fsspec"):
        logging.getLogger(n).setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)   # s3fs leaves sessions open at exit; harmless
    return log


def retry(fn: Callable[[], T], tries: int = 4, base: float = 2.0, max_sleep: float = 60.0,
          retry_on: tuple = (Exception,), log: logging.Logger | None = None, what: str = "") -> T:
    """Exponential backoff with jitter. Re-raises the last exception."""
    last: BaseException | None = None
    for i in range(tries):
        try:
            return fn()
        except retry_on as e:  # noqa: PERF203
            last = e
            if i == tries - 1:
                break
            sleep = min(max_sleep, base * (2 ** i)) * (0.7 + 0.6 * random.random())
            if log:
                log.warning("%s failed (%s); retry %d/%d in %.1fs", what or "call", e, i + 1, tries - 1, sleep)
            time.sleep(sleep)
    assert last is not None
    raise last


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def k_to_f(k):
    return (k - 273.15) * 9.0 / 5.0 + 32.0


def to_utc(x) -> "pd.Timestamp":
    """Coerce str/datetime/Timestamp (naive or aware) to a tz-aware UTC Timestamp."""
    import pandas as pd
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
