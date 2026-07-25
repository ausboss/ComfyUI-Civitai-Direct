# Changelog

All notable changes to ComfyUI-Civitai-Direct are documented here.

## 0.1.0

- Added the `/civitai/*` routes on ComfyUI's own server: `health`, `download`, `status`, `jobs`.
- Added type routing via `folder_paths` — SD-family checkpoints to `checkpoints/`, Flux/Wan/LTX to `diffusion_models/`, LyCORIS/DoRA to `loras/`, embeddings, VAE, ControlNet, upscalers, text encoders — each under a normalized `<base model>/` subfolder.
- Added `.json` + `.md` metadata sidecars (schema `ausboss-civitai-model-sidecar-v1`, shared with easy-civitai-downloader) with SHA256, trigger words, and token redaction.
- Added duplicate detection: re-requesting an already-downloaded version is skipped and reported as `already_exists`.
- Added Wan-family slug-aware filenames.
- Added the model-page and grid userscripts with ComfyUI port discovery (8188/8189) and live job polling — download progress and failures now show on the button.
- Added API key support via `CIVITAI_API_KEY` or `civitai_api_key.txt` for gated / early-access models.
