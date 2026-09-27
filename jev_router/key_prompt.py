"""Collect a TypeSafe key without passing it through an assistant conversation."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys


def _tk_prompt() -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk.Tk()
        root.withdraw()
        try:
            return simpledialog.askstring("Jev Router", "TypeSafe API key", show="*", parent=root)
        finally:
            root.destroy()
    except Exception:
        # Tk may be unavailable on headless machines or minimal Python installs.
        return None


def _native_prompt() -> str | None:
    if sys.platform == "darwin" and shutil.which("osascript"):
        command = ["osascript", "-e", 'return text returned of (display dialog "TypeSafe API key" default answer "" with hidden answer with title "Jev Router")']
    elif sys.platform.startswith("linux"):
        if shutil.which("zenity"):
            command = ["zenity", "--password", "--title=Jev Router"]
        elif shutil.which("kdialog"):
            command = ["kdialog", "--password", "TypeSafe API key", "--title", "Jev Router"]
        else:
            return None
    elif os.name == "nt" and (shell := shutil.which("powershell.exe") or shutil.which("powershell")):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f=New-Object System.Windows.Forms.Form; $f.Text='Jev Router'; $f.Width=420; $f.Height=150; "
            "$box=New-Object System.Windows.Forms.TextBox; $box.Width=370; $box.Left=15; $box.Top=20; "
            "$box.UseSystemPasswordChar=$true; $f.Controls.Add($box); "
            "$ok=New-Object System.Windows.Forms.Button; $ok.Text='OK'; $ok.Left=315; $ok.Top=60; "
            "$ok.DialogResult=[System.Windows.Forms.DialogResult]::OK; $f.Controls.Add($ok); "
            "$f.AcceptButton=$ok; "
            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Write($box.Text) }"
        )
        command = [shell, "-NoProfile", "-STA", "-Command", script]
    else:
        return None
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def hidden_key_prompt() -> str:
    value = _tk_prompt()
    if value is None:
        value = _native_prompt()
    if value is None and sys.stdin.isatty():
        value = getpass.getpass("TypeSafe API key: ")
    if value is None:
        raise ValueError("No hidden input is available here. Rerun setup with --apply in an interactive terminal")
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("TypeSafe key must be one nonempty line")
    return value
