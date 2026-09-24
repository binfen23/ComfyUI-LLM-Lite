"""
ComfyUI Plugin: ComfyUI-LLM-Lite

Package initialization. Exposes WEB_DIRECTORY so ComfyUI loads the
frontend extension (button + dynamic model dropdown), and the
comfy_entrypoint so ComfyUI registers the node class.
"""

from .nodes import LLMChat, LLMLiteExtension, comfy_entrypoint

WEB_DIRECTORY = "./web"

__all__ = [
    "LLMChat",
    "LLMLiteExtension",
    "comfy_entrypoint",
    "WEB_DIRECTORY",
]
