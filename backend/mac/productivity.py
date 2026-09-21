"""
Saksham AI - Productivity Controller
Handles interaction with Mac Reminders, Mail, and internal Timers.
"""

import asyncio
import logging
from typing import Optional
from mac.applescript_bridge import run_applescript

logger = logging.getLogger(__name__)

class ProductivityController:
    """Controls Mac productivity apps and timers"""
    
    def __init__(self):
        self._active_timers = {}
        self._speech_process: Optional[asyncio.subprocess.Process] = None
    
    async def list_running_apps(self) -> list[str]:
        """List all visible running applications"""
        try:
            script = '''
            tell application "System Events"
                set appList to name of every process where background only is false
            end tell
            return appList
            '''
            result = await run_applescript(script)
            # Result is comma separated string
            apps = [a.strip() for a in result.split(',')]
            logger.info(f"📱 Running apps: {len(apps)}")
            return apps
        except Exception as e:
            logger.error(f"Failed to list apps: {e}")
            return []

    async def switch_to_app(self, app_name: str) -> str:
        """Switch focus to a specific app"""
        try:
            # Fuzzy match? For now direct activate
            # System events can identify running apps more reliably
            script = f'''
            tell application "{app_name}" to activate
            '''
            await run_applescript(script)
            
            # self.speak_system(f"Switching to {app_name}")
            return f"Switched to {app_name}"
        except Exception as e:
            logger.error(f"Failed to switch to {app_name}: {e}")
            return f"Failed to switch to {app_name}"

    async def open_app(self, app_name: str) -> str:
        """Open a Mac application"""
        try:
            # Clean up app name (e.g. "Spotify app" -> "Spotify")
            app = app_name.replace(" app", "").replace(" application", "")
            
            script = f'tell application "{app}" to activate'
            await run_applescript(script)
            
            # self.speak_system(f"Opening {app}")
            logger.info(f"🚀 Opened app: {app}")
            return f"Opened {app}"
        except Exception as e:
            logger.error(f"Failed to open {app_name}: {e}")
            return f"Failed to open {app_name}"

    async def speak_system(self, text: str):
        """Speak using Mac system TTS (wait for completion)"""
        process: Optional[asyncio.subprocess.Process] = None
        try:
            process = await asyncio.create_subprocess_exec(
                "say", "-v", "Rishi", text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            self._speech_process = process
            await process.wait()
        except Exception as e:
            logger.error(f"System TTS failed: {e}")
        finally:
            if process is not None and self._speech_process is process:
                self._speech_process = None

    async def stop_speaking(self) -> None:
        """Stop an active macOS system voice process, if one is running."""
        process = self._speech_process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self._speech_process = None

    async def create_reminder(self, title: str, notes: str = "", list_name: str = "Reminders") -> str:
        """Create a new reminder in the Reminders app"""
        try:
            # AppleScript to create reminder
            # Handles default list if list_name not found? 
            # We'll try specific list, if fails, fallback to default?
            # Simple version: just create in default list if list_name is generic.
            
            script = f'''
            tell application "Reminders"
                if not (exists list "{list_name}") then
                    set list_name to name of default list
                else
                    set list_name to "{list_name}"
                end if
                
                tell list list_name
                    make new reminder with properties {{name:"{title}", body:"{notes}"}}
                end tell
            end tell
            '''
            await run_applescript(script)
            
            # self.speak_system(f"Added to reminders: {title}")
            logger.info(f"✅ Created reminder: {title}")
            return f"Reminder '{title}' created successfully."
            
        except Exception as e:
            logger.error(f"Failed to create reminder: {e}")
            return f"Failed to create reminder: {e}"

    async def find_contacts_applescript(self, name_query: str) -> list[dict]:
        """Search macOS Contacts for a name (fuzzy match)"""
        try:
            # AppleScript to find people with matching name
            # Returns list of "Name|ID" strings
            script = f'''
            tell application "Contacts"
                set matchingPeople to every person whose name contains "{name_query}"
                set results to {{}}
                repeat with p in matchingPeople
                    set pName to name of p
                    set pId to id of p
                    set end of results to (pName & "|" & pId)
                end repeat
                return results
            end tell
            '''
            result = await run_applescript(script)
            
            if not result:
                return []
                
            contacts = []
            # Result is comma separated list of "Name|ID"
            # Need to be careful about parsing if names have commas.
            # AppleScript list output usually separates by ", ".
            # A safer way might be newline separation in script, but let's try standard split first.
            # actually run_applescript bridge handles lists? likely returns string.
            
            # Let's assume comma separated for now. 
            items = result.split(', ')
            for item in items:
                if '|' in item:
                    name, cid = item.split('|', 1)
                    contacts.append({"name": name.strip(), "id": cid.strip()})
            
            return contacts
        except Exception as e:
            logger.error(f"Contact search failed: {e}")
            return []

    async def make_call(self, contact: str, confirmed: bool = False) -> str:
        """Initiate a FaceTime call with contact validation"""
        try:
            target = contact.strip()
            
            # 1. Allow direct phone numbers without validation
            import re
            is_phone = re.match(r'^[0-9\+\-\(\)\s]+$', target) and len(re.sub(r'[^0-9]', '', target)) >= 7
            if is_phone:
                 return await self._execute_call_url(f"tel://{target}", "Phone", target)
            
            # 2. If already confirmed by user, proceed immediately
            # (Assuming the 'contact' passed is the correct name)
            if confirmed:
                return await self._execute_call_url(f"facetime://{target}", "FaceTime", target)
                
            # 3. Search for contact validation
            # self.speak_system(f"Looking for {target} in contacts...")
            matches = await self.find_contacts_applescript(target)
            
            if not matches:
                # No match found
                return f"I couldn't find anyone named '{target}' in your contacts. Did you mean someone else?"
                
            if len(matches) == 1:
                # Exact single match
                match_name = matches[0]["name"]
                # Heuristic: if very close match, maybe just call?
                # User asked for: "Did you mean X?" flow.
                # If exact string match, auto-call?
                if match_name.lower() == target.lower():
                     return await self._execute_call_url(f"facetime://{match_name}", "FaceTime", match_name)
                     
                # Otherwise ask for confirmation
                return f"Found contact: {match_name}. Did you mean {match_name}?"
                
            # Multiple matches
            names = [m["name"] for m in matches[:3]] # limit to 3
            return f"I found multiple contacts: {', '.join(names)}. Which one did you mean?"

        except Exception as e:
            logger.error(f"Failed to call {contact}: {e}")
            return f"Failed to call {contact}"

    async def _execute_call_url(self, url: str, method: str, name: str) -> str:
        """Helper to actually open the URL"""
        import subprocess
        subprocess.Popen(["open", url])
        # self.speak_system(f"Calling {name} via {method}")
        logger.info(f"📞 Initiating call to: {name}")
        return f"Calling {name}..."

    async def send_email(self, to_email: str, subject: str, body: str) -> str:
        """Draft and send an email using Mail.app"""
        try:
            # We will create a draft and open it for review (safer than auto-send)
            script = f'''
            tell application "Mail"
                activate
                set newMessage to make new outgoing message with properties {{subject:"{subject}", content:"{body}", visible:true}}
                tell newMessage
                    make new to recipient at end of to recipients with properties {{address:"{to_email}"}}
                end tell
            end tell
            '''
            await run_applescript(script)
            
            # self.speak_system(f"Drafting email to {to_email}")
            logger.info(f"📧 Drafted email to: {to_email}")
            return f"Email to {to_email} has been drafted and opened for your review."
            
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return f"Failed to draft email: {e}"

    async def set_timer(self, seconds: int, label: str = "Timer", outcome_callback=None) -> str:
        """Set a background timer"""
        try:
            # Validate input
            if seconds <= 0:
                return "Time must be positive."
                
            task = asyncio.create_task(self._run_timer(seconds, label, outcome_callback))
            self._active_timers[label] = task
            
            duration_str = f"{seconds} seconds" if seconds < 60 else f"{seconds // 60} minutes"
            
            # Immediate visual feedback (in case audio fails)
            await run_applescript(f'display notification "Count down: {duration_str}" with title "Timer Started" subtitle "{label}"')
            
            # Immediate Audio feedback (System-level)
            # self.speak_system(f"Timer set for {duration_str}")
            
            logger.info(f"⏳ Timer set: {duration_str} ({label})")
            return f"Timer set for {duration_str}."
            
        except Exception as e:
            logger.error(f"Failed to set timer: {e}")
            return f"Failed to set timer: {e}"

    async def _run_timer(self, seconds: int, label: str, callback):
        """Background timer execution"""
        try:
            await asyncio.sleep(seconds)
            
            logger.info(f"🔔 Timer finished: {label}")
            
            # Trigger alert
            await self._trigger_alert(label)
            
            if callback:
                await callback(f"Timer ended: {label}")
                
        except asyncio.CancelledError:
            logger.info(f"Timer cancelled: {label}")
        finally:
            if label in self._active_timers:
                del self._active_timers[label]

    async def _trigger_alert(self, label: str):
        """Visual/Audible alert on Mac"""
        # Display notification
        script = f'''
        display notification "Time is up!" with title "{label}" sound name "Glass"
        '''
        await self.speak_system(f"Timer finished for {label}")
        await run_applescript(script)
        
        # We could also use 'say' command via terminal for voice alert if needed
        # but notification is standard.
