# Changelog

All notable changes to ComfyUI-Civitai-Direct are documented here.

## 0.1.0

- Added the `/civitai/*` routes on ComfyUI's own server: `health`, `download`, `download-image`, `status`, `jobs`.
- Added gallery-image downloads (`/civitai/download-image` + the images userscript, ported from easy-civitai-downloader): fetches the original upload (PNG originals keep embedded generation/ComfyUI metadata), writes a `.json` sidecar (schema `ausboss-civitai-image-sidecar-v1`), the prompt as `.txt`, and the ComfyUI graph as `.workflow.json` when one exists; already-downloaded image ids are skipped as `already_exists`.
- Added `CIVITAI_IMAGE_DIR` and `CIVITAI_WORKFLOW_DIR` settings (environment or `.env`, re-read per download). Defaults: the OS Pictures folder (`Civitai/` subfolder; Windows known-folder and Linux XDG aware) for images, ComfyUI's user workflow library for workflows. `/civitai/health` reports the resolved `images_dir` and `workflows_dir`.
- Added type routing via `folder_paths` — SD-family checkpoints to `checkpoints/`, Flux/Wan/LTX to `diffusion_models/`, LyCORIS/DoRA to `loras/`, embeddings, VAE, ControlNet, upscalers, text encoders — each under a normalized `<base model>/` subfolder.
- Added `.json` + `.md` metadata sidecars (schema `ausboss-civitai-model-sidecar-v1`, shared with easy-civitai-downloader) with SHA256, trigger words, and token redaction.
- Added classification metadata to both sidecars so other tools can sort, filter, and identify files without re-querying Civitai: decoded NSFW browsing levels (per-version and per-model), the `poi` / `minor` / `sfwOnly` flags, availability and usage control, file format / precision / pruned-or-full, pickle and virus scan results, licensing permissions, per-version stats, and Civitai's full hash set (AutoV1/V2/V3, CRC32, BLAKE3). The `.md` note gained a matching `## Classification` section.
- Fixed the AIR identifier always being empty: the model endpoint's embedded version objects omit it (along with `usageControl`, `uploadType` and `updatedAt`), so the version endpoint is now merged in as well.
- Added two-layer duplicate detection: an already-downloaded version is recognized by its sidecar, and a same-named file from any other source is SHA256-verified against Civitai's hash and adopted (sidecars backfilled) instead of re-downloaded — a ` (1)` suffix is used only for true name collisions with different content.
- Added Wan-family slug-aware filenames.
- Added user-aware Civitai workflow downloads under `user/<user>/workflows/civitai-direct/<base model>/`, including safe ZIP extraction, nested ZIP handling, picker-safe metadata sidecars, and duplicate detection.
- Added the model-page and grid userscripts with ComfyUI port discovery (8188/8189) and live job polling — download progress and failures now show on the button.
- Added API key support via `CIVITAI_API_KEY`, a `.env` file, or `civitai_api_key.txt` — file keys are re-read per download (no restart), `/civitai/health` reports whether and where a key loaded, and downloads send the key both as `Authorization: Bearer` and `?token=`.
- Added distinct error messages for login-required vs Early Access versions (an API key does not unlock Early Access), with token redaction on every stored or logged error.
- Added Early Access warnings to the userscripts: grid cards and model pages show a yellow ⚡ Early Access button (detected from Civitai's own card badge / version notice), and the grid button requires a deliberate double click so EA models aren't queued by accident.
- Fixed Early Access detection server-side: the models API misreports EA versions as `Public` and the download endpoint answers browser-like requests with an HTML redirect, so refusals are now re-probed as JSON — errors name Early Access explicitly and include the date the version unlocks for everyone.
- Duplicate detection now follows files you reorganize: a model moved into a subfolder (or another base-model folder of the same type) is recognized via its moved sidecar or by name + SHA256, instead of being downloaded again.
- Reworked console output into a readable per-job flow — resolved name/version/type/base, size and destination, then the result — with all names and paths escaped to ASCII so a cp1252 Windows console can never crash on them.
- Added the "Is this safe?" / "Does this send my data anywhere?" README sections.
