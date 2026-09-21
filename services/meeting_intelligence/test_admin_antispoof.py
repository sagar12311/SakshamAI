from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import torch

from admin_antispoof import AASIST_SAMPLE_COUNT, AASISTVerifier, prepare_waveform


class PrepareWaveformTests(unittest.TestCase):
    def test_repeats_short_audio_to_official_aasist_length(self) -> None:
        source = torch.linspace(-1.0, 1.0, 16_000).unsqueeze(0)
        prepared = prepare_waveform(
            {"waveform": source, "sample_rate": 16_000},
            device="cpu",
        )
        self.assertEqual(tuple(prepared.shape), (1, AASIST_SAMPLE_COUNT))
        self.assertTrue(torch.equal(prepared[0, :16_000], source[0]))

    def test_resamples_audio_before_padding(self) -> None:
        prepared = prepare_waveform(
            {"waveform": torch.ones(1, 8_000), "sample_rate": 8_000},
            device="cpu",
        )
        self.assertEqual(tuple(prepared.shape), (1, AASIST_SAMPLE_COUNT))
        self.assertTrue(torch.allclose(prepared, torch.ones_like(prepared)))

    def test_rejects_empty_audio(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "empty or invalid"):
            prepare_waveform(
                {"waveform": torch.empty(1, 0), "sample_rate": 16_000},
                device="cpu",
            )


class AASISTVerifierTests(unittest.TestCase):
    def test_loads_strict_state_and_returns_bonafide_probability(self) -> None:
        source = """\
import torch
import torch.nn as nn

class Model(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.anchor = nn.Parameter(torch.zeros(1))

    def forward(self, samples, Freq_aug=False):
        logits = torch.tensor([[0.0, 2.0]], device=samples.device)
        return samples[:, :1], logits.repeat(samples.shape[0], 1) + self.anchor
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            architecture = root / "AASIST.py"
            weights = root / "AASIST.pth"
            architecture.write_text(source, encoding="utf-8")
            torch.save({"anchor": torch.zeros(1)}, weights)

            verifier = AASISTVerifier(str(architecture), str(weights), device="cpu")
            score = verifier.score(
                {"waveform": torch.ones(1, 16_000), "sample_rate": 16_000}
            )

        self.assertAlmostEqual(score, 0.880797, places=5)


if __name__ == "__main__":
    unittest.main()
