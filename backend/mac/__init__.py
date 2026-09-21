"""
Saksham AI Mac Integration Module
"""

from .applescript_bridge import (
    run_applescript,
    AppleScriptTemplates,
    activate_app,
    quit_app,
    get_frontmost_app,
    open_url,
    type_text,
    notify,
)
from .app_controller import AppController
from .terminal_executor import TerminalExecutor
from .file_controller import FileController
from .browser_controller import BrowserController
from .music_controller import MusicController
from .youtube_controller import YouTubeController

__all__ = [
    # AppleScript Bridge
    "run_applescript",
    "AppleScriptTemplates",
    "activate_app",
    "quit_app",
    "get_frontmost_app",
    "open_url",
    "type_text",
    "notify",
    # Controllers
    "AppController",
    "TerminalExecutor",
    "FileController",
    "BrowserController",
    "MusicController",
]
