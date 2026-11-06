"""Civitai Direct: one-click Civitai downloads straight into the right ComfyUI
model folders.

This pack adds web routes to ComfyUI's own server and defines no graph nodes.
Pair it with the userscript in userscripts/ to get a download button on
civitai.com. The code lives in civitai_direct/; settings are documented in the
README.
"""

from .civitai_direct import routes  # noqa: F401 -- importing registers the /civitai/* routes

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}
