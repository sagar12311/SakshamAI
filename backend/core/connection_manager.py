"""
Saksham AI - WebSocket Connection Manager
Manages multiple WebSocket connections for real-time communication
"""

import asyncio

from typing import Optional
from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    """
    Manages WebSocket connections for Saksham AI.
    Supports multiple channels (chat, voice, etc.)
    """
    
    def __init__(self):
        self._active_connections: dict[str, list[WebSocket]] = {
            "default": [],
            "voice": [],
        }
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
    
    @property
    def active_connections_count(self) -> int:
        return sum(len(conns) for conns in self._active_connections.values())
    
    async def connect(self, websocket: WebSocket, channel: str = "default") -> None:
        """Accept and register a new WebSocket connection"""
        await websocket.accept()
        
        if channel not in self._active_connections:
            self._active_connections[channel] = []
        
        self._active_connections[channel].append(websocket)
        self._send_locks[websocket] = asyncio.Lock()
        logger.info(f"WebSocket connected on channel: {channel}")
    
    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from all channels"""
        for channel, connections in self._active_connections.items():
            if websocket in connections:
                connections.remove(websocket)
                logger.info(f"WebSocket disconnected from channel: {channel}")
        self._send_locks.pop(websocket, None)
    
    async def send_personal_message(self, message: str, websocket: WebSocket) -> None:
        """Send a message to a specific WebSocket"""
        async with self._send_locks.setdefault(websocket, asyncio.Lock()):
            await websocket.send_text(message)
    
    async def send_json(self, data: dict, websocket: WebSocket) -> None:
        """Send JSON data to a specific WebSocket"""
        async with self._send_locks.setdefault(websocket, asyncio.Lock()):
            await websocket.send_json(data)
    
    async def broadcast(self, message: str, channel: str = "default") -> None:
        """Broadcast a message to all connections on a channel"""
        connections = self._active_connections.get(channel, [])
        for connection in connections:
            try:
                await self.send_personal_message(message, connection)
            except Exception as e:
                logger.error(f"Error broadcasting: {e}")
    
    async def broadcast_json(self, data: dict, channel: str = "default") -> None:
        """Broadcast JSON to all connections on a channel"""
        connections = self._active_connections.get(channel, [])
        dead_connections = []
        
        for connection in connections:
            try:
                await self.send_json(data, connection)
            except Exception:
                # Connection is dead, mark for removal
                dead_connections.append(connection)
        
        # Clean up dead connections
        for dead in dead_connections:
            self.disconnect(dead)
            logger.debug(f"Removed stale connection from {channel}")
    
    async def broadcast_bytes(self, data: bytes, channel: str = "voice") -> None:
        """Broadcast binary data to all connections on a channel"""
        connections = self._active_connections.get(channel, [])
        dead_connections = []
        
        for connection in connections:
            try:
                async with self._send_locks.setdefault(connection, asyncio.Lock()):
                    await connection.send_bytes(data)
            except Exception:
                # Connection is dead, mark for removal
                dead_connections.append(connection)
        
        # Clean up dead connections
        for dead in dead_connections:
            self.disconnect(dead)
            logger.debug(f"Removed stale connection from {channel}")
