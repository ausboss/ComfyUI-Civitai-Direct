<div align="center">
  <h1>ComfyUI-Civitai-Direct</h1>
  <p><strong>Browse Civitai like normal. Click ⚡. Models and workflows land in the right ComfyUI folder.</strong></p>
  <p>
    <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT License">
    <img src="https://img.shields.io/badge/release-0.1.0-00b4aa?style=flat-square" alt="Release 0.1.0">
  </p>
  <p>Part of the <a href="https://github.com/ausboss/ComfyUI-AusBoss">AusBoss</a> node family.</p>
</div>

A ComfyUI custom node + Tampermonkey userscripts. No separate server, no Python setup, no config wizard, no paywall or license key — destination folders come straight from ComfyUI's own `folder_paths` (your `extra_model_paths.yaml` is honored automatically). Models are sorted by type and base model (`loras/sdxl/`, `checkpoints/sd15/`, `diffusion_models/flux1/`, ...), workflow ZIPs are safely extracted into ComfyUI's workflow library, and gallery images save into your Pictures folder with their prompt and generation metadata. Every model gets the ecosystem-standard `.civitai.info` sidecar plus a preview image, downloads are SHA256-verified, duplicates are detected instead of re-downloaded, and the button shows live progress — including "already in your library" and "ComfyUI offline" before you even click.

<!-- TODO: GIF of the ⚡ button on a Civitai model page -->

## Install

**1. The custom node** — clone into your ComfyUI `custom_nodes` folder and restart ComfyUI:

```
cd ComfyUI/custom_nodes
git clone https://github.com/ausboss/ComfyUI-Civitai-Direct
```

**2. The userscript** — install [Tampermonkey](https://www.tampermonkey.net/), then click:

- [civitai-direct.user.js](https://github.com/ausboss/ComfyUI-Civitai-Direct/raw/refs/heads/main/userscripts/civitai-direct.user.js) — download button on model pages
- [civitai-direct-grid.user.js](https://github.com/ausboss/ComfyUI-Civitai-Direct/raw/refs/heads/main/userscripts/civitai-direct-grid.user.js) — ⚡ button on model cards (optional)
- [civitai-direct-images.user.js](https://github.com/ausboss/ComfyUI-Civitai-Direct/raw/refs/heads/main/userscripts/civitai-direct-images.user.js) — ⬇ button on gallery images + prompt/workflow copy buttons (optional)

That's it. With ComfyUI running, open any Civitai model page and the **⚡ Direct to Server** button appears next to the native download button.

## API key (for login-gated models)

Public models download without any key. Some need a logged-in account (and all of them do if the creator says so) — create a key at [civitai.com/user/account](https://civitai.com/user/account) → **API Keys**, then use **any one** of these (checked in this order):

1. the `CIVITAI_API_KEY` environment variable, set before launching ComfyUI
2. a `.env` file inside this node's folder containing `CIVITAI_API_KEY=your-key-here`
3. a `civitai_api_key.txt` file inside this node's folder containing just the key

The two files are re-read on every download — add the key and click ⚡ again, no ComfyUI restart needed. Both filenames are gitignored so an update can never commit them.

**To check what the node sees**, open `http://127.0.0.1:8188/civitai/health` (your port): `has_civitai_api_key` tells you whether a key loaded and `api_key_source` tells you where from.

**An API key is not a skeleton key.** *Early Access* versions are time-gated to supporters — they need the early-access purchase/subscription on your account, and no API key changes that. The userscripts warn you up front: on grid cards and model pages the button turns **yellow ⚡ Early Access**, and the grid button won't fire on a single click (click twice if you've actually purchased the version). If a download is attempted anyway, the error names the reason and the date the version unlocks for everyone.

## Where files go

| Civitai type | Destination |
|---|---|
| Checkpoint (SD 1.5 / SDXL / Pony / Illustrious ...) | `checkpoints/<base model>/` |
| Checkpoint (Flux, Wan, LTX, other diffusion models) | `diffusion_models/<base model>/` |
| LoRA / LyCORIS / DoRA | `loras/<base model>/` |
| Embedding | `embeddings/<base model>/` |
| VAE / ControlNet / Upscaler / Text Encoder / CLIP / Hypernetwork | matching folder, `<base model>/` subfolder |
| CLIPVision / UNet / MotionModule / Detection | `clip_vision/`, `diffusion_models/`, `animatediff_models/`, `ultralytics/` |
| Workflow / Workflows | `user/<user>/workflows/civitai-direct/<base model>/<workflow>/` |
| Gallery images | `<your Pictures folder>/Civitai/` |

Base-model folders use lowercase, space-free slugs aligned with Civitai's own AIR family tokens — `sd15`, `sdxl`, `pony`, `illustrious`, `flux1`, `krea2`, `wan-video-22-t2v-a14b` — the spelling shared workflows most often expect. Families the mapping has never heard of get their slug from the version's AIR urn, so new base models sort themselves correctly on day one.

**Metadata that other tools read too.** Every model gets `<stem>.civitai.info` — the verbatim Civitai model-version record, the same file SD-WebUI's Civitai Helper, ComfyUI LoRA loaders with Civitai panels, and ComfyUI-Image-Saver use for trigger words and offline model info — plus `<stem>.preview.<ext>`, the preview-image convention A1111/Forge and Lora Manager display (your own `<stem>.png` always outranks it, so replacing a preview is just dropping a file next to the model). By default the tamest available image is chosen and models with nothing at PG/PG13 get no preview; see the settings below. Since the sidecar is written at download time, your library keeps its metadata even if the model is later removed from Civitai. Prefer this tool's richer records as well? `CIVITAI_SIDECARS=full` additionally writes the `<file>.json` + `<file>.md` pair from earlier releases. New files show up in ComfyUI's loaders immediately — no restart.

Images save as the *original* upload (PNG originals keep their embedded generation metadata) plus a `.json` sidecar, the prompt as a `.txt`, and — when the image carries a ComfyUI graph — a ready-to-load `.workflow.json`.

**Optional settings** — environment variables or the same `.env` file as the API key (re-read per download, no restart needed):

```
CIVITAI_IMAGE_DIR=D:\my\image\folder      # gallery images (default: Pictures/Civitai)
CIVITAI_WORKFLOW_DIR=D:\my\workflow\folder # workflows (default: ComfyUI's workflow library)
CIVITAI_SIDECARS=full                      # also write the <file>.json + .md pair (default: info)
CIVITAI_PREVIEWS=any                       # sfw (default) | any (Civitai's cover) | off
```

Relocated/OneDrive Pictures folders and Linux XDG dirs are honored. `/civitai/health` shows the resolved `images_dir`, `workflows_dir` and the active `sidecars`/`previews` modes.

**Duplicates are skipped, not copied.** Two checks run before any download: a version already on disk is recognized by its sidecar — this tool's, or a `.civitai.info` written by *any* other tool — and a same-named file from anywhere else (manual download, another downloader) is SHA256-verified against Civitai's published hash — if the bytes match, the node adopts it, writes the missing sidecars, and reports `already_exists`. Both checks follow your reorganizing: a model you moved into a subfolder — or a different base-model folder of the same type — is still recognized, with or without its sidecars. Only a same-named file with *different* content (Civitai filenames collide across versions constantly) is saved with a ` (1)` suffix, so nothing is ever overwritten. Fresh downloads are SHA256-verified too: a transfer that doesn't match Civitai's published hash is quarantined as `.corrupt` and reported as an error instead of posing as a model.

**What the button tells you.** ⚡ teal — ready. 📁 violet — this version is already in your library (the tooltip names the file; checked automatically when the page loads). 🔌 red — ComfyUI isn't running; start it and click to retry. ⏳/⬇ % — sending / downloading. ✅ green — saved. ⚡ yellow — Early Access (see above).

If you run ComfyUI on a port other than 8188/8189, add it to `SERVER_PORTS` at the top of the userscript.

## Troubleshooting

- **Button doesn't appear at all (Chrome)** — Tampermonkey 5.x needs Chrome's **Developer mode** (chrome://extensions) or the **Allow User Scripts** toggle (Chrome 138+) before any userscript runs. This is the #1 cause.
- **Button shows 🔌 ComfyUI Offline** — is ComfyUI running? Open `http://127.0.0.1:8188/civitai/health` (your port) — you should see JSON. The button re-checks every ~45 s and whenever you click it.
- **Button doesn't appear on the page** — check Tampermonkey is enabled for civitai.com (and civitai.red — the scripts run on both).
- **"requires a logged-in account"** — add an API key (see above), then check `/civitai/health` reports it loaded.
- **"Early Access"** — no key fixes this one; the version is still supporter-only on Civitai's side. Wait out the window or subscribe to the creator.
- **Wrong folder?** — open an issue with the model link; type/base-model mappings live in `__init__.py` and are easy to extend.

## Image & workflow tools

Civitai workflow assets are supported here: ZIPs are extracted, direct JSON/PNG artifacts are preserved, and metadata stays outside the workflow picker. Gallery images are too, via the images userscript: each image card gets an outline showing what generation data it has (green = full ComfyUI graph, blue = prompt/parameters, red = nothing), hover buttons copy the prompt / A1111-style info block / ComfyUI nodes to the clipboard, and ⬇ saves the original image with its metadata. The standalone-server variant of these tools lives in the sibling project [easy-civitai-downloader](https://github.com/ausboss/easy-civitai-downloader).

## Development

See [`AGENTS.md`](AGENTS.md) for working rules, compatibility contracts (sidecar schema, userscript payloads), and the validation checklist.

## Is this safe?

Fair question for something that connects your browser to your filesystem. The receipts:

**Who can talk to it?** The node adds routes to the ComfyUI server you already run — nothing new is listening. ComfyUI binds to `127.0.0.1` unless you launched it with `--listen`, and the userscripts are only *allowed* to talk to that address; Tampermonkey enforces it from the header:

```
// @connect      127.0.0.1
```

**Where can it write?** Only into folders derived from ComfyUI's `folder_paths` (model folders, the requesting user's workflow library and metadata folder), your Pictures `Civitai/` folder for images, and any folder *you* point `CIVITAI_IMAGE_DIR` / `CIVITAI_WORKFLOW_DIR` at. Every path component built from Civitai data is sanitized first, and workflow ZIP entries are checked before extraction so they cannot escape the destination folder:

```python
value = re.sub(r"[<>:\"/\\|?*\x00-\x1F]", "_", value)   # safe_filename()
```

Downloads stream to a `.part` file that only gets renamed once the transfer finishes — a dropped connection can't leave a truncated `.safetensors` masquerading as a real model.

**Your API key** lives in your environment or in gitignored files inside this folder. It is never written into sidecars or shown in the browser: the download URL is stored pre-token, and every error message passes through redaction before it's stored or logged:

```python
def redact_civitai_token(text):
    return re.sub(r"([?&]token=)[^&\s]+", r"\1REDACTED", str(text or ""))
```

**What code runs?** No compiled blobs, no install scripts, no pip installs — one Python file using only what ComfyUI already ships, and two userscripts you can read in full before installing. That's also your audit path: the whole attack surface is `__init__.py` plus `userscripts/`.

## Does this send my data to Civitai or anyone else?

**One site, total.** Every outbound request in the codebase goes to `civitai.com` or its own image CDN `image.civitai.com` (file downloads redirect to Civitai's CDN). Verify yourself:

```bash
grep -n "https://" __init__.py
```

```python
CIVITAI_MODEL_API = "https://civitai.com/api/v1/models/{model_id}"
CIVITAI_VERSION_API = "https://civitai.com/api/v1/model-versions/{version_id}"
CIVITAI_IMAGE_API = "https://civitai.com/api/v1/images"
```

Per download, exactly three things leave your machine, all to Civitai: a metadata lookup for the model or image you clicked, the file request itself, and — only if you configured one — your API key attached to those requests. That's the same information Civitai gets when you click their own download button while logged in.

**Nothing else, nobody else.** No telemetry, no analytics, no version phone-home, no error reporting service, no accounts. The userscripts run only on Civitai pages (`@match *://civitai.com/*`) and send the model id/URL of the thing you clicked to your own machine (`127.0.0.1`) — they read nothing else off the page and can't contact any other host. Your prompts, workflows, generations, and library never leave your computer through anything in this repo.

## License

MIT. See [`LICENSE`](LICENSE).
