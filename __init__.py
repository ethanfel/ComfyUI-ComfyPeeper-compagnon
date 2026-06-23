"""
ComfyPeeper Companion — bridges ComfyUI with the ComfyPeeper Discord plugin.

Drop this folder into ComfyUI/custom_nodes/ (as `comfypeeper`) and restart ComfyUI.
It adds three routes on the ComfyUI server:

  GET  /comfypeeper/info   advertise a friendly name + capabilities (so the plugin can
                           label this server and light up companion features)
  POST /comfypeeper/load   { workflow }  -> push the workflow to open editor tabs, which
                           the bundled frontend extension loads via app.loadGraphData()
  POST /comfypeeper/send   { filename, subfolder, type } -> upload that output image to
                           the configured Discord webhook (the image carries its workflow
                           in PNG metadata, so ComfyPeeper detects it on the Discord side)

Configure by copying config.example.json -> config.json and setting `name` (and
`discord_webhook` if you want "Send to Discord").
"""

import asyncio
import io
import json
import os

import aiohttp
from aiohttp import web

import folder_paths
from server import PromptServer

VERSION = "1.0.0"
_DIR = os.path.dirname(os.path.realpath(__file__))
_CONFIG_PATH = os.path.join(_DIR, "config.json")


def _config():
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return {
        "name": (cfg.get("name") or "ComfyUI").strip(),
        "discord_webhook": (cfg.get("discord_webhook") or "").strip(),
    }


def _resolve_image_path(filename, subfolder, type_):
    """Resolve a /view-style image ref to a file path, clamped inside the type's base dir."""
    if not filename:
        return None
    base = folder_paths.get_directory_by_type(type_ or "output")
    if not base:
        return None
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, subfolder or "", filename))
    # block path traversal outside the base directory
    if os.path.commonpath([base, target]) != base:
        return None
    return target if os.path.isfile(target) else None


async def _upload_to_webhook(webhook, filename, data, content=""):
    """POST image bytes to a Discord webhook as multipart/form-data. Returns (ok, status, body)."""
    form = aiohttp.FormData()
    form.add_field("payload_json", json.dumps({"content": content}))
    form.add_field("files[0]", data, filename=filename, content_type="application/octet-stream")
    async with aiohttp.ClientSession() as session:
        async with session.post(webhook, data=form) as resp:
            return resp.status in (200, 204), resp.status, (await resp.text())[:300]


routes = PromptServer.instance.routes


@routes.get("/comfypeeper/info")
async def comfypeeper_info(_request):
    cfg = _config()
    caps = ["load"]
    if cfg["discord_webhook"]:
        caps.append("send")
    return web.json_response({"app": "ComfyPeeper", "name": cfg["name"], "version": VERSION, "caps": caps})


@routes.post("/comfypeeper/load")
async def comfypeeper_load(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    workflow = data.get("workflow")
    if workflow is None:
        return web.json_response({"ok": False, "error": "no workflow"}, status=400)
    # broadcast to every connected tab; the frontend extension calls app.loadGraphData()
    PromptServer.instance.send_sync("comfypeeper.load", {"workflow": workflow})
    return web.json_response({"ok": True})


@routes.post("/comfypeeper/send")
async def comfypeeper_send(request):
    webhook = _config()["discord_webhook"]
    if not webhook:
        return web.json_response({"ok": False, "error": "no webhook configured"}, status=400)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    path = _resolve_image_path(data.get("filename"), data.get("subfolder", ""), data.get("type", "output"))
    if not path:
        return web.json_response({"ok": False, "error": "image not found"}, status=404)

    try:
        with open(path, "rb") as f:
            file_bytes = f.read()
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    try:
        ok, status, body = await _upload_to_webhook(webhook, os.path.basename(path), file_bytes, (data.get("content") or "").strip())
        return web.json_response({"ok": ok, "status": status, "data": body})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


class SendToDiscord:
    """Output node: send each incoming image (with workflow metadata) to the Discord webhook."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"images": ("IMAGE",)},
            "optional": {
                "message": ("STRING", {"default": "", "multiline": True}),
                "filename_prefix": ("STRING", {"default": "ComfyPeeper"}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ()
    FUNCTION = "send"
    OUTPUT_NODE = True
    CATEGORY = "ComfyPeeper"

    def send(self, images, message="", filename_prefix="ComfyPeeper", prompt=None, extra_pnginfo=None):
        webhook = _config()["discord_webhook"]
        if not webhook:
            print("[ComfyPeeper] SendToDiscord: no discord_webhook in config.json — skipping")
            return {"ui": {"images": []}}

        import numpy as np  # lazy: keep the routes working even if image libs are unusual
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        ui_images = []
        for i, image in enumerate(images):
            arr = np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)
            # embed workflow + prompt into the PNG, same as ComfyUI's SaveImage, so ComfyPeeper detects it
            meta = PngInfo()
            try:
                if prompt is not None:
                    meta.add_text("prompt", json.dumps(prompt))
                for k, v in (extra_pnginfo or {}).items():
                    meta.add_text(k, json.dumps(v))
            except Exception as e:
                print(f"[ComfyPeeper] SendToDiscord: metadata embed failed: {e}")
            buf = io.BytesIO()
            img.save(buf, format="PNG", pnginfo=meta, compress_level=4)
            data = buf.getvalue()

            # run the upload on the server's event loop (this node runs in a worker thread)
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _upload_to_webhook(webhook, f"{filename_prefix}_{i:03d}.png", data, (message or "").strip()),
                    PromptServer.instance.loop,
                )
                ok, status, _ = fut.result(timeout=60)
                if not ok:
                    print(f"[ComfyPeeper] SendToDiscord: webhook returned {status}")
            except Exception as e:
                print(f"[ComfyPeeper] SendToDiscord: send failed: {e}")

            # drop a temp copy so the node previews what it sent
            try:
                tmp_name = f"comfypeeper_{i:03d}.png"
                with open(os.path.join(temp_dir, tmp_name), "wb") as f:
                    f.write(data)
                ui_images.append({"filename": tmp_name, "subfolder": "", "type": "temp"})
            except Exception:
                pass
        return {"ui": {"images": ui_images}}


WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {"ComfyPeeperSendToDiscord": SendToDiscord}
NODE_DISPLAY_NAME_MAPPINGS = {"ComfyPeeperSendToDiscord": "Send to Discord (ComfyPeeper)"}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print(f"[ComfyPeeper] companion loaded (v{VERSION}) — name: {_config()['name']!r}")
