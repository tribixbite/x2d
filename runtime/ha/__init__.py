"""Home Assistant MQTT auto-discovery bridge for the X2D (#50).

Reads live state from a running ``x2d_bridge.py daemon --http`` via its
SSE feed, transforms it into Home Assistant's MQTT-discovery payload
shape, and publishes both the discovery configs and the per-entity
state to a user-supplied MQTT broker. Subscribes to HA-side command
topics (light on/off, pause/resume/stop, AMS slot load, heat presets)
and forwards them to the bridge's ``POST /control/<verb>`` HTTP route.

Spec reference: https://www.home-assistant.io/integrations/mqtt/#discovery
"""
