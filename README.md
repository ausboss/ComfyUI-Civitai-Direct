<div align="center">
  <h1>ComfyUI-Civitai-Direct</h1>
  <p><strong>Browse Civitai like normal. Click ⚡. Models land in the right ComfyUI folder.</strong></p>
  <p>
    <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="MIT License">
    <img src="https://img.shields.io/badge/release-0.1.0-00b4aa?style=flat-square" alt="Release 0.1.0">
  </p>
  <p>Part of the <a href="https://github.com/ausboss/ComfyUI-AusBoss">AusBoss</a> node family.</p>
</div>

A ComfyUI custom node + a Tampermonkey userscript. No separate server, no Python setup, no config wizard — destination folders come straight from ComfyUI's own `folder_paths` (your `extra_model_paths.yaml` is honored automatically). Models are sorted by type and base model (`loras/SDXL/`, `checkpoints/SD 1.5/`, `diffusion_models/Flux.1 Dev/`, ...) with `.json` + `.md` metadata sidecars, duplicate detection, and live progress on the button.

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

That's it. With ComfyUI running, open any Civitai model page and the **⚡ Direct to Server** button appears next to the native download button.

## API key (for gated / early-access models)

Public models download without any key. For gated ones, create a key at [civitai.com/user/account](https://civitai.com/user/account) → **API Keys**, then either:

- set the `CIVITAI_API_KEY` environment variable before starting ComfyUI, **or**
- put the key in a `civitai_api_key.txt` file inside this node's folder

## Where files go

| Civitai type | Destination |
|---|---|
| Checkpoint (SD 1.5 / SDXL / Pony / Illustrious ...) | `checkpoints/<base model>/` |
| Checkpoint (Flux, Wan, LTX, other diffusion models) | `diffusion_models/<base model>/` |
| LoRA / LyCORIS / DoRA | `loras/<base model>/` |
| Embedding | `embeddings/<base model>/` |
| VAE / ControlNet / Upscaler / Text Encoder / Hypernetwork | matching folder, `<base model>/` subfolder |

Every model gets a `<file>.json` sidecar (full Civitai metadata) and a `<file>.md` notes file (trigger words, description, source links). Re-downloading a version you already have is detected and skipped. New files show up in ComfyUI's loaders immediately — no restart.

If you run ComfyUI on a port other than 8188/8189, add it to `SERVER_PORTS` at the top of the userscript.

## Troubleshooting

- **Button doesn't appear** — is ComfyUI running? Open `http://127.0.0.1:8188/civitai/health` (your port) — you should see JSON. Check Tampermonkey is enabled for civitai.com.
- **"gated or early-access"** error — add an API key (see above).
- **Wrong folder?** — open an issue with the model link; type/base-model mappings live in `__init__.py` and are easy to extend.

## Image & workflow tools

Prompt/image downloading, ComfyUI-workflow extraction from gallery images, and workflow ZIP handling live in the sibling project [easy-civitai-downloader](https://github.com/ausboss/easy-civitai-downloader) (standalone server). This node stays focused on one thing: models into the right folders.

## Development

See [`AGENTS.md`](AGENTS.md) for working rules, compatibility contracts (sidecar schema, userscript payloads), and the validation checklist.

## License

MIT. See [`LICENSE`](LICENSE).
