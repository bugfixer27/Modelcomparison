"""Load config/*.yaml and .env. Nothing else in the package hardcodes tunables."""
from __future__ import annotations
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("SCOREBOARD_ROOT", Path(__file__).resolve().parent.parent))


@dataclass
class Config:
    main: dict
    sites: dict
    regimes: dict
    env: dict
    root: Path

    @property
    def store(self) -> Path:
        return self.root / self.main["paths"]["store"]

    @property
    def site_data(self) -> Path:
        return self.root / self.main["paths"]["site_data"]

    @property
    def herbie_cache(self) -> Path:
        return self.root / self.main["paths"]["herbie_cache"]

    @property
    def models(self) -> dict:
        return {k: v for k, v in self.main["models"].items() if v.get("enabled", True)}

    @property
    def variables(self) -> list[str]:
        return list(self.main["variables"])

    @property
    def lead_bins(self) -> list[tuple[int, int]]:
        return [tuple(b) for b in self.main["lead_bins"]]

    def model_fxx(self, model: str) -> list[int]:
        spec = self.main["models"][model]["fxx"]
        if isinstance(spec, dict):
            return list(range(spec["start"], spec["stop"] + 1, spec["step"]))
        return list(spec)

    def model_step_h(self, model: str) -> int:
        spec = self.main["models"][model]["fxx"]
        return spec["step"] if isinstance(spec, dict) else 1

    def precip_var(self, model: str) -> str:
        return f"precip{self.main['models'][model]['precip_window_h']}h"

    def model_vars(self, model: str) -> list[str]:
        """Variable ids this model can be verified on (rh2m is derived from t2m/td2m)."""
        f = self.main["models"][model]["fields"]
        out = [v for v in ("t2m", "td2m", "mslp", "vis") if v in f]
        if "t2m" in f and "td2m" in f:
            out.append("rh2m")
        if "precip" in f:
            out.append(self.precip_var(model))
        return out


@lru_cache(maxsize=1)
def load(root: Path | None = None) -> Config:
    root = Path(root) if root else ROOT
    load_dotenv(root / ".env")
    main = yaml.safe_load((root / "config" / "scoreboard.yaml").read_text())
    s = yaml.safe_load((root / "config" / "sites.yaml").read_text())
    env = {k: os.environ.get(k, "") for k in ("ZENTRA_TOKEN", "ZENTRA_DEVICE_SN", "GIT_REMOTE")}
    return Config(main=main, sites=s["sites"], regimes=s["regimes"], env=env, root=root)
