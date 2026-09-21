"""
Saksham AI - Application Controller
Control Mac applications: open, close, interact.
"""

import asyncio
from typing import Optional

from loguru import logger

from .applescript_bridge import run_applescript, AppleScriptTemplates


class AppController:
    """
    Controls Mac applications for Saksham AI.
    """
    
    # Common app identifiers
    APPS = {
        "vscode": "Visual Studio Code",
        "code": "Visual Studio Code",
        "cursor": "Cursor",
        "chrome": "Google Chrome",
        "safari": "Safari",
        "finder": "Finder",
        "terminal": "Terminal",
        "iterm": "iTerm",
        "slack": "Slack",
        "spotify": "Spotify",
        "music": "Music",
        "notes": "Notes",
        "messages": "Messages",
        "mail": "Mail",
        "calendar": "Calendar",
        "reminders": "Reminders",
    }
    
    def __init__(self):
        self._load_custom_aliases()

    def _load_custom_aliases(self):
        """Load custom aliases from apps.json"""
        import json
        from pathlib import Path
        
        try:
            p = Path(__file__).parent / "apps.json"
            if p.exists():
                with open(p, "r") as f:
                    custom = json.load(f)
                    self.APPS.update(custom)
                    logger.info(f"Loaded {len(custom)} custom app aliases")
        except Exception as e:
            logger.warning(f"Failed to load apps.json: {e}")
    
    def _resolve_app_name(self, name: str) -> str:
        """Resolve common aliases to actual app names"""
        return self.APPS.get(name.lower(), name)
    
    def add_alias(self, alias: str, app_name: str) -> bool:
        """Add a new alias dynamically"""
        import json
        from pathlib import Path
        
        self.APPS[alias.lower()] = app_name
        
        # Persist
        try:
            p = Path(__file__).parent / "apps.json"
            # Read existing
            current = {}
            if p.exists():
                with open(p, "r") as f:
                    current = json.load(f)
            
            current[alias.lower()] = app_name
            
            with open(p, "w") as f:
                json.dump(current, f, indent=4)
                
            return True
        except Exception as e:
            logger.error(f"Failed to save alias: {e}")
            return False
    
    async def open_app(
        self,
        app_name: str,
        file_path: Optional[str] = None,
        url: Optional[str] = None,
    ) -> dict:
        """
        Open an application, optionally with a file or URL.
        
        Args:
            app_name: Name or alias of the application
            file_path: Optional file to open in the app
            url: Optional URL to open (for browsers)
        """
        resolved_name = self._resolve_app_name(app_name)
        
        try:
            # If resolved name is a path (starts with /), use it directly or extract name
            if "/" in resolved_name and resolved_name.endswith(".app"):
                # For paths, we might interpret differently or pass to open command
                pass 
            
            if file_path:
                script = AppleScriptTemplates.open_file(file_path, resolved_name)
            elif url:
                script = AppleScriptTemplates.open_url(url, resolved_name)
            else:
                # If it looks like a path, try "do shell script open" which is more robust for paths
                if resolved_name.startswith("/"):
                    script = f'do shell script "open -a \\"{resolved_name}\\""'
                else:
                    script = AppleScriptTemplates.activate_app(resolved_name)
            
            await run_applescript(script)
            logger.info(f"Opened {resolved_name}")
            
            return {
                "success": True,
                "app": resolved_name,
                "file": file_path,
                "url": url,
            }
            
        except Exception as e:
            logger.error(f"Failed to open {resolved_name}: {e}")
            return {"success": False, "error": str(e)}
    
    async def close_app(self, app_name: str) -> dict:
        """Close an application"""
        resolved_name = self._resolve_app_name(app_name)
        
        try:
            await run_applescript(AppleScriptTemplates.quit_app(resolved_name))
            return {"success": True, "closed": resolved_name}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def get_frontmost_app(self) -> str:
        """Get the currently active application"""
        return await run_applescript(AppleScriptTemplates.get_frontmost_app())
    
    async def get_windows(self, app_name: str) -> list[str]:
        """Get list of window titles for an app"""
        resolved_name = self._resolve_app_name(app_name)
        
        try:
            result = await run_applescript(
                AppleScriptTemplates.get_window_list(resolved_name)
            )
            # Parse AppleScript list format
            if result:
                return [w.strip() for w in result.split(",")]
            return []
        except Exception:
            return []
    
    async def click_menu(self, app_name: str, menu: str, item: str) -> dict:
        """Click a menu item in an application"""
        resolved_name = self._resolve_app_name(app_name)
        
        try:
            await run_applescript(
                AppleScriptTemplates.click_menu_item(resolved_name, menu, item)
            )
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def is_running(self, app_name: str) -> bool:
        """Check if an application is running"""
        resolved_name = self._resolve_app_name(app_name)
        
        script = f'''
        tell application "System Events"
            set isRunning to (name of processes) contains "{resolved_name}"
        end tell
        return isRunning
        '''
        
        try:
            result = await run_applescript(script)
            return result.lower() == "true"
        except Exception:
            return False
    
    async def focus_window(self, app_name: str, window_title: str) -> dict:
        """Focus a specific window of an application"""
        resolved_name = self._resolve_app_name(app_name)
        
        script = f'''
        tell application "{resolved_name}"
            activate
            set index of window "{window_title}" to 1
        end tell
        '''
        
        try:
            await run_applescript(script)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
