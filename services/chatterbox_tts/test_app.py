import unittest

from services.chatterbox_tts.app import (
    TONE_TEMPERATURES,
    seed_generation,
    validate_api_key,
    validate_device_name,
)


class ConfigurationTests(unittest.TestCase):
    def test_accepts_generated_secret(self) -> None:
        validate_api_key("a" * 32)

    def test_rejects_missing_short_and_placeholder_secrets(self) -> None:
        for api_key in ("", "too-short", "replace-with-a-long-random-token"):
            with self.subTest(api_key=api_key):
                with self.assertRaisesRegex(RuntimeError, "generated secret"):
                    validate_api_key(api_key)

    def test_accepts_supported_accelerators(self) -> None:
        for device in ("cpu", "cuda", "mps"):
            with self.subTest(device=device):
                validate_device_name(device)

    def test_rejects_unknown_accelerator(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unsupported TTS_DEVICE"):
            validate_device_name("metal")

    def test_tone_sampling_stays_within_one_voice_range(self) -> None:
        self.assertLessEqual(max(TONE_TEMPERATURES.values()) - min(TONE_TEMPERATURES.values()), 0.12)

    def test_generation_seed_is_applied_to_cpu_and_cuda(self) -> None:
        class FakeCuda:
            seed = None

            def manual_seed_all(self, seed: int) -> None:
                self.seed = seed

        class FakeTorch:
            seed = None
            cuda = FakeCuda()

            def manual_seed(self, seed: int) -> None:
                self.seed = seed

        fake_torch = FakeTorch()
        seed_generation(fake_torch, "cuda", 42)
        self.assertEqual(42, fake_torch.seed)
        self.assertEqual(42, fake_torch.cuda.seed)


if __name__ == "__main__":
    unittest.main()
