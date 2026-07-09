""" Web UI bridge package for the WeatherFlow PiConsole.

This package adds a modern, browser-based front-end to the console WITHOUT
modifying the existing data pipeline. It reads the live observation state that
the console already maintains and streams it to any connected browser over a
WebSocket. See server/bridge.py for details.
"""

from server.bridge import start_web_bridge  # noqa: F401
