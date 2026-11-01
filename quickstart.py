#!/usr/bin/env python3
"""Civitai Direct quick start.

Finds your ComfyUI, puts this node in its custom_nodes folder, saves your
Civitai API key, checks that ComfyUI has loaded the node, and opens the
userscript install page.

    python quickstart.py                       # asks a few questions
    python quickstart.py --comfyui D:\\ComfyUI  # say where ComfyUI is
    python quickstart.py --check               # only check an existing setup
    python quickstart.py --help                # every option

Standard library only, Python 3.8 or later. It never runs on its own: ComfyUI
and ComfyUI Manager do not execute it, and it writes only inside your ComfyUI
folder (the node folder and its .env file).
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

REPO_URL = "https://github.com/ausboss/ComfyUI-Civitai-Direct"
USERSCRIPT_URL = REPO_URL + "/raw/refs/heads/main/userscripts/civitai-direct.user.js"
KEY_PAGE = "https://civitai.com/user/account"
NODE_FOLDER = "ComfyUI-Civitai-Direct"
PORTS = [8188, 8000, 8189]  # ComfyUI default, the desktop app, a common second install
HERE = Path(__file__).resolve().parent
COPY_SKIP = {".git", "__pycache__", ".env", "civitai_api_key.txt", "_scratch", ".claude", "docs"}

TEAL = "\033[38;2;0;180;170m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _color_ok() -> bool:
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # turns on ANSI colors in a Windows console
    return True


COLOR = _color_ok()


def paint(text: str, style: str) -> str:
    return f"{style}{text}{RESET}" if COLOR else text


def step(text: str) -> None:
    print(f"\n{paint('▸', TEAL)} {paint(text, BOLD)}")


def ok(text: str) -> None:
    print(f"  {paint('✓', TEAL)} {text}")


def warn(text: str) -> None:
    print(f"  {paint('!', BOLD)} {text}")


def note(text: str) -> None:
    print(f"  {paint(text, DIM)}")


def safe_print_setup() -> None:
    """A cp1252 Windows console cannot print every path; never crash on one."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


# ----------------------------------------------------------------------
# Finding ComfyUI
# ----------------------------------------------------------------------

def is_comfy_root(path: Path) -> bool:
    """A ComfyUI folder: custom_nodes plus main.py (a git or portable install)
    or models/ (the desktop app's base folder)."""
    try:
        return (path / "custom_nodes").is_dir() and (
            (path / "main.py").is_file() or (path / "models").is_dir()
        )
    except OSError:
        return False


def normalize_root(path: Path):
    """Accept the folder people point at: ComfyUI itself, or the portable /
    Easy-Install folder that holds it."""
    path = path.expanduser()
    if path.name == "custom_nodes":
        path = path.parent
    for candidate in (path, path / "ComfyUI"):
        if is_comfy_root(candidate):
            return candidate.resolve()
    return None


def desktop_app_base_paths():
    """The ComfyUI desktop app keeps its folder in config.json as basePath."""
    configs = []
    if os.name == "nt":
        if os.getenv("APPDATA"):
            configs.append(Path(os.environ["APPDATA"]) / "ComfyUI" / "config.json")
    elif sys.platform == "darwin":
        configs.append(Path.home() / "Library" / "Application Support" / "ComfyUI" / "config.json")
    else:
        configs.append(Path.home() / ".config" / "ComfyUI" / "config.json")
    for config in configs:
        try:
            base = json.loads(config.read_text(encoding="utf-8")).get("basePath")
        except (OSError, ValueError, AttributeError):
            continue
        if base:
            yield Path(base)


def find_comfy_roots():
    """Every ComfyUI this script can see, most likely first."""
    if HERE.parent.name == "custom_nodes" and is_comfy_root(HERE.parent.parent):
        return [HERE.parent.parent.resolve()]  # already installed: no question to ask

    home = Path.home()
    guesses = [Path.cwd(), *Path.cwd().parents, HERE.parent, *desktop_app_base_paths()]
    for base in (home, home / "Documents", home / "Desktop", home / "code"):
        guesses += [
            base / "ComfyUI",
            base / "ComfyUI_windows_portable",
            base / "ComfyUI-Easy-Install",
            base / "comfyui" / "ComfyUI-Easy-Install",
        ]
    if os.name == "nt":
        for drive in "CDEFG":
            guesses += [Path(f"{drive}:/ComfyUI"), Path(f"{drive}:/ComfyUI_windows_portable"),
                        Path(f"{drive}:/ComfyUI-Easy-Install")]

    roots = []
    for guess in guesses:
        root = normalize_root(guess)
        if root and root not in roots:
            roots.append(root)
    return roots


def choose_root(args):
    if args.comfyui:
        root = normalize_root(Path(args.comfyui))
        if not root:
            sys.exit(f"No ComfyUI found at {args.comfyui} (looked for custom_nodes next to main.py or models/).")
        return root

    roots = find_comfy_roots()
    if len(roots) == 1:
        return roots[0]
    if not roots:
        if args.yes:
            sys.exit("Could not find ComfyUI. Run again with --comfyui <path to your ComfyUI folder>.")
        typed = input("  Where is your ComfyUI folder? ").strip().strip('"')
        root = normalize_root(Path(typed)) if typed else None
        if not root:
            sys.exit(f"No ComfyUI found at {typed!r}.")
        return root
    if args.yes:
        listing = "\n".join(f"  {r}" for r in roots)
        sys.exit(f"Found more than one ComfyUI:\n{listing}\nRun again with --comfyui <one of these>.")
    for number, root in enumerate(roots, 1):
        print(f"  {number}. {root}")
    picked = input(f"  Which one? [1-{len(roots)}, default 1] ").strip() or "1"
    try:
        return roots[int(picked) - 1]
    except (ValueError, IndexError):
        sys.exit("Not one of the listed numbers.")


# ----------------------------------------------------------------------
# Installing the node
# ----------------------------------------------------------------------

def find_installed(custom_nodes: Path):
    """An existing copy of this node in custom_nodes, under any folder name."""
    for child in sorted(custom_nodes.iterdir()):
        try:
            if child.name.lower() == NODE_FOLDER.lower() and child.is_dir():
                return child
            pyproject = child / "pyproject.toml"
            if pyproject.is_file() and 'name = "comfyui-civitai-direct"' in pyproject.read_text(encoding="utf-8"):
                return child
        except OSError:
            continue
    return None


def link_folder(target: Path, link: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        link.symlink_to(target, target_is_directory=True)


def install_node(root: Path, args) -> Path:
    custom_nodes = root / "custom_nodes"
    existing = find_installed(custom_nodes)
    if existing:
        ok(f"Already installed: {existing}")
        if (existing / ".git").exists() and shutil.which("git") and not args.no_update:
            result = subprocess.run(["git", "-C", str(existing), "pull", "--ff-only", "-q"],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                ok("Updated to the latest version.")
            else:
                warn("git pull did not run cleanly; left the folder as it is.")
        return existing

    dest = custom_nodes / NODE_FOLDER
    if args.link:
        link_folder(HERE, dest)
        ok(f"Linked {dest} -> {HERE}")
    elif shutil.which("git"):
        subprocess.run(["git", "clone", "-q", REPO_URL, str(dest)], check=True)
        ok(f"Cloned into {dest}")
    else:
        shutil.copytree(HERE, dest, ignore=lambda _d, names: [n for n in names if n in COPY_SKIP])
        ok(f"Copied into {dest}")
        note("git is not installed, so this copy will not update itself. ComfyUI Manager can reinstall it later.")
    return dest


# ----------------------------------------------------------------------
# Settings (.env in the node folder, read by the node on every download)
# ----------------------------------------------------------------------

def read_env(path: Path) -> dict:
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key.replace("export ", "").strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def write_env(path: Path, updates: dict) -> None:
    """Change or add NAME=value lines, keeping every other line as it was."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    for index, line in enumerate(lines):
        key = line.strip().partition("=")[0].replace("export ", "").strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    lines += [f"{key}={value}" for key, value in remaining.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)


def key_source(node_dir: Path):
    if os.getenv("CIVITAI_API_KEY", "").strip():
        return "the CIVITAI_API_KEY environment variable"
    if read_env(node_dir / ".env").get("CIVITAI_API_KEY"):
        return ".env"
    key_file = node_dir / "civitai_api_key.txt"
    if key_file.is_file() and key_file.read_text(encoding="utf-8", errors="ignore").strip():
        return "civitai_api_key.txt"
    return None


def configure(node_dir: Path, args) -> None:
    updates = {}
    source = key_source(node_dir)
    if args.key_from_env:
        key = os.getenv("CIVITAI_API_KEY", "").strip()
        if not key:
            sys.exit("--key-from-env was given but CIVITAI_API_KEY is empty.")
        updates["CIVITAI_API_KEY"] = key
    elif source:
        ok(f"API key found in {source}.")
    elif args.yes:
        note(f"No API key. Many models need one: create it at {KEY_PAGE} (API Keys), then run this again.")
    else:
        print(f"  Many Civitai models need a login to download. Create a key at {KEY_PAGE} under API Keys.")
        try:
            key = getpass.getpass("  Paste your key (hidden), or press Enter to skip: ").strip()
        except EOFError:
            key = ""
        if key:
            updates["CIVITAI_API_KEY"] = key
        else:
            note("Skipped. You can run this again any time to add it.")

    if args.image_dir:
        updates["CIVITAI_IMAGE_DIR"] = str(Path(args.image_dir).expanduser().resolve())
    if args.workflow_dir:
        updates["CIVITAI_WORKFLOW_DIR"] = str(Path(args.workflow_dir).expanduser().resolve())

    if updates:
        write_env(node_dir / ".env", updates)
        saved = ", ".join(sorted(updates))
        ok(f"Saved {saved} to {node_dir / '.env'} (ignored by git; no restart needed).")


# ----------------------------------------------------------------------
# Checking the running ComfyUI
# ----------------------------------------------------------------------

_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never send localhost through a proxy


def get_json(url: str):
    try:
        with _LOCAL.open(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 -- any failure just means "not here"
        return None


def check_server(args) -> bool:
    ports = ([args.port] if args.port else []) + [p for p in PORTS if p != args.port]
    seen_comfy = []
    for port in ports:
        base = f"http://127.0.0.1:{port}"
        health = get_json(base + "/civitai/health")
        if isinstance(health, dict) and health.get("node") == "ComfyUI-Civitai-Direct":
            ok(f"ComfyUI on port {port} has the node loaded (version {health.get('version')}).")
            if health.get("has_civitai_api_key"):
                ok(f"It sees your API key (from {health.get('api_key_source')}).")
            else:
                note("It sees no API key. Public models still work.")
            dirs = health.get("dirs") or {}
            if dirs.get("loras"):
                note(f"LoRAs go to       {dirs['loras']}")
            if dirs.get("checkpoints"):
                note(f"Checkpoints go to {dirs['checkpoints']}")
            if port not in PORTS:
                note(f"Port {port} is not one the userscript tries. In the Tampermonkey menu on Civitai, "
                     f"choose Set ComfyUI address and enter {base}")
            return True
        if get_json(base + "/system_stats") is not None:
            seen_comfy.append(port)
    if seen_comfy:
        warn(f"ComfyUI is running on port {seen_comfy[0]} but has not loaded the node yet. Restart ComfyUI.")
    else:
        warn("ComfyUI is not running (checked ports " + ", ".join(map(str, ports)) + "). "
             "Start it, then run: python quickstart.py --check")
    return False


def main() -> int:
    safe_print_setup()
    parser = argparse.ArgumentParser(description="Set up Civitai Direct for ComfyUI.")
    parser.add_argument("--comfyui", metavar="PATH", help="your ComfyUI folder (found automatically if left out)")
    parser.add_argument("--yes", "-y", action="store_true", help="ask nothing; for scripts and agents")
    parser.add_argument("--check", action="store_true", help="only check that ComfyUI has the node loaded")
    parser.add_argument("--key-from-env", action="store_true",
                        help="save the key from the CIVITAI_API_KEY environment variable into .env")
    parser.add_argument("--image-dir", metavar="PATH", help="where gallery images go (default: Pictures/Civitai)")
    parser.add_argument("--workflow-dir", metavar="PATH", help="where workflows go (default: ComfyUI's workflow library)")
    parser.add_argument("--port", type=int, help="ComfyUI's port, if it is not 8188, 8000 or 8189")
    parser.add_argument("--link", action="store_true", help="link this folder into custom_nodes instead of cloning (for development)")
    parser.add_argument("--no-update", action="store_true", help="do not git pull an existing install")
    parser.add_argument("--no-browser", action="store_true", help="do not open the userscript page")
    args = parser.parse_args()

    print(paint("Civitai Direct", TEAL + BOLD) + paint("  ·  quick start", DIM))

    if args.check:
        step("Checking ComfyUI")
        return 0 if check_server(args) else 1

    step("Finding ComfyUI")
    root = choose_root(args)
    ok(f"ComfyUI: {root}")

    step("Installing the node")
    node_dir = install_node(root, args)

    step("API key and folders")
    configure(node_dir, args)

    step("Checking ComfyUI")
    running = check_server(args)

    step("The userscript (the button on civitai.com)")
    print("  1. Install Tampermonkey: https://www.tampermonkey.net/")
    print("     Chrome: turn on Developer mode at chrome://extensions, or Allow User Scripts (Chrome 138+).")
    print(f"  2. Open this and click Install: {USERSCRIPT_URL}")
    note("Remove civitai-direct-grid and civitai-direct-images if you have them; the main script does both now.")
    if not args.no_browser and not args.yes:
        webbrowser.open(USERSCRIPT_URL)

    print()
    if running:
        print(paint("Ready.", TEAL + BOLD) + " Open a model page on civitai.com and click ⚡ Direct to Server.")
    else:
        print(paint("Almost.", TEAL + BOLD) + " Start or restart ComfyUI, then run: python quickstart.py --check")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")
        sys.exit(130)
    except subprocess.CalledProcessError as error:
        sys.exit(f"A command failed: {' '.join(map(str, error.cmd))}")
