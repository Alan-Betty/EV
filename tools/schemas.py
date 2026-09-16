"""Canonical JSON-Schema tool definitions exposed to the LLM.

These are provider-neutral. `ev.brain` translates them into whatever shape
Groq (OpenAI-compatible) or Gemini expects, so a tool is described exactly
once, here.
"""

from __future__ import annotations

from typing import Any

ToolSpec = dict[str, Any]


TOOL_SPECS: list[ToolSpec] = [
    {
        "name": "open_app",
        "description": (
            "Launch a desktop application by name, for example Chrome, VS Code, "
            "Notepad, Spotify, Windows Terminal or Task Manager. Use this only "
            "for starting a program. If the user wants a web search, use "
            "web_search instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": (
                        "Application name as the user said it, e.g. 'chrome', "
                        "'vs code', 'notepad', 'task manager'."
                    ),
                },
                "arguments": {
                    "type": "string",
                    "description": (
                        "Optional extra command-line arguments, such as a file "
                        "or folder to open with the app. Leave empty if unsure."
                    ),
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Open a browser on a search results page, or on a specific URL. "
            "Handles requests like 'search for a good ergonomic mouse', 'look "
            "that up on YouTube', or 'open Chrome and find me a gaming mouse'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search terms. Clean them up into a good search "
                        "query; drop filler words like 'can you find me'."
                    ),
                },
                "engine": {
                    "type": "string",
                    "description": (
                        "Which site to search. Defaults to google."
                    ),
                    "enum": [
                        "google",
                        "bing",
                        "duckduckgo",
                        "youtube",
                        "github",
                        "amazon",
                        "maps",
                        "images",
                        "stackoverflow",
                        "wikipedia",
                        "reddit",
                    ],
                },
                "browser": {
                    "type": "string",
                    "description": (
                        "Which browser to open it in. Only set this if the user "
                        "named one; otherwise omit to use the system default."
                    ),
                    "enum": ["chrome", "edge", "firefox", "brave", "default"],
                },
                "url": {
                    "type": "string",
                    "description": (
                        "Open this exact URL instead of running a search. Use "
                        "only when the user names a site to go to directly."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "dev_workflow",
        "description": (
            "Developer macro: open VS Code on a project folder, spawn an "
            "integrated terminal, start the Claude Code CLI in it, and "
            "optionally type an opening prompt. Use for requests like 'launch "
            "VS Code and start Claude Code on my API project'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Project folder to open. Use exactly what the user said "
                        "(a full path, or a bare project name to search for). "
                        "Omit entirely if they did not name one."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Initial prompt to send to Claude Code once it starts, "
                        "e.g. 'review the auth middleware for bugs'. Omit if "
                        "the user only asked to start it."
                    ),
                },
                "start_claude": {
                    "type": "boolean",
                    "description": (
                        "Whether to launch the Claude CLI in the terminal. "
                        "Defaults to true. Set false if the user only wants "
                        "VS Code and a plain terminal."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "terminal_command",
        "description": (
            "Run a shell command on the user's machine. Last resort only: use "
            "open_app for programs and web_search for the browser. Good for "
            "things like git status, ipconfig, listing a folder, or checking a "
            "package version."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The exact shell command to run.",
                },
                "shell": {
                    "type": "string",
                    "description": "Which shell to use. Defaults to powershell.",
                    "enum": ["powershell", "cmd", "bash"],
                },
                "working_directory": {
                    "type": "string",
                    "description": "Directory to run in. Omit for the default.",
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "True for long-running commands that should be detached "
                        "into their own visible terminal window (a dev server, "
                        "a watch task). False to run it and read back the "
                        "output. Defaults to false."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "chat",
        "description": (
            "Speak a reply with no machine action. Use for questions, banter, "
            "acknowledgements, and anything the other tools do not cover."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": (
                        "What to say out loud. One or two short sentences, "
                        "about 25 words maximum, in E.V.'s dry voice."
                    ),
                }
            },
            "required": ["reply"],
        },
    },
]


TOOL_NAMES: frozenset[str] = frozenset(spec["name"] for spec in TOOL_SPECS)


def to_openai_tools() -> list[dict[str, Any]]:
    """Groq / OpenAI-compatible `tools` payload."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in TOOL_SPECS
    ]


def _strip_unsupported(schema: Any) -> Any:
    """Gemini's function-declaration schema is a strict OpenAPI subset.

    It rejects keys it does not know about, so drop everything outside the
    handful it accepts.
    """
    allowed = {
        "type",
        "description",
        "properties",
        "required",
        "enum",
        "items",
        "nullable",
    }
    if isinstance(schema, dict):
        return {
            key: _strip_unsupported(value)
            for key, value in schema.items()
            if key in allowed
        }
    if isinstance(schema, list):
        return [_strip_unsupported(item) for item in schema]
    return schema


def to_gemini_tools() -> list[dict[str, Any]]:
    """Gemini `tools[].functionDeclarations` payload."""
    return [
        {
            "functionDeclarations": [
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": _strip_unsupported(spec["parameters"]),
                }
                for spec in TOOL_SPECS
            ]
        }
    ]
