# Changelog

All notable changes to ComfyUI-Civitai-Direct are documented here.

## 0.1.0

- Added the `/civitai/*` routes on ComfyUI's own server: `health`, `download`, `status`, `jobs`.
- Added type routing via `folder_paths` — SD-family checkpoints to `checkpoints/`, Flux/Wan/LTX to `diffusion_models/`, LyCORIS/DoRA to `loras/`, embeddings, VAE, ControlNet, upscalers, text encoders — each under a normalized `<base model>/` subfolder.
- Added `.json` + `.md` metadata sidecars (schema `ausboss-civitai-model-sidecar-v1`, shared with easy-civitai-downloader) with SHA256, trigger words, and token redaction.
- Added two-layer duplicate detection: an already-downloaded version is recognized by its sidecar, and a same-named file from any other source is SHA256-verified against Civitai's hash and adopted (sidecars backfilled) instead of re-downloaded — a ` (1)` suffix is used only for true name collisions with different content.
- Added Wan-family slug-aware filenames.
- Added the model-page and grid userscripts with ComfyUI port discovery (8188/8189) and live job polling — download progress and failures now show on the button.
- Added API key support via `CIVITAI_API_KEY`, a `.env` file, or `civitai_api_key.txt` — file keys are re-read per download (no restart), `/civitai/health` reports whether and where a key loaded, and downloads send the key both as `Authorization: Bearer` and `?token=`.
- Added distinct error messages for login-required vs Early Access versions (an API key does not unlock Early Access), with token redaction on every stored or logged error.
- Added the "Is this safe?" / "Does this send my data anywhere?" README sections.
