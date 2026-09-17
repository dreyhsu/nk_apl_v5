#!/usr/bin/env python3
"""Production WSGI entry point for the PC multi-stream edition."""
import argparse
import os
from waitress import serve
import server


def main():
    parser = argparse.ArgumentParser(description='TM-Inspect PC production server')
    parser.add_argument('--host', default=os.environ.get('TM_INSPECT_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('TM_INSPECT_PORT', '5000')))
    parser.add_argument('--threads', type=int, default=int(os.environ.get('TM_WSGI_THREADS', '16')))
    args = parser.parse_args()
    server.STREAM_MANAGER.reload()
    print(f'TM-Inspect PC running on http://{args.host}:{args.port} (Waitress, threads={args.threads})')
    serve(
        server.app,
        host=args.host,
        port=args.port,
        threads=max(4, args.threads),
        channel_timeout=120,
        cleanup_interval=30,
    )


if __name__ == '__main__':
    main()
