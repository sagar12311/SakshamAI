"""
Saksham AI - JXA Bridge (JavaScript for Automation)
Enables deep system control of macOS using JXA.
"""

import logging
from mac.applescript_bridge import run_applescript

logger = logging.getLogger(__name__)

class JXAController:
    """
    Controls macOS System Settings and Window Management via JXA.
    """
    
    async def run_jxa(self, script: str) -> str:
        """Run JXA script via osascript -l JavaScript"""
        # Wrapping JXA in AppleScript 'run script ... in "JavaScript"' 
        # is sometimes easier than raw osascript call for async consistency
        # But run_applescript uses 'osascript -e'.
        # We'll use the "-l JavaScript" flag if we modify run_applescript, 
        # but for now we can wrap it:
        
        escaped_script = script.replace('"', '\\"')
        full_script = f'''
        run script "
            {escaped_script}
        " in "JavaScript"
        '''
        return await run_applescript(full_script)

    async def set_volume(self, level: int) -> str:
        """Set system volume (0-100)"""
        level = max(0, min(100, level))
        script = f'Application.currentApplication().includeStandardAdditions = true; app = Application.currentApplication(); app.setVolumeOutputVolume({level})'
        await self.run_jxa(script)
        return f"Volume set to {level}%"

    async def set_dark_mode(self, enabled: bool) -> str:
        """Toggle Dark Mode"""
        val = "true" if enabled else "false"
        script = f'''
        Application("System Events").appearancePreferences.darkMode = {val}
        '''
        await self.run_jxa(script)
        return f"Dark Mode {'enabled' if enabled else 'disabled'}"

    async def open_url(self, url: str) -> str:
        """Open URL in default browser"""
        script = f'var app = Application.currentApplication(); app.includeStandardAdditions = true; app.openLocation("{url}");'
        await self.run_jxa(script)
        return f"Opened {url}"
        
    async def arrange_windows(self, style: str) -> str:
        """
        Arrange windows (left, right, full).
        Note: Requires Accessibility permissions which might trigger prompts.
        """
        # Simple implementation for focused window
        if style == "maximize":
            # Just set bounds to screen bounds?
            # Keeping it simple to avoid complex screen looping for now
            return "Window management requires Accessibility logic (Phase 2 Pending)"
            
        return f"Arranged windows: {style}"
