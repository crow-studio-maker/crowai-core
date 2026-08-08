"""Private local inference backend for CrowAI Chat V1.0."""

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
        raise LocalModelError("Chat V1.0 contains an invalid package-local file reference.")
    candidate = (BASE_DIR / relative).resolve()
    if candidate != BASE_DIR and BASE_DIR not in candidate.parents:
        raise LocalModelError("Chat V1.0 file reference escapes its version directory.")
    if area:
        area_root = (BASE_DIR / area).resolve()
        if candidate.parent != area_root and area_root not in candidate.parents:
            raise LocalModelError(f"Chat V1.0 {area} file must stay inside its package {area} directory.")
    return candidate


def _runtime_file(value: Any) -> Path:
    try:
        return resolve_runtime_file(BASE_DIR, str(value or ""))
    except ValueError as exc:
        raise LocalModelError("V1.0 contains an invalid package-local runtime reference.") from exc


class LocalModelError(RuntimeError):
    """Raised when the private local model backend cannot answer."""


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise LocalModelError("Configuration file is missing from chat/v1.0.")

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalModelError("The local model configuration is invalid.") from exc

    if not isinstance(value, dict):
        raise LocalModelError("The local model configuration must be a JSON object.")

    return value


class LocalChatEngine:
    """Starts and communicates with the bundled model runtime."""

    def __init__(self) -> None:
        self.config = _load_config()

        self.runtime_path = _runtime_file(self.config["runtime_file"])

        self.model_path = _package_file(self.config["model_file"], area="model")

        self.prompt_path = _package_file(self.config["system_prompt_file"], area="prompts")

        self.host = str(self.config.get("host", "127.0.0.1"))
        self.port = int(self.config.get("port", 18081))
        self.base_url = f"http://{self.host}:{self.port}"
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise LocalModelError("Only a loopback inference host is allowed for Chat V1.0.")
        if not 1024 <= self.port <= 65535:
            raise LocalModelError("The Chat V1.0 inference port is invalid.")

        self.state_dir = model_state_dir(BASE_DIR, "chat", "v1.0")
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any | None = None
        self._start_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cancelled = threading.Event()
        self._owns_process = False

        atexit.register(self.stop)

    def _read_system_prompt(self) -> str:
        try:
            prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LocalModelError(
                f"System prompt could not be read: {self.prompt_path}"
            ) from exc

        if not prompt:
            raise LocalModelError("The system prompt is empty.")

        return prompt

    def _request_json(
        self,
        endpoint: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        data = None
        method = "GET"

        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "POST"

        request = urllib.request.Request(
            url=f"{self.base_url}{endpoint}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise LocalModelError(
                f"Local model request failed with HTTP {exc.code}: "
                f"{details[:500]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LocalModelError(
                "The local model backend could not be reached."
            ) from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LocalModelError(
                "The local model backend returned invalid JSON."
            ) from exc

        if not isinstance(result, dict):
            raise LocalModelError(
                "The local model backend returned an invalid response."
            )

        return result

    def is_ready(self) -> bool:
        if self._process is None or not self._owns_process or self._process.poll() is not None:
            return False
        try:
            result = self._request_json("/v1/models", timeout=1.5)
        except LocalModelError:
            return False
        data = result.get("data")
        if not isinstance(data, list):
            return False
        alias = str(self.config.get("model_alias", "")).strip()
        advertised = {str(item.get("id") or item.get("model") or "").strip() for item in data if isinstance(item, dict)}
        return not alias or alias in advertised

    def _validate_files(self) -> None:
        runtime_state = local_file_state(self.runtime_path, kind="runtime")
        if runtime_state != "ready":
            raise LocalModelError(
                "Local runtime file is missing or invalid in chat/v1.0/runtime."
            )

        model_state = local_file_state(self.model_path, kind="model")
        if model_state != "ready":
            raise LocalModelError(
                "Local model file is missing or invalid in chat/v1.0/model."
            )

        if not self.prompt_path.is_file():
            raise LocalModelError(
                "System prompt file is missing from chat/v1.0/prompts."
            )

    def _build_command(self) -> list[str]:
        command = [
            str(self.runtime_path),
            "-m",
            str(self.model_path),
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
            "--alias",
            str(self.config.get("model_alias", "crowai-chat-v1")),
        ]

        gpu_layers = self.config.get("gpu_layers", "all")
        command.extend(["--gpu-layers", str(gpu_layers)])

        if bool(self.config.get("flash_attention", True)):
            command.extend(["--flash-attn", "on"])

        return command

    def start(self) -> None:
        """Start the bundled backend once and wait until it is ready."""

        if self._process is not None and self._owns_process and self._process.poll() is None:
            if self.is_ready():
                return
            self._wait_until_ready()
            return

        with self._start_lock:
            if self._cancelled.is_set():
                raise LocalModelError("The Chat V1.0 request was cancelled.")
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
                startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            else:
                process_options["start_new_session"] = True

            try:
                # Serialize publication of the child process with cancellation.
                # Once Popen returns, cancel() can never observe _process without
                # also seeing ownership, so delete-to-cancel cannot orphan startup.
                with self._state_lock:
                    if self._cancelled.is_set():
                        raise LocalModelError("The Chat V1.0 request was cancelled.")
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
                raise LocalModelError(
                    "The bundled local model backend could not be started."
                ) from exc

            try:
                self._wait_until_ready()
            except Exception:
                self.stop()
                raise

    def _wait_until_ready(self) -> None:
        timeout = max(
            10,
            int(self.config.get("startup_timeout_seconds", 180)),
        )
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                raise LocalModelError("The Chat V1.0 request was cancelled.")
            process = self._process

            if process is not None and process.poll() is not None:
                raise LocalModelError(
                    "The local model backend stopped during startup. "
                    "Check instance/model_state/chat/v1.0/engine.log."
                )

            if self.is_ready():
                return

            time.sleep(0.75)

        raise LocalModelError(
            f"The local model did not become ready within {timeout} seconds. "
            "Check instance/model_state/chat/v1.0/engine.log."
        )

    def generate(self, messages: list[dict[str, str]], *, maximum_tokens: int | None = None) -> str:
        """Generate one non-streaming chat response."""

        if self._cancelled.is_set():
            raise LocalModelError("The Chat V1.0 request was cancelled.")
        self.start()
        if self._cancelled.is_set():
            raise LocalModelError("The Chat V1.0 request was cancelled.")

        prepared_messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": self._read_system_prompt(),
            }
        ]

        for item in messages:
            if not isinstance(item, dict):
                continue

            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()

            if role not in {"system", "user", "assistant"}:
                continue

            if not content:
                continue

            prepared_messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

        payload = {
            "model": str(
                self.config.get("model_alias", "crowai-chat-v1")
            ),
            "messages": prepared_messages,
            "temperature": float(
                self.config.get("temperature", 0.65)
            ),
            "top_p": float(self.config.get("top_p", 0.9)),
            "top_k": int(self.config.get("top_k", 40)),
            "repeat_penalty": float(
                self.config.get("repeat_penalty", 1.08)
            ),
            "max_tokens": min(
                max(32, int(maximum_tokens or self.config.get("max_output_tokens", 1024))),
                int(self.config.get("max_output_tokens", 1024)),
            ),
            "stream": False,
        }

        timeout = max(
            30,
            int(self.config.get("request_timeout_seconds", 240)),
        )

        # The selected configuration is single-slot, so serialize requests.
        with self._request_lock:
            if self._cancelled.is_set():
                raise LocalModelError("The Chat V1.0 request was cancelled.")
            result = self._request_json(
                "/v1/chat/completions",
                payload=payload,
                timeout=timeout,
            )
            if self._cancelled.is_set():
                raise LocalModelError("The Chat V1.0 request was cancelled.")

        try:
            answer = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalModelError(
                "The local model response did not contain an answer."
            ) from exc

        answer = str(answer).strip()

        if not answer:
            raise LocalModelError("The local model generated an empty answer.")

        return answer

    def begin_request(self) -> None:
        """Reset cancellation exactly once at the start of a new Core request."""
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


_ENGINE = LocalChatEngine()


def begin_request() -> None:
    _ENGINE.begin_request()


def generate_reply(messages: list[dict[str, str]], *, maximum_tokens: int | None = None) -> str:
    """Public internal entry used by this model package."""

    return _ENGINE.generate(messages, maximum_tokens=maximum_tokens)


def cancel() -> None:
    _ENGINE.cancel()


def shutdown() -> None:
    """Stop the private backend owned by this model package."""

    _ENGINE.stop()

def health_check() -> dict[str, Any]:
    states = {
        "runtime": local_file_state(_ENGINE.runtime_path, kind="runtime"),
        "model": local_file_state(_ENGINE.model_path, kind="model"),
        "prompt": "ready" if _ENGINE.prompt_path.is_file() else "missing",
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
