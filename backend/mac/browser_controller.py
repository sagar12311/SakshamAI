"""
Saksham AI - Browser Controller
Control Chrome and Safari for web automation.
"""

from typing import Optional

from loguru import logger

from .applescript_bridge import run_applescript


class BrowserController:
    """
    Controls web browsers for Saksham AI.
    Supports Chrome and Safari.
    """
    
    def __init__(self, default_browser: str = "Google Chrome"):
        self.default_browser = default_browser
    
    async def navigate(
        self,
        url: str,
        browser: Optional[str] = None,
        new_tab: bool = True,
    ) -> dict:
        """
        Navigate to a URL.
        
        Args:
            url: The URL to navigate to
            browser: Browser to use (default: Chrome)
            new_tab: Open in new tab
        """
        browser = browser or self.default_browser
        
        if browser == "Google Chrome":
            script = self._chrome_navigate(url, new_tab)
        elif browser == "Safari":
            script = self._safari_navigate(url, new_tab)
        else:
            return {"success": False, "error": f"Unsupported browser: {browser}"}
        
        try:
            await run_applescript(script)
            return {"success": True, "url": url, "browser": browser}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _escape_applescript_string(value: str) -> str:
        """Encode untrusted text as an AppleScript string literal fragment."""
        return str(value).replace("\\", "\\\\").replace('"', '\\"')
    
    def _chrome_navigate(self, url: str, new_tab: bool) -> str:
        escaped_url = self._escape_applescript_string(url)
        if new_tab:
            return f'''
            tell application "Google Chrome"
                activate
                if (count of windows) = 0 then
                    make new window
                end if
                tell front window
                    make new tab with properties {{URL:"{escaped_url}"}}
                    set active tab index to (count of tabs)
                end tell
            end tell
            '''
        else:
            return f'''
            tell application "Google Chrome"
                activate
                if (count of windows) = 0 then
                    make new window
                end if
                set URL of active tab of front window to "{escaped_url}"
            end tell
            '''
    
    def _safari_navigate(self, url: str, new_tab: bool) -> str:
        if new_tab:
            return f'''
            tell application "Safari"
                activate
                tell front window
                    make new tab with properties {{URL:"{url}"}}
                end tell
            end tell
            '''
        else:
            return f'''
            tell application "Safari"
                activate
                set URL of current tab of front window to "{url}"
            end tell
            '''
    
    async def get_current_url(self, browser: Optional[str] = None) -> dict:
        """Get the URL of the current tab"""
        browser = browser or self.default_browser
        
        if browser == "Google Chrome":
            script = '''
            tell application "Google Chrome"
                return URL of active tab of front window
            end tell
            '''
        elif browser == "Safari":
            script = '''
            tell application "Safari"
                return URL of current tab of front window
            end tell
            '''
        else:
            return {"success": False, "error": f"Unsupported browser: {browser}"}
        
        try:
            url = await run_applescript(script)
            return {"success": True, "url": url}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_active_chrome_tab(self) -> dict:
        """Read the active Chrome tab identity for a code-owned recovery flow."""
        try:
            result = await run_applescript('''
            tell application "Google Chrome"
                if (count of windows) = 0 then error "Chrome has no open windows"
                tell front window
                    set currentTab to active tab
                    return (id of currentTab as text) & "\\n" & (URL of currentTab as text)
                end tell
            end tell
            ''')
            tab_id, url = str(result).split("\n", 1)
            tab_id = tab_id.strip()
            if not tab_id.isdigit():
                return {"success": False, "error": "Chrome did not return a valid active tab id"}
            return {"success": True, "tab_id": tab_id, "url": url.strip()}
        except Exception as error:
            return {"success": False, "error": str(error)}
    
    async def get_page_title(self, browser: Optional[str] = None) -> dict:
        """Get the title of the current page"""
        browser = browser or self.default_browser
        
        if browser == "Google Chrome":
            script = '''
            tell application "Google Chrome"
                return title of active tab of front window
            end tell
            '''
        elif browser == "Safari":
            script = '''
            tell application "Safari"
                return name of current tab of front window
            end tell
            '''
        else:
            return {"success": False, "error": f"Unsupported browser: {browser}"}
        
        try:
            title = await run_applescript(script)
            return {"success": True, "title": title}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def get_tabs(self, browser: Optional[str] = None) -> dict:
        """Get list of open tabs"""
        browser = browser or self.default_browser
        
        if browser == "Google Chrome":
            script = '''
            tell application "Google Chrome"
                set tabList to {}
                repeat with w in windows
                    repeat with t in tabs of w
                        set end of tabList to {title of t, URL of t}
                    end repeat
                end repeat
                return tabList
            end tell
            '''
        elif browser == "Safari":
            script = '''
            tell application "Safari"
                set tabList to {}
                repeat with w in windows
                    repeat with t in tabs of w
                        set end of tabList to {name of t, URL of t}
                    end repeat
                end repeat
                return tabList
            end tell
            '''
        else:
            return {"success": False, "error": f"Unsupported browser: {browser}"}
        
        try:
            result = await run_applescript(script)
            # Parse the AppleScript list format
            return {"success": True, "tabs": result}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def close_tab(self, browser: Optional[str] = None) -> dict:
        """Close the current tab"""
        browser = browser or self.default_browser
        
        if browser == "Google Chrome":
            script = '''
            tell application "Google Chrome"
                close active tab of front window
            end tell
            '''
        elif browser == "Safari":
            script = '''
            tell application "Safari"
                close current tab of front window
            end tell
            '''
        else:
            return {"success": False, "error": f"Unsupported browser: {browser}"}
        
        try:
            await run_applescript(script)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def execute_javascript(
        self,
        script: str,
        browser: Optional[str] = None,
    ) -> dict:
        """
        Execute JavaScript in the current page.
        Chrome only for security reasons.
        """
        browser = browser or self.default_browser
        
        if browser != "Google Chrome":
            return {"success": False, "error": "JavaScript execution only supported in Chrome"}
        
        # AppleScript strings use backslash escaping for backslashes and double
        # quotes. Apostrophes are ordinary characters and must remain untouched.
        escaped_script = script.replace("\\", "\\\\").replace('"', '\\"')
        
        applescript = f'''
        tell application "Google Chrome"
            execute active tab of front window javascript "{escaped_script}"
        end tell
        '''
        
        try:
            result = await run_applescript(applescript)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def open_tracked_chrome_tab(self, url: str) -> dict:
        """Open a Chrome tab and return its immutable Chrome tab id.

        This is intentionally separate from generic navigation.  Code-owned
        commerce adapters can pin all subsequent reads and actions to this id
        so a different active tab cannot be substituted between operations.
        """
        escaped_url = self._escape_applescript_string(url)
        applescript = f'''
        tell application "Google Chrome"
            activate
            if (count of windows) = 0 then
                make new window
            end if
            tell front window
                make new tab with properties {{URL:"{escaped_url}"}}
                set active tab index to (count of tabs)
                return id of active tab as text
            end tell
        end tell
        '''
        try:
            tab_id = str(await run_applescript(applescript)).strip()
            if not tab_id.isdigit():
                return {"success": False, "error": "Chrome did not return a valid tab id"}
            return {"success": True, "tab_id": tab_id, "url": url, "browser": "Google Chrome"}
        except Exception as error:
            return {"success": False, "error": str(error)}

    @staticmethod
    def _validated_chrome_tab_id(tab_id: object) -> Optional[str]:
        value = str(tab_id).strip()
        return value if value.isdigit() and int(value) > 0 else None

    def _chrome_tab_lookup_script(self, tab_id: object, body: str) -> str:
        """Build a fixed Chrome tab lookup; ``tab_id`` is numeric only."""
        safe_id = self._validated_chrome_tab_id(tab_id)
        if safe_id is None:
            raise ValueError("Chrome tab id must be a positive integer")
        return f'''
        tell application "Google Chrome"
            set targetTab to missing value
            repeat with targetWindow in windows
                repeat with candidateTab in tabs of targetWindow
                    if (id of candidateTab as text) is "{safe_id}" then
                        set targetTab to candidateTab
                        exit repeat
                    end if
                end repeat
                if targetTab is not missing value then exit repeat
            end repeat
            if targetTab is missing value then error "Tracked Chrome tab is no longer available"
            {body}
        end tell
        '''

    async def get_chrome_tab_url(self, tab_id: object) -> dict:
        """Read the URL for a specific Chrome tab without using the active tab."""
        try:
            applescript = self._chrome_tab_lookup_script(tab_id, "return URL of targetTab")
            url = str(await run_applescript(applescript)).strip()
            return {"success": True, "tab_id": str(tab_id), "url": url}
        except Exception as error:
            return {"success": False, "error": str(error)}

    async def activate_tracked_chrome_tab(self, tab_id: object) -> dict:
        """Bring the pinned Chrome tab to the foreground without navigating it."""
        try:
            safe_id = self._validated_chrome_tab_id(tab_id)
            if safe_id is None:
                raise ValueError("Chrome tab id must be a positive integer")
            # AppleScript's `index of targetTab` remains an object reference
            # when targetTab came from a repeat loop, which Chrome rejects
            # with -10006.  Track an ordinary integer while locating the
            # immutable tab ID, then set that window's active tab index.
            applescript = f'''
            tell application "Google Chrome"
                set targetWindow to missing value
                set targetIndex to 0
                repeat with candidateWindow in windows
                    set candidateIndex to 0
                    repeat with candidateTab in tabs of candidateWindow
                        set candidateIndex to candidateIndex + 1
                        if (id of candidateTab as text) is "{safe_id}" then
                            set targetWindow to candidateWindow
                            set targetIndex to candidateIndex
                            exit repeat
                        end if
                    end repeat
                    if targetWindow is not missing value then exit repeat
                end repeat
                if targetWindow is missing value then error "Tracked Chrome tab is no longer available"
                set active tab index of targetWindow to targetIndex
                activate
                return id of active tab of targetWindow as text
            end tell
            '''
            active_id = str(await run_applescript(applescript)).strip()
            if active_id != str(tab_id):
                return {"success": False, "error": "Chrome did not activate the tracked tab"}
            return {"success": True, "tab_id": str(tab_id)}
        except Exception as error:
            return {"success": False, "error": str(error)}

    async def execute_javascript_in_chrome_tab(self, tab_id: object, script: str) -> dict:
        """Execute a fixed, code-owned script in a specific tracked Chrome tab."""
        try:
            escaped_script = self._escape_applescript_string(script)
            applescript = self._chrome_tab_lookup_script(
                tab_id,
                f'execute targetTab javascript "{escaped_script}"',
            )
            result = await run_applescript(applescript)
            return {"success": True, "tab_id": str(tab_id), "result": result}
        except Exception as error:
            return {"success": False, "error": str(error)}

    async def scroll_chrome_tab(self, tab_id: object, direction: str = "down") -> dict:
        """Scroll a pinned Chrome tab using one of four allowlisted motions."""
        scripts = {
            "down": "window.scrollBy(0, Math.max(500, Math.floor(window.innerHeight * 0.8))); 'scrolled'",
            "up": "window.scrollBy(0, -Math.max(500, Math.floor(window.innerHeight * 0.8))); 'scrolled'",
            "top": "window.scrollTo(0, 0); 'scrolled'",
            "bottom": "window.scrollTo(0, document.body.scrollHeight); 'scrolled'",
        }
        if direction not in scripts:
            return {"success": False, "error": "Unknown scroll direction"}
        return await self.execute_javascript_in_chrome_tab(tab_id, scripts[direction])
    
    async def interact(
        self,
        selector: str,
        action: str = "click",
        value: Optional[str] = None,
        browser: Optional[str] = None,
    ) -> dict:
        """
        Interact with a page element using JavaScript.
        
        Args:
            selector: CSS selector for the element
            action: click, type, focus, getValue
            value: Value to type (for type action)
        """
        if action == "click":
            js = f"document.querySelector('{selector}').click()"
        elif action == "type" and value:
            js = f"document.querySelector('{selector}').value = '{value}'"
        elif action == "focus":
            js = f"document.querySelector('{selector}').focus()"
        elif action == "getValue":
            js = f"document.querySelector('{selector}').value"
        elif action == "getText":
            js = f"document.querySelector('{selector}').textContent"
        else:
            return {"success": False, "error": f"Unknown action: {action}"}
        
        return await self.execute_javascript(js, browser)
    
    async def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
        browser: Optional[str] = None,
    ) -> dict:
        """Scroll the page"""
        if direction == "down":
            js = f"window.scrollBy(0, {amount})"
        elif direction == "up":
            js = f"window.scrollBy(0, -{amount})"
        elif direction == "top":
            js = "window.scrollTo(0, 0)"
        elif direction == "bottom":
            js = "window.scrollTo(0, document.body.scrollHeight)"
        else:
            return {"success": False, "error": f"Unknown direction: {direction}"}
        
        return await self.execute_javascript(js, browser)
