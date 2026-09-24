"""
ComfyUI Plugin: ComfyUI-LLM-Lite

Calls an OpenAI-compatible v1/chat/completions API and outputs text.

Model list is fetched via a frontend button (POST /llm_lite/fetch_models),
no workflow execution required. The frontend fills the model dropdown
dynamically after a successful fetch.
"""

import base64
import io as _io_mod
import json
import logging
import threading
import requests
from typing_extensions import override
from urllib.parse import urlparse

import torch
from PIL import Image
from aiohttp import web
from server import PromptServer
import comfy.model_management as model_management
from comfy_api.latest import ComfyExtension, io


def _derive_base_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _tensor_to_data_url(t: torch.Tensor) -> str:
    """ComfyUI IMAGE [B,H,W,C] float0-1 -> PNG data URL for llama.cpp."""
    arr = (t[0].detach().cpu().clamp(0, 1).numpy() * 255).round().astype("uint8")
    img = Image.fromarray(arr[..., :3], mode="RGB")
    buf = _io_mod.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _resolve_endpoints(api_url: str) -> tuple[str, str]:
    url = api_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        chat_url = url
        base = url[: -len("/chat/completions")]
    else:
        chat_url = f"{url}/chat/completions"
        base = url
    if base.endswith("/v1"):
        models_url = f"{base}/models"
    else:
        models_url = f"{base}/v1/models"
    return chat_url, models_url


def _fetch_model_ids(api_url: str, api_key: str, timeout: int = 30) -> list[str]:
    _, models_url = _resolve_endpoints(api_url)
    resp = requests.get(
        models_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return [m["id"] for m in resp.json().get("data", []) if m.get("id")]


def _model_status(models_url: str, headers: dict, model: str, timeout: int) -> str | None:
    try:
        resp = requests.get(models_url, headers=headers, timeout=min(timeout, 30))
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        return None
    for m in resp.json().get("data", []):
        if m.get("id") == model or m.get("alias") == model:
            status = m.get("status")
            if isinstance(status, dict):
                return status.get("value")
            return None
    return None


def _ensure_model_loaded(
    base_url: str,
    models_url: str,
    headers: dict,
    model: str,
    timeout: int,
) -> None:
    """llama-server router: unloaded/sleeping worker causes 500 proxy error."""
    status = _model_status(models_url, headers, model, timeout)
    if status in (None, "loaded", "loading"):
        if status == "loading":
            _wait_model_loaded(models_url, headers, model, timeout)
        return
    try:
        resp = requests.post(
            f"{base_url}/models/load",
            headers=headers,
            json={"model": model},
            timeout=min(timeout, 30),
        )
        if resp.ok:
            _wait_model_loaded(models_url, headers, model, timeout)
        else:
            logging.warning("llamacpp /models/load HTTP %s: %s", resp.status_code, resp.text[:500])
    except requests.exceptions.RequestException as e:
        logging.warning("llamacpp /models/load failed: %s", e)


def _wait_model_loaded(models_url: str, headers: dict, model: str, timeout: int) -> None:
    import time

    status: str | None = None
    deadline = time.monotonic() + max(10, min(timeout, 180))
    while time.monotonic() < deadline:
        model_management.throw_exception_if_processing_interrupted()
        status = _model_status(models_url, headers, model, timeout)
        if status == "loaded":
            return
        if status in (None, "unloaded", "error"):
            break
        time.sleep(1.0)
    logging.warning("llamacpp model %r not loaded in time (status=%s)", model, status)


def _close_quiet(response) -> None:
    if response is None:
        return
    for close in (
        lambda: response.close(),
        lambda: response.raw.release_conn(),
        lambda: response.raw.close(),
    ):
        try:
            close()
        except Exception:
            pass


def _feed_sse_line(line: str, acc: dict) -> bool:
    """Fold one SSE line into acc. Returns True on terminal [DONE]/error."""
    if not line or not line.startswith("data:"):
        return False
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return data == "[DONE]"
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return False
    if chunk.get("error"):
        acc["error"] = chunk["error"]
        return True
    if chunk.get("usage"):
        acc["usage"] = chunk["usage"]
    if chunk.get("id"):
        acc["id"] = chunk["id"]
    if chunk.get("model"):
        acc["model"] = chunk["model"]
    choices = chunk.get("choices") or []
    if not choices:
        return False
    choice = choices[0] or {}
    for src in (choice.get("delta") or {}, choice.get("message") or {}):
        if src.get("content"):
            acc["content_parts"].append(src["content"])
        reasoning = src.get("reasoning_content") or src.get("reasoning")
        if reasoning:
            acc["reasoning_parts"].append(reasoning)
    if choice.get("finish_reason"):
        acc["finish_reason"] = choice["finish_reason"]
    return False


def _post_chat_stream(
    chat_url: str,
    headers: dict,
    payload: dict,
    timeout: int,
) -> tuple[int, str, dict]:
    """Stream chat/completions; close socket on ComfyUI cancel so llama-server stops.

    Returns (http_status, error_body, acc). Raises InterruptProcessingException on cancel.
    """
    box: dict = {
        "resp": None,
        "status": None,
        "error_body": "",
        "fatal": None,
        "done": False,
        "acc": {
            "content_parts": [],
            "reasoning_parts": [],
            "finish_reason": None,
            "usage": None,
            "error": None,
            "id": None,
            "model": None,
        },
    }

    def reader() -> None:
        try:
            model_management.throw_exception_if_processing_interrupted()
            resp = requests.post(
                chat_url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=timeout,
            )
            box["resp"] = resp
            if model_management.processing_interrupted():
                _close_quiet(resp)
                box["fatal"] = "interrupt"
                return
            box["status"] = resp.status_code
            if resp.status_code != 200:
                try:
                    resp.encoding = "utf-8"
                    box["error_body"] = (resp.text or "")[:2000]
                except Exception:
                    box["error_body"] = ""
                _close_quiet(resp)
                return
            resp.encoding = "utf-8"
            acc = box["acc"]
            for line in resp.iter_lines(decode_unicode=True):
                if model_management.processing_interrupted():
                    _close_quiet(resp)
                    box["fatal"] = "interrupt"
                    return
                if _feed_sse_line(line, acc):
                    break
            _close_quiet(resp)
            if acc.get("error"):
                box["status"] = 500
                box["error_body"] = json.dumps(acc["error"], ensure_ascii=False)[:2000]
        except model_management.InterruptProcessingException:
            _close_quiet(box.get("resp"))
            box["fatal"] = "interrupt"
        except requests.exceptions.RequestException as e:
            box["fatal"] = e
        except Exception as e:
            box["fatal"] = e
        finally:
            box["done"] = True

    worker = threading.Thread(target=reader, name="ComfyUI-LLM-Lite-chat", daemon=True)
    worker.start()

    while not box["done"]:
        if model_management.processing_interrupted():
            _close_quiet(box.get("resp"))
            worker.join(0.5)
            model_management.throw_exception_if_processing_interrupted()
        worker.join(0.15)

    if box["fatal"] == "interrupt":
        raise model_management.InterruptProcessingException()
    if model_management.processing_interrupted():
        model_management.throw_exception_if_processing_interrupted()
    if box["fatal"] is not None:
        raise ValueError(f"Failed to call OpenAI API: {box['fatal']}") from (
            box["fatal"] if isinstance(box["fatal"], BaseException) else None
        )
    status = box["status"]
    if status is None:
        raise ValueError("Failed to call OpenAI API: no response")
    return status, box["error_body"], box["acc"]


def _extract_message(acc: dict) -> dict:
    content = "".join(acc.get("content_parts") or [])
    reasoning = "".join(acc.get("reasoning_parts") or [])
    message: dict = {"role": "assistant", "content": content or None}
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _unload_model(base_url: str, headers: dict, model: str, timeout: int) -> None:
    try:
        requests.post(
            f"{base_url}/models/unload",
            headers=headers,
            json={"model": model},
            timeout=timeout,
        ).raise_for_status()
    except Exception as e:
        logging.warning("Failed to unload model via llamacpp: %s", e)


@PromptServer.instance.routes.post("/llm_lite/fetch_models")
async def fetch_models_route(request: web.Request) -> web.Response:
    data = await request.json()
    api_url = data.get("api_url", "")
    api_key = data.get("api_key", "")
    if not api_url:
        return web.json_response({"models": [], "error": "api_url is required"})
    try:
        models = _fetch_model_ids(api_url, api_key)
        return web.json_response({"models": models, "error": None})
    except Exception as e:
        logging.warning("Failed to fetch model list: %s", e)
        return web.json_response({"models": [], "error": str(e)})


class LLMChat(io.ComfyNode):
    """OpenAI-compatible chat completions node."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="LLMChat",
            display_name="LLM Chat",
            category="text/LLM",
            description="Call an OpenAI-compatible chat completions API and output text.",
            inputs=[
                io.String.Input(
                    "api_url",
                    default="http://127.0.0.1:8080/v1",
                    multiline=False,
                    tooltip="OpenAI-compatible base (.../v1) or chat completions endpoint",
                ),
                io.String.Input(
                    "api_key",
                    default="sk-********",
                    multiline=False,
                    placeholder="sk-********",
                    tooltip="API key (Bearer token)",
                ),
                io.String.Input(
                    "system_prompt",
                    default="",
                    multiline=True,
                    tooltip="System prompt",
                ),
                io.String.Input(
                    "user_prompt",
                    default="",
                    multiline=True,
                    tooltip="User prompt",
                ),
                io.Autogrow.Input(
                    "images",
                    template=io.Autogrow.TemplateNames(
                        io.Image.Input("image"),
                        names=[f"image_{i}" for i in range(1, 11)],
                        min=0,
                    ),
                    tooltip="Reference images; connected ones become <image1>, <image2>, ... sent to the model",
                ),
                io.Combo.Input(
                    "model",
                    options=[""],
                    default="",
                    tooltip="Model name (click the fetch button to populate the list)",
                ),
                io.Int.Input(
                    "seed",
                    default=1,
                    min=0,
                    max=0xffffffffffffffff,
                    control_after_generate=True,
                    tooltip="Sampling seed; control_after_generate: fixed/increment/decrement/randomize (randomize changes seed each queue and busts ComfyUI cache)",
                ),
                io.Float.Input(
                    "temperature",
                    default=1.0,
                    min=0.0,
                    max=2.0,
                    step=0.01,
                    tooltip="Sampling temperature",
                ),
                io.Float.Input(
                    "top_p",
                    default=0.95,
                    min=0.0,
                    max=1.0,
                    step=0.01,
                    tooltip="Nucleus sampling parameter",
                ),
                io.Int.Input(
                    "top_k",
                    default=20,
                    min=1,
                    max=100,
                    step=1,
                    tooltip="Top-k sampling parameter",
                ),
                io.Boolean.Input(
                    "enable_thinking",
                    default=False,
                    label_on="true",
                    label_off="false",
                    tooltip="Per-request thinking/reasoning toggle (chat_template_kwargs.enable_thinking; off also sends reasoning_budget_tokens=0)",
                ),
                io.Int.Input(
                    "max_tokens",
                    default=4096,
                    min=1,
                    max=32768,
                    step=1,
                    tooltip="Max tokens to generate",
                ),
                io.Int.Input(
                    "timeout",
                    default=300,
                    min=1,
                    max=3600,
                    step=1,
                    tooltip="Request timeout in seconds",
                ),
                io.Boolean.Input(
                    "unload_model",
                    default=False,
                    label_on="true",
                    label_off="false",
                    tooltip="Unload local model via llamacpp POST /models/unload after the call",
                ),
            ],
            outputs=[
                io.String.Output(display_name="generated_text"),
            ],
        )

    @classmethod
    def validate_inputs(cls, model: str, **kwargs) -> bool:
        return True

    @classmethod
    def execute(
        cls,
        api_url: str,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        images: dict | None,
        model: str,
        seed: int,
        temperature: float,
        top_p: float,
        top_k: int,
        enable_thinking: bool,
        max_tokens: int,
        timeout: int,
        unload_model: bool,
    ) -> io.NodeOutput:
        if not api_key:
            raise ValueError("API key is required but is empty")

        chat_url, models_url = _resolve_endpoints(api_url)
        base_url = _derive_base_url(api_url)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        _ensure_model_loaded(base_url, models_url, headers, model, timeout)

        image_list: list[str] = []
        if images:
            for name in sorted(images, key=lambda n: int(n.rsplit("_", 1)[-1])):
                t = images[name]
                if t is not None:
                    image_list.append(_tensor_to_data_url(t))

        if image_list:
            user_content = [
                {"type": "image_url", "image_url": {"url": url}} for url in image_list
            ] + [{"type": "text", "text": user_prompt}]
            system_content = [{"type": "text", "text": system_prompt}]
        else:
            user_content = user_prompt
            system_content = system_prompt

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "seed": seed,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if not enable_thinking:
            payload["reasoning_budget_tokens"] = 0

        payload["stream"] = True
        message: dict | None = None
        try:
            status, error_body, acc = _post_chat_stream(
                chat_url, headers, payload, timeout
            )
            if status != 200 and "proxy error" in (error_body or ""):
                logging.warning("llamacpp proxy error, reloading model and retrying once")
                _ensure_model_loaded(
                    base_url, models_url, headers, model, timeout
                )
                status, error_body, acc = _post_chat_stream(
                    chat_url, headers, payload, timeout
                )
            if status != 200:
                raise ValueError(
                    f"OpenAI API HTTP {status}: {(error_body or '')[:2000]}"
                )
            message = _extract_message(acc)
        except model_management.InterruptProcessingException:
            if unload_model:
                _unload_model(base_url, headers, model, timeout)
            raise
        except requests.exceptions.RequestException as e:
            if unload_model:
                _unload_model(base_url, headers, model, timeout)
            raise ValueError(f"Failed to call OpenAI API: {e}") from e

        content = message.get("content") or message.get("reasoning_content") or ""

        if unload_model:
            _unload_model(base_url, headers, model, timeout)

        return io.NodeOutput(content)


class LLMLiteExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [LLMChat]


async def comfy_entrypoint() -> LLMLiteExtension:
    return LLMLiteExtension()
