"""Canonical JSON-Schema tool definitions exposed to the LLM.

These are provider-neutral. `leo.brain` translates them into whatever shape
Groq (OpenAI-compatible) or Gemini expects, so a tool is described exactly
once, here.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

ToolSpec = dict[str, Any]

# What `chat` may put on Leo's face. Every one but "demo" is a mood in
# `leo/face/expressions.json`, which a test checks; the loop's own states -
# listening, speaking, focused, the red lockdown alert - are deliberately
# not offered, because wearing one of those would be a lie about what Leo
# is doing.
FACE_MOODS: tuple[str, ...] = (
    "happy", "sad", "angry", "surprised", "confused", "skeptical", "excited",
    "smug", "wink", "worried", "bored", "sleepy", "thinking", "demo",
)


TOOL_SPECS: list[ToolSpec] = [
    {
        "name": "open_app",
        "description": (
            "Launch a program by name - 'open Spotify'. Programs only: "
            "web_search for the web, file_manager 'open' for a folder; "
            "close/switch: app_control."
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
                        "Extra arguments. Only a path you were told, never "
                        "one you construct. Empty if unsure."
                    ),
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "app_control",
        "description": (
            "Act on an open program: switch to, close, quit, minimize, kill, "
            "list what's open; read its window, then press, set_text or menu "
            "by ref - no mouse."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list", "focus", "close", "quit", "minimize", "maximize",
                        "kill", "read", "press", "set_text", "menu",
                    ],
                },
                "app": {"type": "string", "description": "Program; empty = focused."},
                "target": {"type": "string", "description": "Ref, label, or 'File > Save As'."},
                "text": {"type": "string"},
                "discard": {"type": "boolean", "description": "Close without saving."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Only opens a page - a search, a URL, or engine mail, calendar "
            "or drive with no query. It cannot click, fill or read; that is "
            "browser_task. Never ask which provider."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms. Empty for mail, calendar, drive.",
                },
                "engine": {
                    "type": "string",
                    "description": "Default google.",
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
                    "enum": ["chrome", "edge", "firefox", "brave", "default"],
                },
                "url": {
                    "type": "string",
                    "description": "Exact URL, if named.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "dev_workflow",
        "on_demand": True,
        "description": (
            "Developer macro: open VS Code on a folder, spawn an integrated "
            "terminal, start the Claude Code CLI, optionally type a prompt."
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
                    "description": "Prompt for Claude Code once it starts.",
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
            "Run a shell command - git status, a package version. Last "
            "resort: never for programs, web pages or files."
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
                    "enum": ["powershell", "cmd", "bash"],
                },
                "working_directory": {
                    "type": "string",
                    "description": "Omit for the default.",
                },
                "background": {
                    "type": "boolean",
                    "description": "True detaches; false reads output back.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "file_manager",
        "description": (
            # The action list that used to sit here repeated the `action`
            # enum below it word for word, and the enum is the copy the
            # model actually has to satisfy. Paying for it twice on every
            # turn bought nothing - see the token budget note in CLAUDE.md.
            "Anything to do with the user's files and folders: 'what's on "
            "my Desktop', 'copy every invoice to Documents'. Always prefer "
            "this over terminal_command, and one batch action over many "
            "single calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "'open' shows a folder in Explorer, or reads a file "
                        "out; 'list' names its contents without showing it. "
                        "'create'/'append' need 'content'; "
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
                        "The file or folder as the user said it: "
                        "'Downloads', 'Documents/notes.txt'. Never invent an "
                        "absolute path. Omitted, a new file lands in "
                        "Documents."
                    ),
                },
                "destination": {
                    "type": "string",
                    "description": "A folder to move into, or a new filename.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The full text to write, never a placeholder."
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
        "on_demand": True,
        "description": (
            "What Leo itself left unfinished - interrupted commands and "
            "failures: 'what's outstanding', 'retry the first one'. The "
            "user's own list is manage_todo."
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
            "Save something lasting: key+value a short fact ('coffee'="
            "'black'); value alone a free note. forget deletes it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "1-2 words; omit for a note."},
                "value": {"type": "string"},
                "forget": {"type": "boolean"},
                "about": {"type": "string", "description": "Person, or 'topic: name'."},
                "link": {"type": "string", "description": "e.g. 'sister: Sam'."},
            },
            "required": [],
        },
    },
    {
        "name": "recall_fact",
        "description": (
            "Look up a fact or search notes by meaning: 'where's my spare "
            "key'. No key lists all."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Name or search words."},
                "about": {"type": "string", "description": "Person or topic."},
            },
            "required": [],
        },
    },
    {
        "name": "manage_todo",
        "description": (
            "The user's own to-do list, kept between sessions: 'add milk "
            "to my list', 'what's on my list'. Not the backlog, which is "
            "what Leo itself left unfinished."
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
            "Look at the screen and answer a question about it: 'what does "
            "that error say'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Omit to describe it.",
                },
                "region": {
                    "type": "string",
                    "description": "Zoom: 'left,top,right,bottom' as 0-1.",
                },
                "save_as": {
                    "type": "string",
                    "description": "Only if asked to keep it.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "mouse_action",
        "description": (
            "Drive the real mouse. Screenshot first - never guess. Last "
            "resort, no undo."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
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
                    "description": "0-1 from left.",
                },
                "y": {
                    "type": "string",
                    "description": "0-1 from top.",
                },
                "to_x": {"type": "string", "description": "Drag end."},
                "to_y": {"type": "string", "description": "Drag end."},
                "amount": {
                    "type": "string",
                    "description": "Scroll; ~400 a screen, negative down.",
                },
                "label": {
                    "type": "string",
                    "description": "Target's visible name: 'the Save button'. Always set.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "keyboard_action",
        "description": (
            "Type text or send a hotkey to whatever has focus: 'type my "
            "address', 'hit enter'. Screenshot first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["type", "press"],
                },
                "text": {
                    "type": "string",
                    "description": "Exact characters, for 'type'.",
                },
                "keys": {
                    "type": "string",
                    "description": "e.g. 'enter', 'ctrl+s', 'alt+tab'.",
                },
                "label": {
                    "type": "string",
                    "description": "What it is for; shown for approval.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "screen_task",
        "description": (
            "A whole job inside desktop apps: looks, acts, looks again - "
            "'open Notepad and type hello', 'turn on dark mode in Settings'. "
            "One call per goal. Web pages: browser_task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The whole goal, checkable on screen. Name the app "
                        "and file if known."
                    ),
                },
                "max_steps": {
                    "type": "string",
                    "description": "Optional step ceiling.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "browser_task",
        "description": (
            "Do anything on a website through the page, not the pixels: "
            "log in, search, click, fill, play, read results back. Give "
            "task alone and it works the page out itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The whole errand in one sentence; it is shown in "
                        "the confirmation."
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
                        "Optional; leave empty unless you know the page. "
                        "One per line: goto, click, fill, select, check, "
                        "press, wait, scroll, read. Plain text targets match "
                        "visible text. Example: 'fill #search = mouse' / "
                        "'press Enter' / 'read .s-result-item'."
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
        "name": "agent_task",
        "description": (
            "Take the screen and keep working across apps and pages until a "
            "whole errand is done: 'find a gaming mouse under 5000 with an "
            "infinite scroll wheel and put it in my basket'. For jobs that "
            "need judgement between steps; screen_task and browser_task are "
            "the single-job versions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The whole errand in one sentence, naming where to "
                        "start and what decides when it is done."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "system_control",
        "on_demand": True,
        "description": (
            "This machine: status, processes, ports; and servers you start: "
            "start, stop, list, logs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "processes", "ports", "start", "stop", "list", "logs"],
                },
                "name": {"type": "string", "description": "Server name, process filter or port."},
                "command": {"type": "string", "description": "What start runs."},
                "directory": {"type": "string", "description": "Where start runs it."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "dev_session",
        "on_demand": True,
        "description": (
            "Leave a coding job running in Claude Code on its own branch: start, "
            "status, continue, stop. Reports back when it stops."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "status", "continue", "stop"]},
                "goal": {"type": "string", "description": "What to do, or what to do next."},
                "directory": {"type": "string", "description": "The project."},
                "id": {"type": "string", "description": "Omit for the latest."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "whatsapp",
        "on_demand": True,
        "description": (
            "The user's WhatsApp: search, recent, fetch a file, send_to_phone a local "
            "file, or send one message after a spoken yes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "recent", "fetch", "send_to_phone", "send"],
                },
                "query": {"type": "string"},
                "contact": {"type": "string", "description": "As the user said it."},
                "message": {"type": "string", "description": "What to say; drafted in their style."},
                "exact": {"type": "boolean", "description": "They dictated it."},
                "id": {"type": "string", "description": "For fetch."},
                "path": {"type": "string", "description": "For send_to_phone."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "media_control",
        "on_demand": True,
        "description": (
            "Music already playing, and system volume: pause, skip, what's "
            "on, louder, mute. New music is browser_task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "play", "pause", "next", "previous", "now_playing",
                        "volume_up", "volume_down", "set_volume", "mute",
                        "unmute",
                    ],
                },
                "level": {
                    "type": "integer",
                    "description": "0-100, or the step for up/down.",
                },
            },
            "required": ["action"],
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
                        "words max, in Leo's warm dry voice."
                    ),
                },
                "mood": {
                    "type": "string",
                    "description": (
                        "Your face while you say it."
                    ),
                    "enum": list(FACE_MOODS),
                },
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
        # Notes are recalled by question, not by the word "remember".
        # Phrases, not "note": these are prefix matches and "notepad" exists.
        "note that", "note down", "make a note", "take a note", "my notes",
        "your notes", "where did i", "where's my", "wheres my", "what did i",
        "did i tell", "i told you", "last time",
        # People and projects are asked about by name, with nothing else in
        # the sentence to say that memory is what is wanted.
        "know about", "who is", "who's", "sister", "brother", "mum", "mom", "dad", "wife",
        "husband", "girlfriend", "boyfriend", "friend", "colleague", "birthday",
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
        "settings", "toggle", "checkbox",
    ),
    # Window-level verbs. These used to pull in the whole screen family, so a
    # plain "close Spotify" paid for four vision schemas to reach something
    # the window manager does in one call.
    "app_control": (
        "close", "quit", "exit", "kill", "force quit", "switch to", "switch",
        "focus", "bring up", "go back to", "minimis", "minimiz", "maximis",
        "maximiz", "what's open", "whats open", "apps",
        "window", "windows", "running", "save", "menu", "button", "dialog",
        "settings",
    ),
    # Anything that happens *on* a site. Missing here is the expensive
    # direction: with only `web_search` on offer, "log into netflix" or
    # "go to wikipedia and read me the intro" opened a page and reported
    # success, and the browser automation was never even shown to the model.
    # Volume and mute used to pull in the whole screen family, which cost
    # four tool schemas to reach a knob the OS turns in one call.
    "media_control": (
        "music", "song", "songs", "track", "volume", "louder", "quieter",
        "softer", "mute", "unmute", "pause", "resume", "skip", "next",
        "previous", "playing", "play", "spotify", "turn it up", "turn it down",
        "sound", "audio",
    ),
    "dev_session": (
        "coding session", "code session", "dev session", "session", "coding agent", "claude code",
        "fix the bug", "fix this bug", "fix that bug", "implement", "refactor", "make the tests pass",
        "write the code", "add a feature", "on its own branch", "worktree",
    ),
    "whatsapp": (
        "whatsapp", "message", "messages", "msg", "texted", "text her", "text him", "text my",
        "text them", "tell my", "what did", "did they say", "did she say", "did he say",
        "to my phone", "on my phone", "chat with",
    ),
    "system_control": (
        "cpu", "ram", "memory usage", "disk space", "free space", "process", "processes",
        "port", "ports", "server", "servers", "localhost", "uptime", "system status",
        "resources", "logs", "log file", "spin up", "dev server", "hogging", "using so much",
    ),
    "browser_task": (
        "browser", "chrome", "edge", "firefox", "website", "webpage", "site",
        "web", "online", "page", "link", "result", "go to", "goto",
        "navigate", "visit", "playwright",
        "gmail", "email", "mail", "inbox", "login", "log in", "log into",
        "sign in", "signin", "sign into", "account", "form",
        "amazon", "flipkart", "ebay", "youtube", "netflix", "spotify web",
        "reddit", "wikipedia", "github", "google", "linkedin", "twitter",
        "instagram", "facebook", "whatsapp web", "cart", "checkout",
        "summaris", "summariz", "url", "tab", "play", "watch", "stream",
    ),
    # An errand rather than an action: words that describe an outcome
    # somebody wants reached, with the steps left unsaid. Generous on
    # purpose - a false positive costs tokens on one turn, a false negative
    # costs the user a job Leo could have finished by itself.
    "agent_task": (
        "buy", "order", "cart", "basket", "checkout", "book", "reserve",
        "apply", "sign up", "subscribe", "compare", "cheapest", "under",
        "best", "research", "shop", "shopping", "find me", "get me",
        "sort out", "handle", "take over", "keep going", "until", "figure out",
        "work out", "on your own", "autonomous", "whole thing", "for me",
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
    # An errand on a website is the one place where "one scripted run" and
    # "keep going until it is done" are both plausible, and the choice
    # between them is the model's to make. Showing only one turns that
    # choice into whichever word happened to match.
    frozenset({"browser_task", "agent_task"}),
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


# A domain or a URL names a website whatever words surround it -
# "example.com", "amazon.in", "news.ycombinator.com". Word lists cannot hold
# every site, so the shape of one is matched instead.
_DOMAIN = re.compile(
    r"https?://|www\.|\b[a-z0-9-]+\.(?:com|in|org|net|io|co|dev|app|ai|gov|edu|uk|me|tv)\b"
)


# -- the on-demand tier ------------------------------------------------------
#
# `"on_demand": True` on a spec means the tool is only ever sent when this
# utterance asks for it, or a recent one did. It leaves the full schema, so it
# does not count against the two-turn ceiling that `tests/test_memory_tools.py`
# enforces - which is what lets a narrow tool exist at all when there are 28
# tokens of headroom. A flag rather than a second list, because `_ALLOWED_ARGS`,
# the aliases and every schema test read `TOOL_SPECS`: a tool missing from it
# silently loses all its arguments.
#
# The cost is that the tool-failure retry cannot rescue a miss here, since the
# retry only knows what was triggered. So triggers for these are generous, and
# `ON_DEMAND_MAX` bounds how many can ride on one turn: an utterance that
# matches every list must still fit the budget.
ON_DEMAND_MAX = 2

ON_DEMAND_TOOLS: frozenset[str] = frozenset(
    spec["name"] for spec in TOOL_SPECS if spec.get("on_demand")
)


def select_tools(
    *texts: str, sticky: Sequence[str] = (), max_on_demand: int = ON_DEMAND_MAX
) -> list[str]:
    """The tools worth offering for this utterance, in declaration order.

    Never returns an empty list: `CORE_TOOLS` is always in it, so a
    transcript nothing matches still reaches `chat`. `sticky` is on-demand
    tools a recent turn used: "continue it" names nothing, and without this
    the tool that "it" refers to would have vanished by then.
    """
    haystack = " ".join(text for text in texts if text).lower()
    chosen = set(CORE_TOOLS)
    wanted: list[tuple[int, str]] = []
    for name, pattern in _TRIGGER_PATTERNS.items():
        match = pattern.search(haystack)
        if not match:
            continue
        if name in ON_DEMAND_TOOLS:
            wanted.append((match.start(), name))
        else:
            chosen.add(name)
    # What was said first is what was asked for, and what was said goes ahead
    # of what merely was recent.
    ranked = [name for _position, name in sorted(wanted)]
    ranked += [name for name in sticky if name in ON_DEMAND_TOOLS and name not in ranked]
    chosen.update(ranked[: max(0, max_on_demand)])
    if _DOMAIN.search(haystack):
        chosen.add("browser_task")
    for family in _FAMILIES:
        if chosen & family:
            chosen |= family
    # Declaration order, so two identical utterances produce byte-identical
    # payloads and a diff of the request is readable.
    return [spec["name"] for spec in TOOL_SPECS if spec["name"] in chosen]


def base_specs() -> list[ToolSpec]:
    """The full schema: everything except the on-demand tier."""
    return [spec for spec in TOOL_SPECS if not spec.get("on_demand")]


def full_schema_names(selected: Sequence[str]) -> list[str]:
    """Every base tool, plus the on-demand tools already picked this turn.

    What "put the whole schema back" means now. Dropping the on-demand picks
    on a retry would take away the one tool the user just asked for by name.
    """
    wanted = set(selected)
    return [
        spec["name"] for spec in TOOL_SPECS if not spec.get("on_demand") or spec["name"] in wanted
    ]


def _specs_for(names: Sequence[str] | None) -> list[ToolSpec]:
    """`TOOL_SPECS` filtered to `names`, or the full schema when `names` is None.

    An unknown name is ignored rather than raised on: the selector is a
    heuristic and a typo in it must not take the assistant down.
    """
    if names is None:
        return base_specs()
    wanted = set(names)
    return [spec for spec in TOOL_SPECS if spec["name"] in wanted] or base_specs()


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
