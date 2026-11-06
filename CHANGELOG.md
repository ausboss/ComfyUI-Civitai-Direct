# Changelog

## Unreleased

### Added

- `quickstart.py` (and `quickstart.bat` for Windows) sets everything up: it finds ComfyUI, installs the node, saves your API key to `.env`, checks that ComfyUI loaded the node and opens the userscript page. `--check` checks an existing setup, and `--yes --key-from-env` runs it without questions. The README has a step-by-step section for AI agents setting this up for someone.
- The ready button uses the AusBoss slate-teal colors.
- One userscript now covers everything: the model-page button, the model-card button and the gallery tools. Install `civitai-direct.user.js` and remove the two older scripts, which update to empty ones.
- "Set ComfyUI address" in the Tampermonkey menu, for a ComfyUI on another port, another computer or a remote machine. The address survives script updates.
- The userscript finds ComfyUI on ports 8188, 8000 (the desktop app) and 8189, and only counts a server that answers as this node.
- A failed download says why when you hover the button, and a missing API key shows as "API Key Needed".
- `CIVITAI_BASE_FOLDERS=off` saves models straight into `loras/`, `checkpoints/` and the like, without a base-model subfolder.
- Preview images: `<stem>.preview.<ext>` beside every model. The file type comes from the image's bytes, since Civitai serves PNGs from `.jpeg` addresses. The default picks the tamest image and skips a model that has nothing rated PG or PG13. `CIVITAI_PREVIEWS=any` uses Civitai's cover image, and `off` saves none.
- The buttons check before you click. The model-page button asks the new `GET /civitai/exists` route whether you already have the version and shows "In Your Library", or shows "ComfyUI Offline" when nothing answers. Results that were already in your library are labelled as such on all buttons.
- Folder routing for CLIP (`text_encoders/`), CLIPVision, UNet, MotionModule and Detection models.
- A unit test suite: `python -m unittest discover -s tests -t .`.
- `/civitai/health` reports the active `sidecars`, `previews` and `base_folders` settings.

### Changed

- The code moved from one 2,000-line `__init__.py` into the `civitai_direct/` package. `__init__.py` only registers the routes. Behaviour is the same except for the fixes below.
- Models now get the `<stem>.civitai.info` file that SD-WebUI Civitai Helper, Lora Manager and ComfyUI-Image-Saver read, in place of the `.json` and `.md` pair. `CIVITAI_SIDECARS=full` writes the pair as well, and workflows always keep it. Duplicate detection reads both kinds, including `.civitai.info` files written by other tools, and adds the new file when you ask for a model you downloaded earlier.
- Base-model folders are lowercase names without spaces or dots (`sd15`, `sdxl`, `pony`, `flux1`, `krea2`, `wan-video-22-t2v-a14b`). A base model the node has not seen takes its name from the version's AIR identifier. Folders from earlier releases are still searched, so nothing downloads twice.
- The model-page button no longer reads type, base model, trigger words or hashes from Civitai's page markup, which the server never used. It sends the model and version ids, and takes the type and base model for its label from Civitai's API.
- Requests identify as `ComfyUI-Civitai-Direct` instead of a Windows browser.
- The undocumented `/civitai/download-by-id` alias is gone. Use `/civitai/download`, which takes the same payloads.
- The contributor guide, `AGENTS.md`, no longer contains machine-specific paths.

### Fixed

- An image address sent by the browser to `/civitai/download-image` was fetched with your API key attached. Only `civitai.com` addresses are fetched now.
- A folder from `extra_model_paths.yaml` marked `is_default: true` was ignored unless it was named exactly like ComfyUI's own (`models/Lora` lost to `models/loras`).
- Duplicate checks only looked in the folder new files go to. They now cover every folder ComfyUI has registered for the type, including checkpoints kept in `diffusion_models/`.
- File names containing `[` and `]` were never recognized as already downloaded.
- SD 1.4, SD 2.1 and SD 3.x checkpoints were saved to `diffusion_models/`, where Load Checkpoint cannot see them.
- Every `GET /civitai/exists` call read every sidecar in the library again. Parsed sidecars are cached until the file changes, folder listings are reused for a few seconds, and unrelated `.json` files are no longer opened.
- A JSON body that was not an object returned a 500 error.
- Finished jobs were kept in memory forever. The most recent 200 are kept.
- Workflow ZIPs are limited in size and entry count.
- Windows device names such as `CON` are no longer used as file names.
- Downloads are checked against Civitai's SHA256. A file that does not match is kept as `.corrupt` and reported as an error.
- Errors for models that need a login now say so and say how to add an API key. Early Access errors name the date the version opens.

## 0.1.0

First release.

- Adds the `/civitai/*` routes to ComfyUI's own server: `health`, `download`, `download-image`, `status` and `jobs`.
- Sorts models by type and base model into the folders ComfyUI reads, taken from `folder_paths` so `extra_model_paths.yaml` is honored.
- Downloads Civitai workflows into ComfyUI's workflow library, with safe ZIP extraction, nested ZIP handling and duplicate detection.
- Downloads gallery images (`/civitai/download-image`) as the original upload with a `.json` record, the prompt as `.txt` and the ComfyUI graph as `.workflow.json`. The image location is `CIVITAI_IMAGE_DIR`, and the default is the Pictures folder on Windows and Linux, including moved and OneDrive folders.
- Writes `.json` and `.md` records for each model (schema `ausboss-civitai-model-sidecar-v1`, shared with easy-civitai-downloader) with SHA256, trigger words, ratings, license, file details and Civitai's hashes.
- Skips files you already have. A version is recognized by its record, and a same-named file is compared by SHA256 and adopted when it matches. A different file with the same name is saved as `name (1)`.
- Reads the API key from `CIVITAI_API_KEY`, a `.env` file or `civitai_api_key.txt`. File keys are read on every download.
- Explains Login and Early Access refusals separately. The models API reports Early Access versions as public, so the node asks the download endpoint and reports the date the version opens.
- Adds the model-page and model-card userscripts, and the gallery-image userscript, with live progress on the buttons and a yellow Early Access state.
- Keeps console output readable for every model name by escaping non-ASCII text, so a Windows console cannot crash on it.
