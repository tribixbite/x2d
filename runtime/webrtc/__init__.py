"""aiortc-based WebRTC server: pulls JPEG frames from the camera daemon
and re-encodes them as a live video track over WebRTC. Sub-second
end-to-end latency vs HLS's 6-8s.
"""
