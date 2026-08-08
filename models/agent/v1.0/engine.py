"""Local multimodal CUDA inference backend for CrowAI Agent V1.0."""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from models.local_files import local_file_state, resolve_runtime_file
from models.runtime_state import model_state_dir, open_private_log


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


def _package_file(value: Any, *, area: str | None = None) -> Path:
    raw = str(value or "").strip()
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise LocalAgentError("Agent V1.0 contains an invalid package-local file reference.")
    candidate = (BASE_DIR / relative).resolve()
    if candidate != BASE_DIR and BASE_DIR not in candidate.parents:
        raise LocalAgentError("Agent V1.0 file reference escapes its version directory.")
    if area:
        area_root = (BASE_DIR / area).resolve()
        if candidate.parent != area_root and area_root not in candidate.parents:
            raise LocalAgentError(f"Agent V1.0 {area} file must stay inside its package {area} directory.")
    return candidate


def _runtime_file(value: Any) -> Path:
    try:
        return resolve_runtime_file(BASE_DIR, str(value or ""))
    except ValueError as exc:
        raise LocalAgentError("Agent V1.0 contains an invalid package-local runtime reference.") from exc


class LocalAgentError(RuntimeError):
    """Raised when the local Agent model cannot complete a request."""


def _load_config() -> dict[str, Any]:
    try:
        value = json.loads(
            CONFIG_PATH.read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise LocalAgentError(
            "Configuration file is missing from agent/v1.0."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalAgentError(
            "The Agent V1.0 configuration is invalid."
        ) from exc

    if not isinstance(value, dict):
        raise LocalAgentError(
            "The Agent V1.0 configuration must be a JSON object."
        )

    return value


class LocalAgentEngine:
    """Manage one warm llama-server process with optional vision input."""

    def __init__(self) -> None:
        self.config = _load_config()

        self.runtime_path = _runtime_file(self.config["runtime_file"])
        self.model_path = _package_file(self.config["model_file"], area="model")
        self.mmproj_path = _package_file(self.config["mmproj_file"], area="model")
        self.system_prompt_path = _package_file(self.config["system_prompt_file"], area="prompts")

        self.host = str(self.config.get("host", "127.0.0.1"))
        self.port = int(self.config.get("port", 18083))
        self.base_url = f"http://{self.host}:{self.port}"
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise LocalAgentError("Only a loopback inference host is allowed for Agent V1.0.")
        if not 1024 <= self.port <= 65535:
            raise LocalAgentError("The Agent V1.0 inference port is invalid.")

        self.state_dir = model_state_dir(BASE_DIR, "agent", "v1.0")
        self._process: subprocess.Popen[Any] | None = None
        self._log_handle: Any | None = None
        self._owns_process = False

        self._start_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cancelled = threading.Event()
        self._idle_timer: threading.Timer | None = None
        self._idle_generation = 0

        atexit.register(self.stop)

    def _validate_files(self) -> None:
        binary_requirements = (
            ("runtime", self.runtime_path, "runtime"),
            ("model", self.model_path, "model"),
            ("vision projector", self.mmproj_path, "vision_projector"),
        )
        for label, path, kind in binary_requirements:
            if local_file_state(path, kind=kind) != "ready":
                raise LocalAgentError(
                    f"Agent {label} file is missing or invalid in agent/v1.0."
                )
        if not self.system_prompt_path.is_file():
            raise LocalAgentError(
                "Agent system prompt file is missing from agent/v1.0."
            )

    def _read_system_prompt(self) -> str:
        try:
            prompt = self.system_prompt_path.read_text(
                encoding="utf-8"
            ).strip()
        except OSError as exc:
            raise LocalAgentError(
                "The Agent system prompt could not be read."
            ) from exc

        if not prompt:
            raise LocalAgentError(
                "The Agent system prompt is empty."
            )

        return prompt

    def _build_command(self) -> list[str]:
        command = [
            str(self.runtime_path),
            "-m",
            str(self.model_path),
            "--mmproj",
            str(self.mmproj_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--ctx-size",
            str(int(self.config.get("context_size", 4096))),
            "--parallel",
            str(int(self.config.get("parallel", 1))),
            "--batch-size",
            str(int(self.config.get("batch_size", 512))),
            "--ubatch-size",
            str(int(self.config.get("ubatch_size", 256))),
            "--threads",
            str(int(self.config.get("threads", 6))),
            "--threads-batch",
            str(int(self.config.get("threads_batch", 6))),
            "-ngl",
            str(int(self.config.get("gpu_layers", 99))),
            "--alias",
            str(
                self.config.get(
                    "model_alias",
                    "crowai-agent-v1",
                )
            ),
        ]

        if bool(self.config.get("flash_attention", True)):
            command.extend(["--flash-attn", "on"])

        if bool(self.config.get("jinja", True)):
            command.append("--jinja")

        return command

    def _request_json(
        self,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        method = "GET"
        body: bytes | None = None

        if payload is not None:
            method = "POST"
            body = json.dumps(
                payload,
                ensure_ascii=False,
            ).encode("utf-8")

        request = urllib.request.Request(
            url=f"{self.base_url}{endpoint}",
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:
                raw = response.read().decode(
                    "utf-8",
                    errors="replace",
                )
        except urllib.error.HTTPError as exc:
            details = exc.read().decode(
                "utf-8",
                errors="replace",
            )
            raise LocalAgentError(
                f"Local Agent HTTP error {exc.code}: "
                f"{details[:1500]}"
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            raise LocalAgentError(
                "The Agent backend could not be reached."
            ) from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LocalAgentError(
                "The Agent backend returned invalid JSON."
            ) from exc

        if not isinstance(result, dict):
            raise LocalAgentError(
                "The Agent backend returned an invalid response."
            )

        return result

    def is_ready(self) -> bool:
        if self._process is None or not self._owns_process or self._process.poll() is not None:
            return False
        try:
            value = self._request_json("/v1/models", timeout=1.5)
        except LocalAgentError:
            return False
        data = value.get("data")
        if not isinstance(data, list):
            return False
        alias = str(self.config.get("model_alias", "")).strip()
        advertised = {str(item.get("id") or item.get("model") or "").strip() for item in data if isinstance(item, dict)}
        return not alias or alias in advertised

    def _cancel_idle_timer(self) -> None:
        with self._state_lock:
            self._idle_generation += 1
            timer = self._idle_timer
            self._idle_timer = None

            if timer is not None:
                timer.cancel()

    def _schedule_idle_shutdown(self) -> None:
        seconds = int(
            self.config.get("idle_shutdown_seconds", 150)
        )

        if seconds <= 0:
            return

        with self._state_lock:
            self._idle_generation += 1
            generation = self._idle_generation

            if self._idle_timer is not None:
                self._idle_timer.cancel()

            timer = threading.Timer(
                seconds,
                self._idle_shutdown_callback,
                args=(generation,),
            )
            timer.daemon = True
            self._idle_timer = timer
            timer.start()

    def _idle_shutdown_callback(self, generation: int) -> None:
        with self._state_lock:
            if generation != self._idle_generation:
                return
            self._idle_timer = None

        if not self._request_lock.acquire(blocking=False):
            self._schedule_idle_shutdown()
            return

        try:
            with self._state_lock:
                if generation != self._idle_generation:
                    return
            self._stop_process()
        finally:
            self._request_lock.release()

    def start(self) -> None:
        self._cancel_idle_timer()
        if self._cancelled.is_set():
            raise LocalAgentError("The Agent V1.0 request was cancelled.")

        if self._process is not None and self._owns_process and self._process.poll() is None:
            if self.is_ready():
                return
            self._wait_until_ready()
            return

        with self._start_lock:
            if self._cancelled.is_set():
                raise LocalAgentError("The Agent V1.0 request was cancelled.")
            if self._process is not None and self._owns_process and self._process.poll() is None:
                self._wait_until_ready()
                return

            self._validate_files()

            log_path = self.state_dir / "engine.log"
            self._log_handle = open_private_log(log_path)

            creation_flags = 0
            startup_info = None
            process_options: dict[str, Any] = {}

            if os.name == "nt":
                creation_flags = (
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
                startup_info = subprocess.STARTUPINFO()
                startup_info.dwFlags |= (
                    subprocess.STARTF_USESHOWWINDOW
                )
            else:
                process_options["start_new_session"] = True

            try:
                with self._state_lock:
                    if self._cancelled.is_set():
                        raise LocalAgentError("The Agent V1.0 request was cancelled.")
                    process = subprocess.Popen(
                        self._build_command(),
                        cwd=str(self.runtime_path.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=self._log_handle,
                        stderr=subprocess.STDOUT,
                        creationflags=creation_flags,
                        startupinfo=startup_info,
                        **process_options,
                    )
                    self._process = process
                    self._owns_process = True
            except OSError as exc:
                self._close_log()
                raise LocalAgentError(
                    "The Agent backend could not be started."
                ) from exc

            try:
                self._wait_until_ready()
            except Exception:
                self.stop()
                raise

    def _wait_until_ready(self) -> None:
        timeout = max(
            10,
            int(
                self.config.get(
                    "startup_timeout_seconds",
                    240,
                )
            ),
        )
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                raise LocalAgentError("The Agent V1.0 request was cancelled.")
            if (
                self._process is not None
                and self._process.poll() is not None
            ):
                raise LocalAgentError(
                    "The Agent backend stopped during startup. "
                    "Check instance/model_state/agent/v1.0/engine.log."
                )

            if self.is_ready():
                return

            time.sleep(0.5)

        raise LocalAgentError(
            f"The Agent model was not ready within {timeout} seconds."
        )

    def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        maximum_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        if self._cancelled.is_set():
            raise LocalAgentError("The Agent V1.0 request was cancelled.")
        self.start()
        self._cancel_idle_timer()

        prepared: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self._read_system_prompt(),
            }
        ]

        for item in messages:
            if not isinstance(item, dict):
                continue

            role = str(item.get("role") or "").strip().lower()
            content = item.get("content")

            if role not in {"system", "user", "assistant"}:
                continue

            if isinstance(content, str):
                if content.strip():
                    prepared.append(
                        {
                            "role": role,
                            "content": content.strip(),
                        }
                    )
            elif isinstance(content, list) and content:
                prepared.append(
                    {
                        "role": role,
                        "content": content,
                    }
                )

        requested = int(
            maximum_tokens
            or self.config.get(
                "synthesis_output_tokens",
                2600,
            )
        )
        absolute = int(
            self.config.get(
                "absolute_max_output_tokens",
                3200,
            )
        )

        payload: dict[str, Any] = {
            "model": str(
                self.config.get(
                    "model_alias",
                    "crowai-agent-v1",
                )
            ),
            "messages": prepared,
            "temperature": float(
                temperature
                if temperature is not None
                else self.config.get("temperature", 0.25)
            ),
            "top_p": float(self.config.get("top_p", 0.9)),
            "top_k": int(self.config.get("top_k", 40)),
            "repeat_penalty": float(
                self.config.get("repeat_penalty", 1.05)
            ),
            "max_tokens": min(max(32, requested), absolute),
            "stream": False,
            "cache_prompt": True,
        }

        if json_mode:
            payload["response_format"] = {
                "type": "json_object"
            }

        timeout = max(
            60,
            int(
                self.config.get(
                    "request_timeout_seconds",
                    900,
                )
            ),
        )

        try:
            with self._request_lock:
                if self._cancelled.is_set():
                    raise LocalAgentError("The Agent V1.0 request was cancelled.")
                result = self._request_json(
                    "/v1/chat/completions",
                    payload=payload,
                    timeout=timeout,
                )
                if self._cancelled.is_set():
                    raise LocalAgentError("The Agent V1.0 request was cancelled.")
        finally:
            self._schedule_idle_shutdown()

        try:
            answer = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalAgentError(
                "The Agent response did not contain an answer."
            ) from exc

        answer = str(answer).strip()

        if not answer:
            raise LocalAgentError(
                "The Agent model produced an empty answer."
            )

        return answer

    def begin_request(self) -> None:
        """Reset cancellation exactly once when Core starts a new Agent turn."""
        self._cancelled.clear()

    def _stop_process(self) -> None:
        with self._state_lock:
            process = self._process
            owns_process = self._owns_process

        if process is not None and owns_process and process.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        process.terminate()
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (subprocess.TimeoutExpired, OSError):
                    pass

        with self._state_lock:
            if self._process is process:
                self._process = None
                self._owns_process = False
        self._close_log()

    def cancel(self) -> None:
        """Cancel active inference immediately and release the package-owned backend."""
        self._cancelled.set()
        self._cancel_idle_timer()
        self._stop_process()

    def stop(self) -> None:
        """Stop the package-owned backend without waiting for an active HTTP request."""
        self.cancel()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except OSError:
                pass
        self._log_handle = None


_ENGINE = LocalAgentEngine()


def begin_request() -> None:
    _ENGINE.begin_request()


def generate_response(
    messages: list[dict[str, Any]],
    *,
    maximum_tokens: int | None = None,
    temperature: float | None = None,
    json_mode: bool = False,
) -> str:
    return _ENGINE.generate(
        messages,
        maximum_tokens=maximum_tokens,
        temperature=temperature,
        json_mode=json_mode,
    )


def _cancel_active() -> None:
    _ENGINE.cancel()


def shutdown() -> None:
    _ENGINE.stop()

def health_check() -> dict[str, Any]:
    states = {
        "runtime": local_file_state(_ENGINE.runtime_path, kind="runtime"),
        "model": local_file_state(_ENGINE.model_path, kind="model"),
        "mmproj": local_file_state(_ENGINE.mmproj_path, kind="vision_projector"),
        "prompt": "ready" if _ENGINE.system_prompt_path.is_file() else "missing",
    }
    backend_running = _ENGINE.is_ready()
    if backend_running:
        status = "backend_running"
    elif "missing" in states.values():
        status = "missing_local_files"
    elif "invalid" in states.values():
        status = "invalid_local_files"
    else:
        status = "runnable"
    return {
        "ok": all(value == "ready" for value in states.values()),
        "status": status,
        "files": {key: value == "ready" for key, value in states.items()},
        "invalid_requirements": [key for key, value in states.items() if value == "invalid"],
        "backend_running": backend_running,
    }
