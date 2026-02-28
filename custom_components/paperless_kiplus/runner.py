"""Script runner for Paperless KIplus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import shlex

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_STATUS_UPDATED

_LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Result metadata for a script run."""

    status: str
    exit_code: int | None
    message: str


class PaperlessRunner:
    """Execute the configured Paperless KIplus command safely."""

    def __init__(self, hass: HomeAssistant, *, command: str, workdir: str, cooldown_seconds: int) -> None:
        self.hass = hass
        self.command = command
        self.workdir = workdir
        self.cooldown_seconds = cooldown_seconds

        self._lock = asyncio.Lock()
        self.running = False

        self.last_started: datetime | None = None
        self.last_finished: datetime | None = None
        self.last_exit_code: int | None = None
        self.last_status: str = "idle"
        self.last_message: str = "not started"
        self.last_stdout_tail: str = ""
        self.last_stderr_tail: str = ""

    @property
    def cooldown_until(self) -> datetime | None:
        """Return the next allowed run time if cooldown is active."""

        if self.last_finished is None:
            return None
        return self.last_finished + timedelta(seconds=self.cooldown_seconds)

    async def async_run(self, *, force: bool = False) -> RunResult:
        """Run the command unless already running or in cooldown."""

        if self._lock.locked() and not force:
            self.last_status = "skipped_running"
            self.last_message = "run skipped because another run is active"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        now = datetime.now(UTC)
        cooldown_until = self.cooldown_until
        if not force and cooldown_until is not None and now < cooldown_until:
            self.last_status = "cooldown"
            self.last_message = f"run skipped due to cooldown until {cooldown_until.isoformat()}"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        async with self._lock:
            self.running = True
            self.last_started = datetime.now(UTC)
            self.last_status = "running"
            self.last_message = "script is running"
            self._notify()

            try:
                args = shlex.split(self.command)
                if not args:
                    raise ValueError("configured command is empty")

                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=self.workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()

                self.last_stdout_tail = stdout.decode("utf-8", errors="replace")[-4000:]
                self.last_stderr_tail = stderr.decode("utf-8", errors="replace")[-4000:]
                self.last_exit_code = process.returncode

                if process.returncode == 0:
                    self.last_status = "success"
                    self.last_message = "script completed successfully"
                else:
                    self.last_status = "error"
                    self.last_message = f"script failed with exit code {process.returncode}"

                _LOGGER.info(
                    "Paperless KIplus run finished | status=%s exit_code=%s",
                    self.last_status,
                    self.last_exit_code,
                )
            except Exception as exc:  # noqa: BLE001
                self.last_status = "error"
                self.last_message = f"runner exception: {exc}"
                self.last_exit_code = None
                self.last_stderr_tail = str(exc)
                _LOGGER.exception("Paperless KIplus run crashed: %s", exc)
            finally:
                self.running = False
                self.last_finished = datetime.now(UTC)
                self._notify()

        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    def _notify(self) -> None:
        """Notify entities/sensors about runner state updates."""

        async_dispatcher_send(self.hass, SIGNAL_STATUS_UPDATED)
