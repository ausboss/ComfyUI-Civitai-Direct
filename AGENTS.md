# ComfyUI-Civitai-Direct contributor guide

A ComfyUI custom node and a Tampermonkey userscript. The userscript adds a
download button to civitai.com, and the node saves the file into the right
ComfyUI model folder. This guide covers how the code is laid out, what must
keep working, and how to check a change.

## Rules

- Do not modify `LICENSE`.
- Do not change `version` in `pyproject.toml`. The maintainer sets it at
  release time.
- Never commit an API key. `.env` and `civitai_api_key.txt` are gitignored and
  must stay that way. Redact `?token=` from anything that is logged, stored in
  a job record, or shown in the browser.
- Keep diffs small: touch only what the change needs.
- Code, images and text must be your own or under a license that allows MIT
  redistribution, with the source credited.
- No dependencies beyond what ComfyUI already ships (aiohttp). No pip installs,
  no compiled files, no install scripts.

## Layout

```text
__init__.py                 Entry point. Registers the routes and defines no graph nodes,
                            so NODE_CLASS_MAPPINGS is empty.
civitai_direct/
  routes.py                 The /civitai/* handlers on ComfyUI's own web server.
  downloads.py              One model or workflow download, as a sequence of steps.
  images.py                 Gallery image downloads.
  civitai_api.py            URL parsing, API calls, Civitai host checks, gated-download errors.
  classify.py               Model type, base-model slug and destination folder. Pure functions.
  paths.py                  Destination folders, from ComfyUI's folder_paths.
  library.py                Finding what is already on disk (sidecars, same-name files, SHA256).
  sidecars.py               .civitai.info, preview images, the .json and .md records.
  workflows.py              Workflow destinations and safe ZIP extraction.
  jobs.py                   The job registry and destination claims.
  settings.py               Environment and .env settings, the API key.
  text.py                   File-safe names, HTML stripping, log-safe output.
userscripts/
  civitai-direct.user.js    The userscript: model-page button, model-card button, gallery tools.
  civitai-direct-grid.user.js, civitai-direct-images.user.js
                            Empty stubs. Their features moved into civitai-direct.user.js, and
                            the stubs exist so installed copies update instead of duplicating buttons.
tests/                      Standard-library unittest suite.
```

Only `paths.py` and `routes.py` import ComfyUI directly. `images.py` and
`downloads.py` import `paths`, so their tests install stand-ins for
`folder_paths` and `server` (see `tests/stubs.py`). The other modules need
nothing from ComfyUI.

Model destination folders always come from ComfyUI's `folder_paths`, which
already honors `extra_model_paths.yaml`. Do not add a separate path
configuration. The only path settings are `CIVITAI_IMAGE_DIR` and
`CIVITAI_WORKFLOW_DIR`, because images and workflows have no `folder_paths`
entry.

## Compatibility contracts

Other tools and installed userscripts depend on these. Do not break them.

- `<stem>.civitai.info` is the Civitai model-version API response, verbatim. SD-WebUI Civitai Helper,
  ComfyUI Lora Manager and ComfyUI-Image-Saver read it. Do not rename, reshape or add keys. The
  one exception is filling in a top-level `modelId` or `model` when the API left them out. The file
  is always named `<model stem>.civitai.info`, and previews are `<model stem>.preview.<ext>`.
- The sidecar schema `ausboss-civitai-model-sidecar-v1` (the `<file>.json` and `.md` pair) and the
  image schema `ausboss-civitai-image-sidecar-v1` are shared with the sibling project
  [easy-civitai-downloader](https://github.com/ausboss/easy-civitai-downloader). Changes must be
  additive. A rename needs a new schema id. Duplicate detection must keep reading both the
  `.civitai.info` and the v1 JSON dialects.
- `POST /civitai/download` accepts both `{url, metadata}` (the page button) and
  `{model_id, model_version_id}` (the card button). Installed userscripts update slowly, so old
  payloads keep arriving long after a release. `POST /civitai/download-image` takes
  `{image_id, image_url, page_url, prompt, filename, generation_data}`.
- Job states are `resolving`, `hashing_existing`, `downloading`, `hashing`, `extracting`,
  `writing_metadata`, `done`, `already_exists` and `error`. The userscripts treat anything that is
  not terminal as in progress.
- The userscript reads `GET /civitai/health` and needs its `node` field to be
  `ComfyUI-Civitai-Direct`.

## Conventions

- Console output must survive a cp1252 Windows console. Civitai model names often contain CJK or
  emoji, so pass every name and path through `console_safe()`, or `log_safe()` when a URL may be
  involved.
- Downloads stream in 1 MiB chunks to a `.part` file that is renamed only on success. Blocking work
  such as hashing goes through `asyncio.to_thread`.
- A background failure must end up in the job record, because that is what the browser shows. Do
  not let an exception end inside a bare task.
- Anything from the browser is untrusted. A URL the node fetches must pass `is_civitai_url()`, so the
  API key can only go to Civitai. Path parts built from request data go through `safe_filename()` or
  `safe_folder()`.
- The userscript's page selectors break when Civitai changes its markup. Prefer Civitai's API for
  data, and keep selectors to what only the page can tell us: which version is selected, where to
  place the button, whether a version is in Early Access.
- Bump `@version` in `civitai-direct.user.js` on every change to it, or Tampermonkey will not offer
  the update.

## Checking a change

Run the unit tests with ComfyUI's Python, since they need aiohttp:

```bash
python -m unittest discover -s tests -t .
node --check userscripts/civitai-direct.user.js
```

A change to downloading or folder routing also needs a real run on a scratch ComfyUI, so that
nothing lands in your own model library. Start one with a throwaway `--base-directory`, put a copy
of this folder in its `custom_nodes`, and set `CIVITAI_IMAGE_DIR` to a scratch folder. Then:

```bash
curl http://127.0.0.1:PORT/civitai/health
curl -X POST http://127.0.0.1:PORT/civitai/download -H "Content-Type: application/json" \
     -d '{"model_id":16014,"model_version_id":28907}'
curl "http://127.0.0.1:PORT/civitai/status?id=1"
```

Model 16014 is a small SD 1.5 LoRA that downloads without an API key. A change is checked when:

- the file lands in `<type>/<base-model-slug>/` (for example `loras/sd15/`), with a `.civitai.info`
  sidecar and a `.preview.<ext>` image;
- asking for the same version again returns `already_exists`;
- a bad request gives a readable `error` in the job record and no stack trace in the console;
- with `CIVITAI_SIDECARS=full`, the `.json` and `.md` pair is written as well.

Many recent models need a login to download. A test that needs one uses an API key from the
environment of the scratch server, never a key written into a file in the repository.
