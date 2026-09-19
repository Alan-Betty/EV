"""Canonical JSON-Schema tool definitions exposed to the LLM.

These are provider-neutral. `ev.brain` translates them into whatever shape
Groq (OpenAI-compatible) or Gemini expects, so a tool is described exactly
once, here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
                        "Extra arguments. Only a path you were actually told "
                        "- never one you construct. Empty if unsure."
                    ),
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Open a browser on a search, a URL, or a site the user lives "
            "in: 'find me a gaming mouse', 'open my email', 'check my "
            "calendar'. For those last two set engine to mail, calendar or "
            "drive and leave query empty. Never ask which provider - open "
            "the default."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search terms, cleaned of filler. Empty for mail, "
                        "calendar and drive."
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
                    "description": "An exact URL, if the user named a site.",
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
                        "Exactly what the user said - a path or a bare "
                        "project name. Omit if they named none."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "Prompt for Claude Code once it starts. Omit if they "
                        "only asked to start it."
                    ),
                },
                "start_claude": {
                    "type": "boolean",
                    "description": "Default true. False for VS Code alone.",
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
                        "True detaches a long command into its own window. "
                        "False reads the output back."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "file_manager",
        "description": (
            "Anything to do with the user's files and folders: create, "
            "read, list, open, copy, move, rename, delete, find, organise, "
            "and batch versions of those. 'what's on my Desktop', 'open "
            "Explorer at my GitHub folder', 'copy every invoice to "
            "Documents'. Always prefer this over terminal_command, and one "
            "batch action over many single calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "'open' shows a folder in Explorer, or reads a file "
                        "out. 'list' names a folder's contents without "
                        "showing it. 'create'/'append' need 'content'; "
                        "'copy'/'move'/'rename' need 'destination'; 'find' "
                        "needs 'pattern'. Batch actions act on every file in "
                        "'path' matching 'pattern'."
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
                        "The file or folder as the user said it: 'Downloads', "
                        "'my github folder', 'Documents/notes.txt'. Never "
                        "invent an absolute path. Omitted, a new file lands "
                        "in Documents."
                    ),
                },
                "destination": {
                    "type": "string",
                    "description": "A folder to move into, or a new filename.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The full text to write - what the user actually "
                        "asked for, never a placeholder."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "description": (
                        "Name fragment or glob: 'invoice', '*.pdf'. Omit for "
                        "every file."
                    ),
                },
                "new_name": {
                    "type": "string",
                    "description": (
                        "Base name for 'batch_rename': 'holiday' gives "
                        "'holiday 1', 'holiday 2'."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "backlog",
        "description": (
            "What E.V. itself left unfinished - interrupted commands and "
            "failures. E.V. files these by itself; use this for 'what's "
            "outstanding', 'retry the first one'. The user's own list is "
            "manage_todo."
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
                    "description": "A position ('first', '1') or a few words of it.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Store one lasting fact about the user: 'remember I take my "
            "coffee black', 'my name is Alan', 'call me at the office'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "One or two words: 'coffee', 'name'.",
                },
                "value": {"type": "string", "description": "The fact itself, short."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall_fact",
        "description": (
            "Look up a stored fact: 'what do you know about me', 'what's my "
            "main project'. Omit key to list everything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The fact to fetch."},
            },
            "required": [],
        },
    },
    {
        "name": "manage_todo",
        "description": (
            "The user's own to-do list, kept between sessions: 'add milk to "
            "my list', 'what's on my list', 'that one's done'. Not the "
            "backlog, which is what E.V. itself left unfinished."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "'add' needs item; 'done' and 'drop' name one.",
                    "enum": ["add", "list", "done", "drop", "clear"],
                },
                "item": {
                    "type": "string",
                    "description": "The errand, or a position: 'first', '2'.",
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
                        "'left,top,right,bottom' as fractions 0-1, to look "
                        "closely. Use it to read small text."
                    ),
                },
                "save_as": {
                    "type": "string",
                    "description": (
                        "Only if the user asked for it to be kept, e.g. "
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
            "Drive the real mouse. Screenshot first - never guess. No "
            "undo: only when open_app, web_search, browser_task and "
            "file_manager cannot do it."
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
                        "Visible name of the target: 'the Save button'. "
                        "Always fill it in - the user approves it."
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
                        "and file if you know them."
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
            "Automate a website through the page rather than the pixels: "
            "navigate, fill forms, click by visible text, read results back. "
            "'search Amazon for a mouse and add the top one to my cart'. "
            "Prefer it over screen_task on the web; use web_search when the "
            "user only wants a page opened."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The whole errand in one sentence. It is shown in the "
                        "confirmation, so make it honest."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "The page to start on. 'gmail' or 'calendar' resolve "
                        "on their own."
                    ),
                },
                "steps": {
                    "type": "string",
                    "description": (
                        "One action per line. Verbs: goto, click, fill, "
                        "select, check, press, wait, scroll, read. A target "
                        "with no CSS syntax matches visible text. 'read' "
                        "returns every match; end with one to learn what is "
                        "on the page. Example: 'goto amazon.co.uk' / 'fill "
                        "#search = wireless mouse' / 'press Enter' / 'read "
                        ".s-result-item'."
                    ),
                },
                "headless": {
                    "type": "boolean",
                    "description": "True hides the browser. Default false.",
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
                        "What to say out loud. Two or three sentences, 35 "
                        "words max, in E.V.'s warm dry voice."
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


# -- per-utterance tool selection -------------------------------------------
#
# Groq's free tier meters tokens per minute, and the whole tool schema rides
# on every single request: ~2830 tokens of the ~4400 a real turn costs. Most
# of that is irrelevant to the utterance paying for it. "Open notepad" is
# charged 401 tokens to be told about `file_manager` and 270 about
# `browser_task`, and at 8000 tokens a minute that is the difference between
# one command a minute and three.
#
# So the schema is filtered per utterance. Two things keep that from costing
# the user a capability, and both matter:
#
#   * `CORE_TOOLS` is offered every time. It is the cheap, high-frequency set
#     - `chat`, `open_app`, `web_search` - which between them answer most of
#     what anyone actually says to a voice assistant, and it means a
#     transcript that matches nothing still has somewhere to go.
#   * A miss is recoverable rather than final. `Brain` retries once with the
#     full schema when a subset produces no usable tool call, so routing that
#     guesses wrong costs one round trip instead of the user's request.
#
# That asymmetry is why the word lists below are deliberately generous. A
# false positive costs tokens on one turn. A false negative costs the user
# the thing they asked for, and they have no way to tell why.

CORE_TOOLS: tuple[str, ...] = ("chat", "open_app", "web_search")

# Word stems that make a tool worth its tokens on this turn. Matched with
# word boundaries against the lowercased transcript, so "copy" does not fire
# on "copyright" - but stems are used where a suffix is predictable
# ("organis", "delet"), because the alternative is enumerating conjugations.
_TRIGGERS: dict[str, tuple[str, ...]] = {
    "file_manager": (
        "file", "files", "folder", "folders", "directory", "desktop",
        "download", "downloads", "document", "documents", "pdf", "pdfs",
        "copy", "move", "rename", "delet", "organis", "organiz",
        "tidy", "sort", "explorer", "zip", "archive", "trash",
        "picture", "pictures", "photo", "photos", "txt", "csv", "docx",
    ),
    "terminal_command": (
        "run", "command", "terminal", "shell", "powershell", "cmd",
        "git", "npm", "pip", "python", "node", "install", "script",
        "execute", "ping", "curl", "build", "compile",
    ),
    "dev_workflow": (
        "vscode", "project", "repo", "repository", "commit", "branch",
        "claude", "editor", "ide", "workspace",
    ),
    "backlog": (
        "backlog", "pending", "unfinished", "outstanding", "leftover",
        "retry", "failed", "interrupted",
    ),
    # One list for the whole memory family, because any match pulls all
    # three anyway and splitting it would only invite a wrong split.
    "remember_fact": (
        "remember", "remind", "reminder", "forget", "recall", "memoris",
        "memoriz", "prefer", "favourite", "favorite", "todo", "to-do",
        "task", "tasks", "shopping", "errand", "agenda", "know about me",
    ),
    "recall_fact": (),
    "manage_todo": (),
    "take_screenshot": (
        "screenshot", "screen", "capture", "visible", "showing",
        "what's on", "whats on", "look at",
    ),
    "mouse_action": (
        "click", "drag", "scroll", "cursor", "mouse", "pointer", "hover",
    ),
    "keyboard_action": (
        "type", "typing", "press", "key", "keys", "hotkey", "keyboard",
        "shortcut", "ctrl", "alt", "shift", "enter", "escape", "paste",
    ),
    "screen_task": (
        "click", "type", "screen", "window", "button", "menu", "dialog",
        "settings", "volume", "mute", "toggle", "checkbox",
        "close", "minimis", "minimiz", "maximis", "maximiz",
    ),
    "browser_task": (
        "browser", "chrome", "edge", "firefox", "website", "webpage",
        "gmail", "email", "mail", "inbox", "login", "sign in", "signin",
        "amazon", "youtube", "reddit", "cart", "checkout",
        "summaris", "summariz", "url", "tab",
    ),
}

# Tools that only make sense offered together. The model's job on a GUI
# request is to choose between one shot and a loop, and between the pointer
# and the keyboard; showing it two of the four turns that choice into a
# guess. The memory three are bundled for the same reason - "remember what I
# told you" is a recall and "remember that I like X" is a store, and one
# word separates them.
_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"take_screenshot", "mouse_action", "keyboard_action", "screen_task"}),
    frozenset({"remember_fact", "recall_fact", "manage_todo"}),
)

# `if words` is load-bearing. An empty alternation compiles to `\b(?:)`,
# which matches the empty string at the first word boundary of anything at
# all - so a tool with no trigger words of its own was offered on every
# single utterance, which is the exact opposite of the point.
_TRIGGER_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")")
    for name, words in _TRIGGERS.items()
    if words
}


def select_tools(*texts: str) -> list[str]:
    """The tools worth offering for this utterance, in declaration order.

    Never returns an empty list: `CORE_TOOLS` is always in it, so a
    transcript nothing matches still reaches `chat`.
    """
    haystack = " ".join(text for text in texts if text).lower()
    chosen = set(CORE_TOOLS)
    for name, pattern in _TRIGGER_PATTERNS.items():
        if pattern.search(haystack):
            chosen.add(name)
    for family in _FAMILIES:
        if chosen & family:
            chosen |= family
    # Declaration order, so two identical utterances produce byte-identical
    # payloads and a diff of the request is readable.
    return [spec["name"] for spec in TOOL_SPECS if spec["name"] in chosen]


def _specs_for(names: Sequence[str] | None) -> list[ToolSpec]:
    """`TOOL_SPECS` filtered to `names`, or all of them when `names` is None.

    An unknown name is ignored rather than raised on: the selector is a
    heuristic and a typo in it must not take the assistant down.
    """
    if names is None:
        return list(TOOL_SPECS)
    wanted = set(names)
    return [spec for spec in TOOL_SPECS if spec["name"] in wanted] or list(TOOL_SPECS)


def to_openai_tools(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Groq / OpenAI-compatible `tools` payload, optionally a subset.

    `names` is what `select_tools` picked for this utterance. None means all
    of them, which is what the callers that are measuring the schema - and
    the last rung of the tool-failure ladder - both want.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in _specs_for(names)
    ]


# Everything Gemini's `Schema` message understands. Anything else in a spec
# is dropped rather than sent, because Gemini rejects the whole request over a
# single key it does not know.
_GEMINI_SCHEMA_KEYS = frozenset(
    {"type", "description", "properties", "required", "enum", "items", "nullable"}
)


def _strip_unsupported(schema: Any) -> Any:
    """Translate one JSON-Schema node into Gemini's OpenAPI subset.

    The structure has to be walked by *position*, not uniformly, and getting
    that wrong is silent rather than loud. `properties` is a map of property
    name to schema: its keys are the tool's own argument names, so filtering
    them against the keyword list deletes every one of them. That is exactly
    what used to happen here - `open_app` reached Gemini as

        {"type": "object", "properties": {}, "required": ["app"]}

    and the API answered `required[0]: property is not defined`, because by
    then `app` really was not defined. Every tool went out with no arguments
    at all, so the whole Gemini provider was dead on arrival: either a 400, or
    a function call with an empty `args`.

    So three different kinds of node, handled three different ways:

    * a schema node - filter its keys to the ones Gemini knows,
    * `properties` - keep every key, recurse into the values only,
    * `required` and `enum` - lists of plain strings, never schemas, so they
      are copied across untouched.
    """
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            # Property *names*, not schema keywords. Recurse into the values.
            out[key] = {name: _strip_unsupported(sub) for name, sub in value.items()}
        elif key in {"required", "enum"} and isinstance(value, list):
            # Plain strings. Recursing would treat each one as a schema.
            out[key] = list(value)
        elif key == "items":
            out[key] = _strip_unsupported(value)
        else:
            out[key] = value
    return out


def to_gemini_tools(names: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Gemini `tools[].functionDeclarations` payload, optionally a subset.

    Takes the same `names` as `to_openai_tools` and must be given the same
    ones on any given turn: failover re-runs the utterance in the other
    dialect, and a request that needed `file_manager` on Groq would not
    find it on Gemini if the two were selected separately.
    """
    return [
        {
            "functionDeclarations": [
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": _strip_unsupported(spec["parameters"]),
                }
                for spec in _specs_for(names)
            ]
        }
    ]
