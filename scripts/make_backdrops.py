"""Generate the play-page backdrop pack with Qwen-Image through ComfyUI.

    uv run --with pillow python scripts/make_backdrops.py --server http://HOST:8188 [--force]
        [--only scifi] [--only scifi/orbit]

Prompts come from rpg_llm/themes.py. Each image gets a fixed seed (from its theme and setting),
so re-running reproduces the pack; --seed-offset N gives a different take. Images are saved as
WebP in src/rpg_llm/static/backdrops/<theme>/<setting>.webp; existing ones are skipped unless
--force. Needs a ComfyUI with Qwen-Image 2.1 (bf16 denoiser, Qwen3-VL text encoder, VAE).
"""

import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from rpg_llm import themes  # noqa: E402

OUT = Path(__file__).parent.parent / "src" / "rpg_llm" / "static" / "backdrops"
WIDTH, HEIGHT = 1664, 928  # ~16:9, multiples of 32


def workflow(prompt: str, negative: str, seed: int, steps: int) -> dict:
    return {
        "unet": {"class_type": "UNETLoader",
                 "inputs": {"unet_name": "qwen_image_2.1_bf16.safetensors", "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader",
                 "inputs": {"clip_name": "qwen3vl_8b_int8_convrot.safetensors",
                            "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader",
                "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "encode": {"class_type": "TextEncodeQwenImage21",
                   "inputs": {"clip": ["clip", 0], "prompt": prompt, "negative_prompt": negative,
                              "resolution": 1024}},
        "latent": {"class_type": "EmptyLatentImage",
                   "inputs": {"width": WIDTH, "height": HEIGHT, "batch_size": 1}},
        "sample": {"class_type": "KSampler",
                   "inputs": {"model": ["unet", 0], "positive": ["encode", 0],
                              "negative": ["encode", 1], "latent_image": ["latent", 0],
                              "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                              "scheduler": "simple", "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage",
                 "inputs": {"images": ["decode", 0], "filename_prefix": "rpg_backdrop"}},
    }


def call(server: str, path: str, body: dict | None = None):
    req = urllib.request.Request(f"{server}{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def render(server: str, prompt: str, seed: int, steps: int) -> bytes:
    pid = json.loads(call(server, "/prompt", {"prompt": workflow(prompt, themes.NEGATIVE, seed, steps)}))["prompt_id"]
    return wait_image(server, pid)


def wait_image(server: str, pid: str) -> bytes:
    while True:
        hist = json.loads(call(server, f"/history/{pid}"))
        if pid in hist:
            status = hist[pid].get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(json.dumps(status.get("messages"))[:500])
            imgs = [i for o in hist[pid]["outputs"].values() for i in o.get("images", [])]
            if imgs:
                q = urllib.parse.urlencode({k: imgs[0][k] for k in ("filename", "subfolder", "type")})
                return call(server, f"/view?{q}")
        time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8188")
    ap.add_argument("--only", action="append", default=[], help="theme or theme/setting")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--steps", type=int, default=25)
    ap.add_argument("--seed-offset", type=int, default=0)
    ap.add_argument("--quality", type=int, default=78)
    ap.add_argument("--out", type=Path, default=OUT, help="folder to write into (default: the app's)")
    a = ap.parse_args()
    home = a.out / f"{themes.HOME['file']}.webp"
    if (not a.only or "home" in a.only) and (a.force or not home.exists()):
        t0 = time.time()
        pid = json.loads(call(a.server, "/prompt", {"prompt": workflow(
            themes.HOME["prompt"], themes.HOME["negative"], themes.HOME["seed"], a.steps)}))["prompt_id"]
        png = wait_image(a.server, pid)
        home.parent.mkdir(parents=True, exist_ok=True)
        Image.open(io.BytesIO(png)).convert("RGB").save(home, "WEBP", quality=a.quality, method=6)
        print(f"home: {time.time() - t0:.0f}s", flush=True)
    for theme, t in themes.THEMES.items():
        for setting in t["settings"]:
            key = f"{theme}/{setting}"
            if a.only and not any(key == o or theme == o for o in a.only):
                continue
            dest = a.out / theme / f"{setting}.webp"
            if dest.exists() and not a.force:
                print(f"skip {key} (exists)")
                continue
            seed = zlib.crc32(key.encode()) + a.seed_offset
            t0 = time.time()
            png = render(a.server, themes.prompt(theme, setting), seed, a.steps)
            dest.parent.mkdir(parents=True, exist_ok=True)
            Image.open(io.BytesIO(png)).convert("RGB").save(dest, "WEBP", quality=a.quality, method=6)
            print(f"{key}: {time.time() - t0:.0f}s, {dest.stat().st_size // 1024} KB (seed {seed})",
                  flush=True)


main()
