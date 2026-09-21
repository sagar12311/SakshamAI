"""AASIST inference adapter for the private admin voice verifier.

The model is deliberately loaded only inside the private RTX worker. Audio and
model outputs are kept in memory and are never logged or persisted here.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from typing import Any


AASIST_SAMPLE_RATE = 16_000
AASIST_SAMPLE_COUNT = 64_600
AASIST_CONFIG = {
    "architecture": "AASIST",
    "nb_samp": AASIST_SAMPLE_COUNT,
    "first_conv": 128,
    "filts": [70, [1, 32], [32, 32], [32, 64], [64, 64]],
    "gat_dims": [64, 32],
    "pool_ratios": [0.5, 0.7, 0.5, 0.5],
    "temperatures": [2.0, 2.0, 100.0, 100.0],
}


def prepare_waveform(waveform: dict[str, Any], *, device: str):
    """Return a normalized ``[batch, 64600]`` AASIST input tensor."""
    import torch
    import torch.nn.functional as functional

    samples = waveform["waveform"].detach().float().reshape(-1)
    sample_rate = int(waveform["sample_rate"])
    if sample_rate <= 0 or samples.numel() == 0:
        raise RuntimeError("Admin verification audio is empty or invalid")
    if not torch.isfinite(samples).all():
        raise RuntimeError("Admin verification audio contains invalid samples")

    if sample_rate != AASIST_SAMPLE_RATE:
        target_count = max(1, round(samples.numel() * AASIST_SAMPLE_RATE / sample_rate))
        samples = functional.interpolate(
            samples.reshape(1, 1, -1),
            size=target_count,
            mode="linear",
            align_corners=False,
        ).reshape(-1)

    if samples.numel() >= AASIST_SAMPLE_COUNT:
        samples = samples[:AASIST_SAMPLE_COUNT]
    else:
        repeats = math.ceil(AASIST_SAMPLE_COUNT / samples.numel())
        samples = samples.repeat(repeats)[:AASIST_SAMPLE_COUNT]
    return samples.unsqueeze(0).to(device)


class AASISTVerifier:
    """Load the pinned AASIST architecture and expose a bona-fide probability."""

    def __init__(self, architecture_path: str, weights_path: str, *, device: str) -> None:
        import torch

        architecture = Path(architecture_path).expanduser().resolve()
        weights = Path(weights_path).expanduser().resolve()
        if not architecture.is_file():
            raise RuntimeError("Admin anti-spoof architecture path does not exist")
        if not weights.is_file():
            raise RuntimeError("Admin anti-spoof model path does not exist")

        spec = importlib.util.spec_from_file_location("saksham_pinned_aasist", architecture)
        if spec is None or spec.loader is None:
            raise RuntimeError("Admin anti-spoof architecture could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        model_class = getattr(module, "Model", None)
        if model_class is None:
            raise RuntimeError("Admin anti-spoof architecture has no Model class")

        model = model_class(dict(AASIST_CONFIG))
        state = torch.load(str(weights), map_location="cpu", weights_only=True)
        model.load_state_dict(state, strict=True)
        self.device = device
        self.model = model.to(device).eval()

    def score(self, waveform: dict[str, Any]) -> float:
        """Return the class-1 (bona-fide speech) probability in ``[0, 1]``."""
        import torch

        samples = prepare_waveform(waveform, device=self.device)
        with torch.inference_mode():
            _, logits = self.model(samples, Freq_aug=False)
            probability = torch.softmax(logits, dim=-1)[0, 1]
        value = float(probability.detach().cpu().item())
        if not math.isfinite(value):
            raise RuntimeError("Admin anti-spoof model returned an invalid score")
        return max(0.0, min(1.0, value))
