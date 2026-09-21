"""
Saksham AI - Screen Controller
Handles screen capture for Vision capabilities.
"""

import os
import subprocess
import tempfile
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class ScreenController:
    """Controls screen capture operations"""
    
    def __init__(self):
        pass
        
    async def capture_screen(self) -> str:
        """
        Capture the main screen and save to a temporary file.
        Returns the absolute path to the screenshot image.
        """
        try:
            # Create temp file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_dir = tempfile.gettempdir()
            filename = f"saksham_screen_{timestamp}.png"
            filepath = os.path.join(temp_dir, filename)
            
            # Use macOS native screencapture CLI
            # -x: mute sound
            # -m: main monitor only (optional, but good for speed)
            # -C: capture cursor (optional)
            cmd = ["screencapture", "-x", "-m", filepath]
            
            # Run command
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                logger.error(f"Screenshot failed: {stderr.decode()}")
                return ""
            
            if not os.path.exists(filepath):
                logger.error("Screenshot file was not created.")
                return ""
                
            logger.info(f"📸 Screen captured: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"Screen capture error: {e}")
            return ""
