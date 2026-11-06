"""Minimal offline stand-in for tkinterdnd2 (no drag-and-drop in tests).

Used only by the local test harness when the real package is unavailable; a
real tkinterdnd2 earlier on sys.path takes precedence.
"""

import tkinter as tk

DND_FILES = "DND_Files"
DND_TEXT = "DND_Text"


class Tk(tk.Tk):
    def drop_target_register(self, *types):
        return None

    def drop_target_unregister(self, *types):
        return None

    def dnd_bind(self, sequence, func=None, add=None):
        return None


TkinterDnD = type("TkinterDnD", (), {"Tk": Tk})
