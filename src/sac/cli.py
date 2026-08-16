"""
SaC CLI

Command-line interface for the SaC SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sac import __version__


def _load_dotenv() -> None:
    """Load .env file, searching multiple locations by priority.

    Order: cwd/.env → ~/.sac/.env → ~/.env
    First file found wins. Uses setdefault so real env vars take precedence.
    """
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".sac" / ".env",
        Path.home() / ".env",
    ]
    for env_file in candidates:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
            return  # stop after first file found


def _prompt_llm_config() -> dict[str, str]:
    """Shared interactive LLM configuration flow.

    Used by both `sac serve` (first run) and `sac setup claude-code`.
    Returns dict with keys: api_key, api_base, model, search_key, data_dir.
    """
    from sac.runtime.prompts.app import AVAILABLE_MODELS, DEFAULT_MODEL

    # Provider selection
    print()
    print("LLM Provider (used by SaC to generate apps locally, not your agent)")
    print()
    print("  1) OpenRouter (default) — one key, all models")
    print("  2) Anthropic — Claude models directly")
    print("  3) OpenAI — GPT models directly")
    print("  4) Custom — any OpenAI-compatible endpoint")
    provider_choice = input("  Choose [1]: ").strip() or "1"

    api_base = ""
    key_label = "OpenRouter"
    if provider_choice == "2":
        key_label = "Anthropic"
    elif provider_choice == "3":
        api_base = "https://api.openai.com/v1/chat/completions"
        key_label = "OpenAI"
    elif provider_choice == "4":
        api_base = input("  Endpoint URL: ").strip()
        if not api_base:
            print("  ✗ Endpoint URL is required for custom provider")
            sys.exit(1)
        key_label = "Custom"

    # API key
    print()
    api_key = os.environ.get("SAC_API_KEY", "")
    if api_key:
        masked = api_key[:8] + "..." + api_key[-4:]
        use_env = input(f"  API key found in env: {masked}. Use it? [Y/n] ").strip().lower()
        if use_env in ("n", "no"):
            api_key = ""

    if not api_key:
        api_key = input(f"  API key ({key_label}): ").strip()
        if not api_key:
            print("  ✗ API key is required to generate apps.")
            sys.exit(1)

    # Search key (optional)
    search_key = os.environ.get("SAC_SEARCH_API_KEY", "")
    if not search_key:
        search_key = input("  Tavily search key (Enter to skip): ").strip()

    # Data dir
    default_dir = str(Path.home() / ".sac")
    data_dir = os.environ.get("SAC_DATA_DIR", "")
    if not data_dir:
        data_dir = input(f"  Data directory [{default_dir}]: ").strip() or default_dir

    # Model selection
    RECOMMENDED = {
        "anthropic": "anthropic/claude-haiku-4.5",
        "openai": "openai/gpt-5.6-luna",
        "google": "google/gemini-3.7-flash",
    }

    if provider_choice == "2":
        provider_models = [m for m in AVAILABLE_MODELS if m.provider == "anthropic"]
        default_model = RECOMMENDED["anthropic"]
    elif provider_choice == "3":
        provider_models = [m for m in AVAILABLE_MODELS if m.provider == "openai"]
        default_model = RECOMMENDED["openai"]
    else:
        provider_models = AVAILABLE_MODELS
        default_model = DEFAULT_MODEL

    print()
    print("Model (for app generation)")
    rec_ids = set(RECOMMENDED.values())
    # Sort: recommended first, then others
    rec_models = [m for m in provider_models if m.id in rec_ids]
    other_models = [m for m in provider_models if m.id not in rec_ids]
    sorted_models = rec_models + other_models

    print()
    print("  Fast & sufficient for most apps:")
    default_idx = 1
    idx = 1
    for m in rec_models:
        print(f"    {idx}) {m.id}")
        if m.id == default_model:
            default_idx = idx
        idx += 1
    if other_models:
        print()
        print("  Other models (may be slower):")
        for m in other_models:
            print(f"    {idx}) {m.id}")
            if m.id == default_model:
                default_idx = idx
            idx += 1
    print()
    print(f"  Edit models in src/sac/runtime/prompts/app.py")
    model_choice = input(f"  Choose [{default_idx}]: ").strip() or str(default_idx)
    try:
        model = sorted_models[int(model_choice) - 1].id
    except (ValueError, IndexError):
        model = default_model

    return {
        "api_key": api_key,
        "api_base": api_base,
        "model": model,
        "search_key": search_key,
        "data_dir": data_dir,
    }


def _save_global_env(config: dict[str, str]) -> None:
    """Persist config to ~/.sac/.env."""
    sac_dir = Path.home() / ".sac"
    sac_dir.mkdir(parents=True, exist_ok=True)
    env_lines = [
        "# SaC SDK configuration",
        f"SAC_API_KEY={config['api_key']}",
        f"SAC_DATA_DIR={config['data_dir']}",
        f"SAC_MODEL={config['model']}",
    ]
    if config.get("api_base"):
        env_lines.append(f"SAC_API_BASE={config['api_base']}")
    if config.get("search_key"):
        env_lines.append(f"SAC_SEARCH_API_KEY={config['search_key']}")
    (sac_dir / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")


def _ensure_config() -> None:
    """Interactively configure SaC on first run, then persist."""
    print()
    print("First time? Let's configure SaC (saved to ~/.sac/.env).")
    config = _prompt_llm_config()
    _save_global_env(config)

    # Set in current process so serve can proceed
    os.environ["SAC_API_KEY"] = config["api_key"]
    os.environ["SAC_DATA_DIR"] = config["data_dir"]
    os.environ["SAC_MODEL"] = config["model"]
    if config.get("api_base"):
        os.environ["SAC_API_BASE"] = config["api_base"]
    if config.get("search_key"):
        os.environ["SAC_SEARCH_API_KEY"] = config["search_key"]

    print()
    print("  ✓ Saved to ~/.sac/.env")
    print()


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="sac", description="Software as Content SDK")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    # sac serve
    serve_parser = subparsers.add_parser("serve", help="Start the SaC HTTP/SSE server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, default=18420, help="Port to listen on (default: 18420)")
    serve_parser.add_argument("--transport", choices=["http", "stdio"], default="http", help="Transport mode")

    # sac generate
    gen_parser = subparsers.add_parser("generate", help="Generate an app from an intent")
    gen_parser.add_argument("intent", help="The user intent / prompt")
    gen_parser.add_argument("--model", default=None, help="Model to use")
    gen_parser.add_argument("--no-search", action="store_true", help="Disable web search")

    # sac setup
    setup_parser = subparsers.add_parser(
        "setup",
        help="Configure SaC for an agent platform",
    )
    setup_sub = setup_parser.add_subparsers(dest="platform")
    cc_parser = setup_sub.add_parser(
        "claude-code",
        help="Set up SaC as an MCP server for Claude Code",
    )
    cc_parser.add_argument(
        "--remove", action="store_true",
        help="Remove the SaC MCP server from Claude Code",
    )
    codex_parser = setup_sub.add_parser(
        "codex",
        help="Install the SaC skill for Codex",
    )
    codex_parser.add_argument(
        "--remove", action="store_true",
        help="Remove the SaC skill from Codex",
    )
    openclaw_parser = setup_sub.add_parser(
        "openclaw",
        help="Install the SaC skill for OpenClaw",
    )
    openclaw_parser.add_argument(
        "--remove", action="store_true",
        help="Remove the SaC skill from OpenClaw",
    )

    # sac publish
    pub_parser = subparsers.add_parser(
        "publish",
        help="Publish content to a running SaC server",
    )
    pub_parser.add_argument(
        "content", nargs="?", default=None,
        help="Markdown content to publish (omit to read from stdin)",
    )
    pub_parser.add_argument(
        "-f", "--file", default=None,
        help="Read content from a file instead of positional arg / stdin",
    )
    pub_parser.add_argument("--intent", default=None, help="Short description of the content")
    pub_parser.add_argument("--conversation-id", default=None, help="Update an existing conversation")
    pub_parser.add_argument(
        "--server", default=None,
        help="SaC server URL (default: http://127.0.0.1:18420)",
    )
    pub_parser.add_argument(
        "--callback-url", default=None,
        help="Callback URL for interactive actions (e.g. codex://resume?thread=last&cwd=server)",
    )
    pub_parser.add_argument(
        "--callback-format", default=None,
        help="Callback format (e.g. codex_exec_resume, openclaw_gateway)",
    )
    pub_parser.add_argument(
        "--callback-auth", default=None,
        help="Callback auth header (e.g. 'Bearer TOKEN')",
    )

    args = parser.parse_args()

    if args.command == "serve":
        if args.transport == "stdio":
            try:
                from sac.server.mcp import run_stdio
            except ImportError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
            run_stdio()
            return
        # Interactive setup if SAC_API_KEY is missing
        if not os.environ.get("SAC_API_KEY"):
            _ensure_config()
        from sac.server.http import run
        run(host=args.host, port=args.port)

    elif args.command == "generate":
        asyncio.run(_generate(args))

    elif args.command == "setup":
        if args.platform == "claude-code":
            _setup_claude_code(args)
        elif args.platform in ("codex", "openclaw"):
            _setup_skill(args.platform, remove=args.remove)
        else:
            setup_parser.print_help()

    elif args.command == "publish":
        _publish(args)

    else:
        parser.print_help()


def _read_openclaw_gateway_config() -> dict:
    """Read gateway port and token from ~/.openclaw/openclaw.json."""
    import json as _json

    config_path = Path.home() / ".openclaw" / "openclaw.json"
    result: dict = {}
    if not config_path.exists():
        print("  ⚠ ~/.openclaw/openclaw.json not found — callback token will need manual setup")
        return result
    try:
        data = _json.loads(config_path.read_text(encoding="utf-8"))
        gw = data.get("gateway", {})
        port = gw.get("port", 18789)
        token = gw.get("auth", {}).get("token", "")
        if port:
            result["gateway_port"] = port
        if token:
            result["gateway_token"] = token
            print(f"  ✓ Gateway token found (port {port})")
        else:
            print("  ⚠ No gateway token in openclaw.json — callback will need manual setup")
    except Exception as exc:
        print(f"  ⚠ Could not read openclaw.json: {exc}")
    return result


def _setup_skill(platform: str, *, remove: bool = False) -> None:
    """Install or remove a SaC SKILL.md for Codex or OpenClaw."""
    from sac._skills import SKILL_TARGETS

    target = SKILL_TARGETS[platform]
    dest = Path(target["dest"]).expanduser()
    label = target["label"]

    if remove:
        skill_dir = dest.parent
        if skill_dir.exists():
            import shutil
            shutil.rmtree(skill_dir)
            print(f"  ✓ Removed SaC skill from {label} ({skill_dir})")
        else:
            print(f"  ✗ No SaC skill found for {label}")
        return

    # Generate skill content — static for most platforms, dynamic for OpenClaw
    content_fn = target.get("content_fn")
    if content_fn is not None:
        kwargs: dict = {}
        if platform == "openclaw":
            kwargs = _read_openclaw_gateway_config()
        content = content_fn(**kwargs)
    else:
        content = target["content"]

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print()
    print(f"  ✓ SaC skill installed for {label}")
    print(f"    {dest}")

    # Ensure LLM is configured (needed for `sac serve`)
    if not os.environ.get("SAC_API_KEY"):
        print()
        print("SaC server needs an LLM to generate apps. Let's configure it.")
        config = _prompt_llm_config()
        _save_global_env(config)
        print()
        print("  ✓ Saved to ~/.sac/.env")

    print()
    print(f"  Start the SaC server:")
    print(f"    sac serve")
    print()


def _get_desktop_config_path() -> Path | None:
    """Return the Claude Desktop config file path for this platform."""
    import platform
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            return Path(appdata) / "Claude" / "claude_desktop_config.json"
    elif system == "Linux":
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    return None


def _setup_claude_code(args: argparse.Namespace) -> None:
    """Set up or remove SaC as an MCP server for Claude Code."""
    import json
    import shutil
    import subprocess

    # ── Remove ──────────────────────────────────────────────────
    if args.remove:
        print("Removing SaC MCP server from Claude Code...")
        removed = False

        # 1. Desktop config
        desktop_config = _get_desktop_config_path()
        if desktop_config and desktop_config.exists():
            try:
                config = json.loads(desktop_config.read_text(encoding="utf-8"))
                if "sac" in config.get("mcpServers", {}):
                    config["mcpServers"].pop("sac")
                    if not config["mcpServers"]:
                        config.pop("mcpServers")
                    desktop_config.write_text(
                        json.dumps(config, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    print(f"  ✓ Removed from Desktop config")
                    removed = True
            except Exception as exc:
                print(f"  ✗ Failed to update Desktop config: {exc}")

        # 2. CLI config (project scope)
        claude_path = shutil.which("claude")
        if claude_path:
            for scope in ("project", "user", "local"):
                result = subprocess.run(
                    ["claude", "mcp", "remove", "-s", scope, "sac"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    print(f"  ✓ Removed from CLI {scope} config")
                    removed = True

        # 3. Remove .claude/launch.json if it's ours
        launch_file = Path.cwd() / ".claude" / "launch.json"
        if launch_file.exists():
            try:
                launch_data = json.loads(launch_file.read_text(encoding="utf-8"))
                configs = launch_data.get("configurations", [])
                if any(c.get("name") == "sac-viewer" for c in configs):
                    remaining = [c for c in configs if c.get("name") != "sac-viewer"]
                    if remaining:
                        launch_data["configurations"] = remaining
                        launch_file.write_text(
                            json.dumps(launch_data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8",
                        )
                    else:
                        launch_file.unlink()
                    print(f"  ✓ Removed preview config (.claude/launch.json)")
                    removed = True
            except Exception as exc:
                print(f"  ⚠ Failed to clean launch.json: {exc}")

        if not removed:
            print("  ✗ No SaC server found in any config")
        else:
            print()
            print("Restart Claude Code to apply changes.")
        return

    # ── Setup ───────────────────────────────────────────────────
    print()
    print("SaC MCP Setup for Claude Code")
    print("─" * 30)
    print()

    # Check prerequisites
    print("Checking prerequisites...")

    sac_path = shutil.which("sac")
    if not sac_path:
        print("  ✗ sac CLI not found on PATH")
        print("    Install with: pip install sac-sdk")
        sys.exit(1)
    print(f"  ✓ sac CLI: {sac_path}")

    llm_config = _prompt_llm_config()
    api_key = llm_config["api_key"]
    api_base = llm_config.get("api_base", "")
    search_key = llm_config.get("search_key", "")
    data_dir = llm_config["data_dir"]
    model = llm_config["model"]

    # Build server entry
    env = {"SAC_API_KEY": api_key, "SAC_DATA_DIR": data_dir, "SAC_MODEL": model}
    if api_base:
        env["SAC_API_BASE"] = api_base
    server_entry = {
        "command": sac_path,
        "args": ["serve", "--transport", "stdio"],
        "env": env,
    }
    if search_key:
        server_entry["env"]["SAC_SEARCH_API_KEY"] = search_key

    print()
    print("Configuring MCP server...")
    installed = False

    # 1. Desktop config
    desktop_config = _get_desktop_config_path()
    if desktop_config:
        try:
            if desktop_config.exists():
                config = json.loads(desktop_config.read_text(encoding="utf-8"))
            else:
                desktop_config.parent.mkdir(parents=True, exist_ok=True)
                config = {}
            config.setdefault("mcpServers", {})["sac"] = server_entry
            desktop_config.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"  ✓ Desktop config")
            installed = True
        except Exception as exc:
            print(f"  ✗ Desktop config failed: {exc}")

    # 2. CLI config (project scope)
    claude_path = shutil.which("claude")
    if claude_path:
        env_args = ["-e", f"SAC_API_KEY={api_key}"]
        if api_base:
            env_args += ["-e", f"SAC_API_BASE={api_base}"]
        if search_key:
            env_args += ["-e", f"SAC_SEARCH_API_KEY={search_key}"]
        env_args += ["-e", f"SAC_DATA_DIR={data_dir}"]
        env_args += ["-e", f"SAC_MODEL={model}"]

        result = subprocess.run(
            ["claude", "mcp", "add", "-s", "project"] + env_args
            + ["--", "sac", sac_path, "serve", "--transport", "stdio"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  ✓ CLI project config")
            installed = True
        else:
            print(f"  ⚠ CLI config skipped: {(result.stderr or result.stdout).strip()}")

    if not installed:
        print("  ✗ Failed to write any config")
        sys.exit(1)

    # 3. Write .claude/launch.json for built-in preview
    print()
    print("Configuring preview server...")
    launch_dir = Path.cwd() / ".claude"
    launch_file = launch_dir / "launch.json"
    try:
        python_path = sys.executable
        # Health check probes 18420 first, then 18421 (matches MCP server fallback)
        health_script = (
            "import time, urllib.request, json, sys\n"
            "port = None\n"
            "for p in (18420, 18421):\n"
            "    try:\n"
            "        r = urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=2)\n"
            "        d = json.loads(r.read())\n"
            "        if d.get('status') == 'ok':\n"
            "            port = p; break\n"
            "    except Exception: pass\n"
            "if not port:\n"
            "    print('SaC server not found on 18420/18421', file=sys.stderr); sys.exit(1)\n"
            "print(f'Connected to SaC server at http://127.0.0.1:{port}')\n"
            "while True: time.sleep(3600)"
        )
        launch_config = {
            "version": "0.0.1",
            "configurations": [
                {
                    "name": "sac-viewer",
                    "runtimeExecutable": python_path,
                    "runtimeArgs": ["-c", health_script],
                    "port": 18420,
                }
            ],
        }
        launch_dir.mkdir(parents=True, exist_ok=True)
        launch_file.write_text(
            json.dumps(launch_config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  ✓ Preview config (.claude/launch.json)")
    except Exception as exc:
        print(f"  ⚠ Preview config skipped: {exc}")

    # Write ~/.sac/.env so `sac serve` works from any directory
    try:
        _save_global_env(llm_config)
        print(f"  ✓ Global env (~/.sac/.env)")
    except Exception as exc:
        print(f"  ⚠ Global env skipped: {exc}")

    print()
    print("Done! Restart Claude Code to activate SaC tools:")
    print("  generate_app, evolve_app, wait_for_action,")
    print("  list_conversations, get_conversation")
    print()


async def _generate(args: argparse.Namespace) -> None:
    import os
    from sac.sac import SaC

    api_key = os.environ.get("SAC_API_KEY", "")
    if not api_key:
        print("Error: SAC_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    sac = SaC(
        api_key=api_key,
        search_api_key=os.environ.get("SAC_SEARCH_API_KEY"),
    )

    try:
        app = await sac.generate(
            args.intent,
            model=args.model,
            web_search=not args.no_search,
        )
        print(app.code)
    finally:
        await sac.close()


def _publish(args: argparse.Namespace) -> None:
    """Publish content to a running SaC server via /inbox."""
    import json
    import urllib.request

    # Resolve content
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
    elif args.content:
        content = args.content
    elif not sys.stdin.isatty():
        content = sys.stdin.read()
    else:
        print("Error: provide content as argument, --file, or pipe via stdin", file=sys.stderr)
        sys.exit(1)

    if not content.strip():
        print("Error: content is empty", file=sys.stderr)
        sys.exit(1)

    server = (args.server or os.environ.get("SAC_SERVER", "") or "http://127.0.0.1:18420").rstrip("/")

    payload: dict[str, str] = {"content": content}
    if args.intent:
        payload["intent"] = args.intent
    if args.conversation_id:
        payload["conversation_id"] = args.conversation_id
    if args.callback_url:
        payload["callback_url"] = args.callback_url
    if args.callback_format:
        payload["callback_format"] = args.callback_format
    if args.callback_auth:
        payload["callback_auth"] = args.callback_auth

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{server}/inbox"

    try:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=300)
        result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"Error: cannot reach SaC server at {server}: {exc}", file=sys.stderr)
        print("Start the server with: sac serve", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Print result
    app_url = result.get("url", "")
    conv_id = result.get("conversation_id", "")
    version = result.get("version")
    result_type = result.get("type", "")

    if app_url:
        print(app_url)
    if conv_id:
        print(f"  conversation_id: {conv_id}", file=sys.stderr)
    if version is not None:
        print(f"  version: {version}", file=sys.stderr)
    if result_type:
        print(f"  type: {result_type}", file=sys.stderr)


if __name__ == "__main__":
    main()
