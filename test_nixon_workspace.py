#!/usr/bin/env python3
"""
Test server for NIXON Workspace Dashboard
Simple HTTP server to test the workspace functionality
"""

import http.server
import socketserver
import os
import sys
from pathlib import Path

class WorkspaceHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / "static"), **kwargs)
    
    def end_headers(self):
        # Add CORS headers for development
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        super().end_headers()

def main():
    port = 8081
    
    print(f"Starting NIXON Workspace test server on port {port}")
    print(f"Open your browser to: http://localhost:{port}/nixon-workspace.html")
    print("Press Ctrl+C to stop the server")
    
    try:
        with socketserver.TCPServer(("", port), WorkspaceHTTPRequestHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
    except OSError as e:
        if e.errno == 48:  # Address already in use
            print(f"Port {port} is already in use. Try a different port.")
            sys.exit(1)
        else:
            raise

if __name__ == "__main__":
    main()