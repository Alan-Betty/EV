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
            "Launch a desktop program by name: Chrome, VS Code, Notepad, "
            "Spotify, Task Manager. Starting a program only - use web_search "
            "for the web, and file_manager 'open' to show a folder."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "The name as the user said it.",
                },
                "arguments": {
                    "type": "string",
                    "description": (
                        "Extra command-line arguments. Only a path you have "
                        "actually been told - never one you construct. Leave "
                        "empty if unsure."
                    ),
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Open a browser on a search results page, on a specific URL, or "
            "on a site the user lives in. Handles 'search for a good ergonomic "
            "mouse', 'look that up on YouTube', 'open Chrome and find me a "
            "gaming mouse', and also 'open my email', 'check my calendar' - "
            "for those set engine to mail, calendar or drive and leave query "
            "empty. Do not ask which provider; open the default and let the "
            "user say if they wanted another."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search terms, cleaned of filler. Leave empty for "
                        "mail, calendar and drive."
                    ),
                },
                "engine": {
                    "type": "string",
                    "description": "Where to go. Defaults to google.",
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
                        "mail",
                        "gmail",
                        "outlook",
                        "calendar",
                        "drive",
                    ],
                },
                "browser": {
                    "type": "string",
                    "description": "Only if the user named one.",
                    "enum": ["chrome", "edge", "firefox", "brave", "default"],
                },
                "url": {
                    "type": "string",
                    "description": (
                        "An exact URL, when the user names a site directly."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "dev_workflow",
        "description": (
            "Developer macro: open VS Code on a folder, spawn an integrated "
            "terminal, start the Claude Code CLI, optionally type a prompt. "
            "For 'launch VS Code and start Claude on my API project'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Exactly what the user said - a path, or a bare "
                        "project name to search for. Omit if they named none."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Prompt to send to Claude Code once it starts. Omit if "
                        "the user only asked to start it."
                    ),
                },
                "start_claude": {
                    "type": "boolean",
                    "description": (
                        "Defaults to true. False for VS Code and a plain "
                        "terminal only."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "terminal_command",
        "description": (
            "Run a shell command. Last resort: open_app for programs, "
            "web_search for the browser, file_manager for files. Good for "
            "git status, ipconfig, checking a package version."
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
                        "True to detach a long-running command into its own "
                        "window. False reads the output back. Default false."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "file_manager",
        "description": (
            "Everything to do with files and folders in the user's own "
            "directories: create, read, list, open, copy, move, rename, "
            "delete, find, organise, and whole-folder batch versions of "
            "those. Use it for 'make a file with my shopping list', 'what's "
            "on my Desktop', 'open File Explorer at my GitHub folder', "
            "'copy every invoice to Documents'. Always prefer this over "
            "terminal_command for files, and one batch action over many "
            "single-file calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "'open' shows a folder in File Explorer (or reads a "
                        "file out). 'list' names what is in a folder without "
                        "showing it. 'create' and 'append' need 'content'. "
                        "'copy', 'move' and 'rename' need 'destination'. "
                        "'find' needs 'pattern'. 'organize' sorts a folder "
                        "into type subfolders. The batch actions act on every "
                        "file in 'path' matching 'pattern'."
                    ),
                    "enum": [
                        "create",
                        "append",
                        "read",
                        "list",
                        "copy",
                        "move",
                        "rename",
                        "delete",
                        "open",
                        "makedir",
                        "find",
                        "organize",
                        "batch_copy",
                        "batch_move",
                        "batch_rename",
                    ],
                },
                "path": {
                    "type": "string",
                    "description": (
                        "The file or folder, as the user said it: 'Downloads', "
                        "'my github folder', 'Documents/notes.txt'. Never "
                        "invent an absolute path. Omit the folder and a new "
                        "file lands in Documents."
                    ),
                },
                "destination": {
                    "type": "string",
                    "description": (
                        "Where it goes: a folder to move into, or a new "
                        "filename to rename to."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The full text to write. Write out what the user "
                        "actually asked for, in full - never a placeholder."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Name fragment or glob, e.g. 'invoice' or '*.pdf'. "
                        "Omit to mean every file."
                    ),
                },
                "new_name": {
                    "type": "string",
                    "description": (
                        "Base name for 'batch_rename': 'holiday' gives "
                        "'holiday 1', 'holiday 2'. No extension."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "backlog",
        "description": (
            "The running list of unfinished things - interrupted commands, "
            "failures, and reminders. E.V. adds to it by itself; use this "
            "when the user asks: 'what's outstanding', 'remind me to back up "
            "the photos', 'that one's done', 'retry the first one'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "'add' stores 'text'. 'done', 'drop' and 'run' act on "
                        "'item'. 'clear' empties the list."
                    ),
                    "enum": ["list", "add", "done", "drop", "clear", "run"],
                },
                "text": {
                    "type": "string",
                    "description": "One short line, as the user would say it.",
                },
                "item": {
                    "type": "string",
                    "description": (
                        "A position ('first', 'last', '1') or a few words from "
                        "the item."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Keep or recall a small fact about the user between sessions: "
            "'remember I take my coffee black', 'what do you know about me', "
            "'forget what I said about the editor'. Not for conversation "
            "history, which E.V. keeps anyway."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "'set' needs key and value; 'get' and 'forget' need key.",
                    "enum": ["set", "get", "forget", "list"],
                },
                "key": {
                    "type": "string",
                    "description": "One or two words: 'coffee', 'main project'.",
                },
                "value": {
                    "type": "string",
                    "description": "The fact itself, short.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "take_screenshot",
        "description": (
            "Look at the screen and answer a question about it: 'what's on "
            "my screen', 'what does that error say'. Use it before any "
            "mouse_action, so you aim at something you have seen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "What to find out. Omit for a general description."
                    ),
                },
                "region": {
                    "type": "string",
                    "description": (
                        "Optional 'left,top,right,bottom' as fractions 0-1, to "
                        "look closely at one part. Use it to read small text."
                    ),
                },
                "save_as": {
                    "type": "string",
                    "description": (
                        "Only if the user asked for the shot to be kept, e.g. "
                        "'Pictures/bug.png'. Looking needs no file."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "mouse_action",
        "description": (
            "Drive the real mouse. Screenshot first - never guess where "
            "something is. No undo, so only when open_app, web_search, "
            "browser_task and file_manager cannot do the job."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "What the mouse should do.",
                    "enum": [
                        "move",
                        "click",
                        "double_click",
                        "right_click",
                        "middle_click",
                        "drag",
                        "scroll",
                    ],
                },
                "x": {
                    "type": "string",
                    "description": "Fraction across the screen: 0 left, 1 right.",
                },
                "y": {
                    "type": "string",
                    "description": "Fraction down the screen: 0 top, 1 bottom.",
                },
                "to_x": {"type": "string", "description": "Drag end x. 'drag' only."},
                "to_y": {"type": "string", "description": "Drag end y. 'drag' only."},
                "amount": {
                    "type": "string",
                    "description": "Scroll distance, ~400 a screenful. Negative is down.",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "Visible name of the target, e.g. 'the Save button'. "
                        "Always fill it in - the user is asked to approve it."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "keyboard_action",
        "description": (
            "Type text or send a hotkey to whatever has focus: 'type my "
            "address', 'hit enter'. Screenshot first - the keys go wherever "
            "the cursor already is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "'type' writes 'text'. 'press' sends 'keys'."
                    ),
                    "enum": ["type", "press"],
                },
                "text": {
                    "type": "string",
                    "description": "The exact characters to type, for 'type'.",
                },
                "keys": {
                    "type": "string",
                    "description": "e.g. 'enter', 'ctrl+s', 'alt+tab'.",
                },
                "label": {
                    "type": "string",
                    "description": (
                        "What this is for, in a few words. Shown to the user "
                        "when it needs approving."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "screen_task",
        "description": (
            "A whole desktop job: looks, acts, looks again. Opens apps, "
            "focuses windows, clicks, types, sends shortcuts. Use it when a "
            "request needs more than one of those: 'open Notepad and type "
            "hello', 'open my project in VS Code and run the script'. State "
            "the whole goal in one call. For a web page use browser_task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The whole goal in one sentence, so someone looking "
                        "at the screen could tell it was done. Name the app "
                        "and the file if you know them."
                    ),
                },
                "max_steps": {
                    "type": "string",
                    "description": "Optional ceiling on how many actions it may take.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "browser_task",
        "description": (
            "Automate a website through the page itself rather than by "
            "clicking at pixels: navigate, fill forms, apply filters, click "
            "by visible text, read results back. Use it for 'search Amazon "
            "for a mouse and add the top one to my cart'. Prefer it over "
            "screen_task for anything on the web; use web_search when the "
            "user only wants a page opened to read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The whole errand in one sentence. It is shown in the "
                        "confirmation question, so make it honest."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "The page to start on. A name like 'gmail' or "
                        "'calendar' resolves on its own."
                    ),
                },
                "steps": {
                    "type": "string",
                    "description": (
                        "One action per line: 'verb target' or 'verb target = "
                        "value'. Verbs: goto, click, fill, select, check, "
                        "press, wait, scroll, read. A target with no CSS "
                        "syntax matches visible text. 'read' returns every "
                        "match; end with one to learn what is on the page. "
                        "Example: 'goto amazon.co.uk' / 'fill #search = "
                        "wireless mouse' / 'press Enter' / 'read .s-result-item'."
                    ),
                },
                "headless": {
                    "type": "boolean",
                    "description": (
                        "True to hide the browser. Default false so the user "
                        "can watch."
                    ),
                },
            },
            "required": [],
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
                        "What to say out loud. One or two short sentences, 25 "
                        "words maximum, in E.V.'s dry voice."
                    ),
                }
            },
            "required": ["reply"],
        },
    },
]



# Names a model reaches for when it does not quite recall the schema. Groq
# answered "open gmail and summarise the important mail" with a perfectly
# sensible `browser_task` whose only argument was called `goal`, and since
# `dispatch` filters to the declared properties, that call would have arrived
# with nothing in it and browsed nowhere.
#
# This is a rename, never an addition: an alias is applied only when the tool
# really declares the target property and nothing has already filled it, so
# `TOOL_SPECS` stays the single source of truth and no tool can acquire an
# argument it never described. Anything not listed here is still dropped.
ARGUMENT_ALIASES: dict[str, str] = {
    "goal": "task",
    "objective": "task",
    "instruction": "task",
    "application": "app",
    "program": "app",
    "app_name": "app",
    "args": "arguments",
    "file": "path",
    "filename": "path",
    "filepath": "path",
    "folder": "path",
    "directory_path": "path",
    "search": "query",
    "search_query": "query",
    "website": "url",
    "link": "url",
    "cmd": "command",
    "key": "keys",
    "hotkey": "keys",
    "message": "text",
    "body": "content",
}


def normalise_arguments(tool: str, arguments: dict) -> dict:
    """Rename near-miss argument names onto the ones the tool declares.

    Only for a property the tool actually has and that is not already set.
    An unrecognised name is left alone for `dispatch` to drop, exactly as
    before - this widens what a model can be understood to have meant, not
    what a tool can be asked to do.
    """
    declared = _PROPERTIES.get(tool)
    if not declared or not isinstance(arguments, dict):
        return arguments

    renamed = dict(arguments)
    for alias, target in ARGUMENT_ALIASES.items():
        if alias in renamed and target in declared and target not in renamed:
            if alias not in declared:  # never rename a real property away
                renamed[target] = renamed.pop(alias)
    return renamed


_PROPERTIES: dict[str, set[str]] = {
    spec["name"]: set(spec["parameters"].get("properties", {})) for spec in TOOL_SPECS
}

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
