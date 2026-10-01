"""E.V.'s face: a floating robot whose eyes say what the assistant is doing.

Run as its own process - `python -m ev.face` - and driven over stdin. This
package must stay importable without Qt: nothing here imports PySide6 at
module scope except `render` and `window`, which only the face process loads.
"""
