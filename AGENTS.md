# ComfyUI-Civitai-Direct — Agent Working Rules

Instructions for any coding agent working in this repo. `CLAUDE.md` imports
this file, so keep everything here and leave that one as a pointer.

A ComfyUI custom node by ausboss: one-click Civitai downloads routed into the
right ComfyUI model folders, paired with Tampermonkey userscripts that add the
download button to civitai.com. Part of the AusBoss family
(see ComfyUI-AusBoss for the node pack); this repo stays focused on model
downloading — image/prompt tools live in easy-civitai-downloader.

## Hard rules

- Never modify `LICENSE`.
- Never bump `version` in `pyproject.toml` unless explicitly asked.
- Never add agent attribution to commits or PRs — no `Co-Authored-By`
  trailers, no "generated with" footers. Commits are authored by ausboss alone.
- Never commit or log a Civitai API key. `civitai_api_key.txt` is gitignored;
  keep it that way. Tokened URLs must be redacted before they touch disk.
- Do not read or analyze `__pycache__`, `.git`, or editor config directories.
- Ask before whole-repo sweeps or large refactors; propose a short plan first.
- Keep diffs minimal: touch only the lines the task needs.
- Be concise; skip pleasantries.

## Third-party independence

- Never copy third-party code, assets, fonts, icons, CSS, or documentation.
- The sibling project easy-civitai-downloader (same author) is the one
  exception: porting logic from it is expected and encouraged.

## Architecture

```text
__init__.py       # the whole node: aiohttp routes on ComfyUI's own server.
                  # Defines NO graph nodes — NODE_CLASS_MAPPINGS is empty.
userscripts/
  civitai-direct.user.js       # model-page download button
  civitai-direct-grid.user.js  # per-card button on listing pages
```

Routes are namespaced under `/civitai/*`. Destination folders always come
from ComfyUI's `folder_paths` (which already honors extra_model_paths.yaml);
never invent a parallel path-config mechanism — killing that config was the
point of this design.

## Compatibility contracts (never break these)

- The sidecar schema `ausboss-civitai-model-sidecar-v1`: key names and
  structure are shared with easy-civitai-downloader's backfill/scrub tools
  and duplicate detection. Additive changes only; renames need a new schema id.
- `/civitai/download` accepts BOTH payload shapes: the page userscript's
  `{url, metadata}` and the grid userscript's `{model_id, model_version_id}`.
  Installed userscripts update lazily via @updateURL — old payloads keep
  arriving long after a release.
- Job states (`resolving`, `hashing_existing`, `downloading`, `hashing`,
  `writing_metadata`, `done`, `already_exists`, `error`) are read by the
  userscripts' pollers, which treat anything not terminal as in-progress.

## Conventions

- Console output must be crash-proof on a cp1252 Windows console: Civitai
  model names routinely contain CJK/emoji, so never print or log them raw —
  log ids and `ascii()`-wrapped paths only. (A raw print of a model name is
  exactly the bug that silently killed downloads in the predecessor server.)
- Downloads stream in 1 MiB chunks with an `await` per chunk and write to a
  `.part` file renamed only on success. Long blocking work (hashing) goes
  through `asyncio.to_thread`.
- Background failures must land in the job record — the job dict is the error
  channel the browser sees. Never let an exception die in a bare task.
- No pip dependencies beyond what ComfyUI ships (aiohttp). None.
- Userscripts: DOM-selector logic is battle-tested — don't restructure it
  casually. Bump `@version` on every userscript change or Tampermonkey won't
  offer the update.

## Validation

Test bed: the clean ComfyUI install at
`C:\Users\admin\Documents\comfyui\ComfyUI-Easy-Install`, run on port 8189
(the daily-driver ComfyUI on E: uses the default port — leave it alone).
`custom_nodes\ComfyUI-Civitai-Direct` there is a junction to this repo, so
changes apply on ComfyUI restart.

```bash
# after restarting ComfyUI on 8189:
curl http://127.0.0.1:8189/civitai/health
curl -X POST http://127.0.0.1:8189/civitai/download -H "Content-Type: application/json" -d '{"model_id":16014}'
curl "http://127.0.0.1:8189/civitai/status?id=1"
node --check userscripts/civitai-direct.user.js
node --check userscripts/civitai-direct-grid.user.js
```

A change is validated when: a small download lands in the right
`<type>/<base model>/` folder with `.json` + `.md` sidecars, re-requesting the
same version returns `already_exists`, and a bad request produces a readable
`error` in the job record rather than a stack trace in the console.
