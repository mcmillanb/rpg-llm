"""Optional image generation (portraits, scene art) through ComfyUI or an OpenAI-style API.

Everything returns WebP bytes at the requested size. ComfyUI uses a built-in Qwen-Image 2.1
text-to-image workflow (model file names come from the admin settings); OpenAI-style servers get
a standard /images/generations request, and the result is scaled to the requested size (many
only offer 1024x1024).
"""

import asyncio
import base64
import io
import json
import random
import urllib.parse

import httpx2
from PIL import Image

from rpg_llm.config import ImageGen

NEGATIVE = ("text, letters, watermark, logo, signature, frame, border, extra fingers, "
            "deformed face, blurry")
PORTRAIT_STYLE = ("Head-and-shoulders character portrait, {look}, painterly illustration. The face "
                  "is brightly and clearly lit with warm key light and a soft rim light, rich "
                  "vibrant natural colours, crisp detail, a colourful softly blurred background "
                  "from their world, looking slightly off-camera, no text. {prompt}")
LOOKS = {"scifi": "science-fiction", "fantasy": "fantasy", "horror": "gothic 1920s horror",
         "cyberpunk": "neon cyberpunk", "plain": "tabletop role-playing game"}


class ImageError(RuntimeError):
    pass


def _to_webp(data: bytes, width: int, height: int, quality: int = 84) -> bytes:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.size != (width, height):
        img = img.resize((width, height), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "WEBP", quality=quality, method=6)
    return out.getvalue()


def _comfy_workflow(cfg: ImageGen, prompt: str, negative: str, w: int, h: int, seed: int) -> dict:
    return {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": cfg.unet, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": cfg.clip, "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": cfg.vae}},
        "encode": {"class_type": "TextEncodeQwenImage21",
                   "inputs": {"clip": ["clip", 0], "prompt": prompt, "negative_prompt": negative,
                              "resolution": 1024}},
        "latent": {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "sample": {"class_type": "KSampler",
                   "inputs": {"model": ["unet", 0], "positive": ["encode", 0],
                              "negative": ["encode", 1], "latent_image": ["latent", 0],
                              "seed": seed, "steps": cfg.steps, "cfg": 1.0, "sampler_name": "euler",
                              "scheduler": "simple", "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "rpg_llm"}},
    }


async def _comfyui(cfg: ImageGen, prompt: str, negative: str, w: int, h: int, seed: int,
                   timeout: float) -> bytes:
    base = cfg.base_url.rstrip("/")
    async with httpx2.AsyncClient(timeout=30) as http:
        r = await http.post(f"{base}/prompt", json={"prompt": _comfy_workflow(cfg, prompt, negative, w, h, seed)})
        if r.status_code != 200:
            raise ImageError(f"ComfyUI refused the workflow: {r.text[:300]}")
        pid = r.json()["prompt_id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            hist = (await http.get(f"{base}/history/{pid}")).json()
            if pid in hist:
                status = hist[pid].get("status", {})
                if status.get("status_str") == "error":
                    raise ImageError("ComfyUI error: " + json.dumps(status.get("messages"))[:300])
                imgs = [i for o in hist[pid].get("outputs", {}).values() for i in o.get("images", [])]
                if imgs:
                    q = urllib.parse.urlencode({k: imgs[0][k] for k in ("filename", "subfolder", "type")})
                    return (await http.get(f"{base}/view?{q}")).content
            await asyncio.sleep(1)
    raise ImageError("timed out waiting for ComfyUI")


async def _openai(cfg: ImageGen, prompt: str, w: int, h: int, timeout: float) -> bytes:
    base = cfg.base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    size = "1024x1024" if w == h else ("1536x1024" if w > h else "1024x1536")
    body = {"model": cfg.model, "prompt": prompt, "size": size, "n": 1}
    async with httpx2.AsyncClient(timeout=timeout) as http:
        r = await http.post(f"{base}/images/generations", json=body, headers=headers)
        if r.status_code >= 400:
            raise ImageError(f"image API error {r.status_code}: {r.text[:300]}")
        item = (r.json().get("data") or [{}])[0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            return (await http.get(item["url"])).content
    raise ImageError("the image API returned no image")


async def generate(cfg: ImageGen, prompt: str, width: int = 512, height: int = 512,
                   negative: str = NEGATIVE, seed: int | None = None, timeout: float = 240) -> bytes:
    if not cfg.enabled:
        raise ImageError("no image generator is set up (admin → Image generation)")
    seed = random.randrange(2**31) if seed is None else seed
    if cfg.kind == "comfyui":
        raw = await _comfyui(cfg, prompt, negative, width, height, seed, timeout)
    else:
        raw = await _openai(cfg, prompt, width, height, timeout)
    try:
        return _to_webp(raw, width, height)
    except Exception as e:
        raise ImageError(f"couldn't read the generated image: {e}")


def portrait_prompt(look: str, prompt: str) -> str:
    return PORTRAIT_STYLE.format(look=LOOKS.get(look, LOOKS["plain"]), prompt=prompt)
