from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, fmean
from typing import Iterable

BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20_260_823


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quantile(sorted_values: list[float], probability: float) -> float:
    probability = min(1.0, max(0.0, probability))
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def bca_interval(
    observations: Iterable[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> dict:
    values = [float(value) for value in observations]
    if not values:
        raise ValueError("BCa interval requires observations")
    estimate = fmean(values)
    if len(values) == 1 or len(set(values)) == 1:
        return {
            "low": estimate,
            "high": estimate,
            "confidence": 0.95,
            "method": "BCa bootstrap",
            "resamples": resamples,
            "resample_unit": "scenario run",
            "degenerate": True,
        }
    rng = random.Random(seed)
    size = len(values)
    bootstrap = sorted(fmean(rng.choices(values, k=size)) for _ in range(resamples))
    less = sum(value < estimate for value in bootstrap)
    equal = sum(value == estimate for value in bootstrap)
    proportion = (less + 0.5 * equal) / resamples
    epsilon = 0.5 / resamples
    normal = NormalDist()
    bias = normal.inv_cdf(min(1.0 - epsilon, max(epsilon, proportion)))
    jackknife = [fmean(values[:index] + values[index + 1 :]) for index in range(size)]
    jack_mean = fmean(jackknife)
    differences = [jack_mean - value for value in jackknife]
    numerator = sum(value**3 for value in differences)
    denominator = 6.0 * (sum(value**2 for value in differences) ** 1.5)
    acceleration = numerator / denominator if denominator else 0.0
    adjusted = []
    for tail in (alpha / 2.0, 1.0 - alpha / 2.0):
        z_tail = normal.inv_cdf(tail)
        corrected = normal.cdf(
            bias + (bias + z_tail) / (1.0 - acceleration * (bias + z_tail))
        )
        adjusted.append(corrected)
    return {
        "low": _quantile(bootstrap, adjusted[0]),
        "high": _quantile(bootstrap, adjusted[1]),
        "confidence": 0.95,
        "method": "BCa bootstrap",
        "resamples": resamples,
        "resample_unit": "scenario run",
        "degenerate": False,
    }


def rate_summary(values: Iterable[bool], *, seed_offset: int = 0) -> dict:
    observations = [1.0 if value else 0.0 for value in values]
    if not observations:
        raise ValueError("rate requires observations")
    return {
        "numerator": int(sum(observations)),
        "denominator": len(observations),
        "rate": sum(observations) / len(observations),
        "ci_95": bca_interval(observations, seed=BOOTSTRAP_SEED + seed_offset),
    }


def write_json_atomic(
    path: Path, payload: dict, *, allow_replace: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not allow_replace:
        raise FileExistsError(f"refusing to replace receipt: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
