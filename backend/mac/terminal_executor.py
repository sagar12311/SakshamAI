"""
Saksham AI - Terminal Executor
Run terminal commands safely with output capture.
"""

import asyncio
import os
from pathlib import Path
from typing import Optional, Any

from loguru import logger


class TerminalExecutor:
    """
    Executes terminal commands for Saksham AI.
    Includes safety checks and output streaming.
    """
    
    # Commands that require extra caution
    DANGEROUS_COMMANDS = {
        "rm -rf",
        "sudo rm",
        "dd",
        "mkfs",
        "> /dev/",
        "chmod 777",
    }
    
    def __init__(self, default_cwd: Optional[str] = None):
        self.default_cwd = default_cwd or str(Path.home())
    
    def _is_dangerous(self, command: str) -> bool:
        """Check if command is potentially dangerous"""
        command_lower = command.lower()
        return any(danger in command_lower for danger in self.DANGEROUS_COMMANDS)
    
    async def execute(
        self,
        command: str,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        timeout: float = 60.0,
        stream_output: bool = False,
        **kwargs: Any,
    ) -> dict:
        """
        Execute a terminal command.
        
        Args:
            command: The command to execute
            cwd: Working directory (default: user home)
            env: Additional environment variables
            timeout: Maximum execution time
            stream_output: If True, return output incrementally
            
        Returns:
            Dict with stdout, stderr, return_code
        """
        working_dir = cwd or self.default_cwd
        
        # Check for dangerous commands
        if self._is_dangerous(command):
            logger.warning(f"Dangerous command detected: {command}")
            return {
                "success": False,
                "error": "Command flagged as potentially dangerous",
                "command": command,
                "requires_confirmation": True,
            }
        
        # Merge environment
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        
        try:
            logger.info(f"Executing: {command} in {working_dir}")
            
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=full_env,
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
            
            return {
                "success": process.returncode == 0,
                "command": command,
                "cwd": working_dir,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else "",
                "return_code": process.returncode,
            }
            
        except asyncio.TimeoutError:
            process.kill()
            return {
                "success": False,
                "error": f"Command timed out after {timeout}s",
                "command": command,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "command": command,
            }
    
    async def execute_in_terminal_app(
        self,
        command: str,
        terminal: str = "Terminal",
    ) -> dict:
        """
        Execute command in a visible Terminal window.
        Useful for interactive commands.
        """
        from .applescript_bridge import run_applescript
        
        # Escape the command for AppleScript
        escaped_command = command.replace('"', '\\"')
        
        if terminal == "iTerm":
            script = f'''
            tell application "iTerm"
                activate
                tell current window
                    create tab with default profile
                    tell current session
                        write text "{escaped_command}"
                    end tell
                end tell
            end tell
            '''
        else:
            script = f'''
            tell application "Terminal"
                activate
                do script "{escaped_command}"
            end tell
            '''
        
        try:
            await run_applescript(script)
            return {"success": True, "terminal": terminal}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def execute_git(
        self,
        git_command: str,
        repo_path: str,
    ) -> dict:
        """
        Execute a git command in a repository.
        """
        full_command = f"git {git_command}"
        return await self.execute(full_command, cwd=repo_path)
    
    async def execute_npm(
        self,
        npm_command: str,
        project_path: str,
    ) -> dict:
        """
        Execute an npm command in a project.
        """
        full_command = f"npm {npm_command}"
        return await self.execute(full_command, cwd=project_path)
    
    async def execute_python(
        self,
        script: str,
        cwd: Optional[str] = None,
    ) -> dict:
        """
        Execute a Python script or command.
        """
        # Use python3 explicitly on Mac
        command = f"python3 -c '{script}'"
        return await self.execute(command, cwd=cwd)
