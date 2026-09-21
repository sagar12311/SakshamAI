"""
Saksham AI - AppleScript Bridge
Execute AppleScript commands from Python securely.
"""

import asyncio
import subprocess
from typing import Optional

from loguru import logger


async def run_applescript(script: str, timeout: float = 30.0) -> str:
    """
    Execute an AppleScript and return the result.
    
    Args:
        script: The AppleScript code to execute
        timeout: Maximum execution time in seconds
        
    Returns:
        The output of the script
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout
        )
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"AppleScript error: {error_msg}")
            raise RuntimeError(f"AppleScript failed: {error_msg}")
        
        return stdout.decode().strip()
        
    except asyncio.TimeoutError:
        process.kill()
        raise TimeoutError(f"AppleScript timed out after {timeout}s")


async def run_applescript_file(file_path: str, args: Optional[list[str]] = None) -> str:
    """
    Execute an AppleScript file.
    
    Args:
        file_path: Path to the .scpt or .applescript file
        args: Optional arguments to pass to the script
        
    Returns:
        The output of the script
    """
    cmd = ["osascript", file_path]
    if args:
        cmd.extend(args)
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        raise RuntimeError(f"AppleScript failed: {stderr.decode()}")
    
    return stdout.decode().strip()


class AppleScriptTemplates:
    """
    Common AppleScript templates for Saksham operations.
    """
    
    @staticmethod
    def activate_app(app_name: str) -> str:
        return f'tell application "{app_name}" to activate'
    
    @staticmethod
    def quit_app(app_name: str) -> str:
        return f'tell application "{app_name}" to quit'
    
    @staticmethod
    def get_frontmost_app() -> str:
        return '''
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
        end tell
        return frontApp
        '''
    
    @staticmethod
    def open_url(url: str, browser: str = "Safari") -> str:
        return f'''
        tell application "{browser}"
            activate
            open location "{url}"
        end tell
        '''
    
    @staticmethod
    def type_text(text: str) -> str:
        # Escape quotes in text
        escaped_text = text.replace('"', '\\"')
        return f'''
        tell application "System Events"
            keystroke "{escaped_text}"
        end tell
        '''
    
    @staticmethod
    def press_key(key: str, modifiers: Optional[list[str]] = None) -> str:
        modifier_str = ""
        if modifiers:
            modifier_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
        
        return f'''
        tell application "System Events"
            key code {key}{modifier_str}
        end tell
        '''
    
    @staticmethod
    def click_menu_item(app_name: str, menu: str, item: str) -> str:
        return f'''
        tell application "System Events"
            tell process "{app_name}"
                click menu item "{item}" of menu "{menu}" of menu bar 1
            end tell
        end tell
        '''
    
    @staticmethod
    def get_window_list(app_name: str) -> str:
        return f'''
        tell application "{app_name}"
            set windowList to name of every window
        end tell
        return windowList
        '''
    
    @staticmethod
    def display_notification(title: str, message: str) -> str:
        return f'display notification "{message}" with title "{title}"'
    
    @staticmethod
    def display_dialog(message: str, title: str = "Saksham AI") -> str:
        return f'display dialog "{message}" with title "{title}"'
    
    @staticmethod
    def get_clipboard() -> str:
        return 'the clipboard'
    
    @staticmethod
    def set_clipboard(text: str) -> str:
        escaped = text.replace('"', '\\"')
        return f'set the clipboard to "{escaped}"'
    
    @staticmethod
    def open_file(file_path: str, app_name: Optional[str] = None) -> str:
        if app_name:
            return f'''
            tell application "{app_name}"
                activate
                open POSIX file "{file_path}"
            end tell
            '''
        return f'tell application "Finder" to open POSIX file "{file_path}"'
    
    @staticmethod
    def reveal_in_finder(file_path: str) -> str:
        return f'''
        tell application "Finder"
            reveal POSIX file "{file_path}"
            activate
        end tell
        '''


# Convenience functions

async def activate_app(app_name: str) -> bool:
    """Activate (bring to front) an application"""
    try:
        await run_applescript(AppleScriptTemplates.activate_app(app_name))
        return True
    except Exception as e:
        logger.error(f"Failed to activate {app_name}: {e}")
        return False


async def quit_app(app_name: str) -> bool:
    """Quit an application"""
    try:
        await run_applescript(AppleScriptTemplates.quit_app(app_name))
        return True
    except Exception as e:
        logger.error(f"Failed to quit {app_name}: {e}")
        return False


async def get_frontmost_app() -> str:
    """Get the name of the frontmost application"""
    return await run_applescript(AppleScriptTemplates.get_frontmost_app())


async def open_url(url: str, browser: str = "Safari") -> bool:
    """Open a URL in the specified browser"""
    try:
        await run_applescript(AppleScriptTemplates.open_url(url, browser))
        return True
    except Exception as e:
        logger.error(f"Failed to open URL: {e}")
        return False


async def type_text(text: str) -> bool:
    """Type text using keyboard simulation"""
    try:
        await run_applescript(AppleScriptTemplates.type_text(text))
        return True
    except Exception as e:
        logger.error(f"Failed to type text: {e}")
        return False


async def notify(title: str, message: str) -> None:
    """Display a macOS notification"""
    await run_applescript(AppleScriptTemplates.display_notification(title, message))
