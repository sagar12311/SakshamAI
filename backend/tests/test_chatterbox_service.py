import tempfile
import unittest
from pathlib import Path

from core.chatterbox_service import ChatterboxServiceManager


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class ChatterboxServiceManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_only_loopback_urls_are_managed(self) -> None:
        self.assertTrue(ChatterboxServiceManager.is_loopback_url("http://127.0.0.1:8100"))
        self.assertTrue(ChatterboxServiceManager.is_loopback_url("http://localhost:8100"))
        self.assertFalse(ChatterboxServiceManager.is_loopback_url("http://100.70.1.2:8100"))

    async def test_reuses_healthy_service_without_spawning(self) -> None:
        spawned = False

        async def spawn(script, cwd, log_handle):
            nonlocal spawned
            spawned = True
            return FakeProcess()

        manager = ChatterboxServiceManager(
            enabled=True,
            base_url="http://127.0.0.1:8100",
            health_check=lambda: self._result(True),
            process_factory=spawn,
        )

        self.assertTrue(await manager.start())
        self.assertFalse(spawned)
        self.assertEqual("external", manager.status["state"])
        self.assertFalse(manager.status["owned_by_backend"])

    async def test_starts_and_stops_bundled_service(self) -> None:
        health_results = iter((False, False, True))
        process = FakeProcess()

        async def health_check() -> bool:
            return next(health_results)

        async def spawn(script, cwd, log_handle):
            return process

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "run_macos.sh").touch()
            manager = ChatterboxServiceManager(
                enabled=True,
                base_url="http://127.0.0.1:8100",
                service_dir=root,
                log_path=root / "chatterbox.log",
                health_check=health_check,
                port_check=lambda: self._result(False),
                process_factory=spawn,
                poll_interval_seconds=0,
            )

            self.assertTrue(await manager.start())
            self.assertEqual("healthy", manager.status["state"])
            self.assertTrue(manager.status["owned_by_backend"])
            await manager.stop()

        self.assertTrue(process.terminated)

    async def test_waits_for_occupied_port_without_spawning_duplicate(self) -> None:
        health_results = iter((False, False, True))
        spawned = False

        async def health_check() -> bool:
            return next(health_results)

        async def spawn(script, cwd, log_handle):
            nonlocal spawned
            spawned = True
            return FakeProcess()

        manager = ChatterboxServiceManager(
            enabled=True,
            base_url="http://127.0.0.1:8100",
            health_check=health_check,
            port_check=lambda: self._result(True),
            process_factory=spawn,
            poll_interval_seconds=0,
        )

        self.assertTrue(await manager.start())
        self.assertFalse(spawned)
        self.assertEqual("external", manager.status["state"])
        self.assertFalse(manager.status["owned_by_backend"])

    async def test_remote_service_is_never_spawned(self) -> None:
        manager = ChatterboxServiceManager(
            enabled=True,
            base_url="http://100.70.1.2:8100",
            health_check=lambda: self._result(False),
        )

        self.assertFalse(await manager.start())
        self.assertEqual("remote", manager.status["state"])

    @staticmethod
    async def _result(value: bool) -> bool:
        return value


if __name__ == "__main__":
    unittest.main()
