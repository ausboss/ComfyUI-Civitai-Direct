"""Stand-ins for the two modules ComfyUI provides (`folder_paths`, `server`),
so the tests run with any Python that has aiohttp. Import this first."""

import sys
import types
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeFolderPaths(types.ModuleType):
    """Only what the node reads. `registered` maps a folder name to its paths."""

    def __init__(self):
        super().__init__("folder_paths")
        self.models_dir = "/comfy/models"
        self.registered: dict[str, list[str]] = {}

    def get_folder_paths(self, name):
        return list(self.registered[name])  # KeyError for unknown names, like ComfyUI


folder_paths = sys.modules.get("folder_paths")
if not isinstance(folder_paths, FakeFolderPaths):
    folder_paths = FakeFolderPaths()
    sys.modules["folder_paths"] = folder_paths


class _Prompt:
    routes = web.RouteTableDef()
    user_manager = None


_server = types.ModuleType("server")
_server.PromptServer = types.SimpleNamespace(instance=_Prompt)
sys.modules.setdefault("server", _server)
