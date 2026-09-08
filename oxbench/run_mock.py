"""Run the oxbench mock server standalone.

Usage:
    python oxbench/run_mock.py --port 5080 --verbose

This starts the mock server in the foreground so you can interact with it manually.
"""
import argparse
from mock_openai_server import start_mock_server


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=5080)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args()
    if args.verbose:
        import os
        os.environ['MOCK_VERBOSE'] = '1'
    srv, thr = start_mock_server(port=args.port, host=args.host)
    print(f'Mock server running on http://{args.host}:{args.port}/v1 (Ctrl-C to stop)')
    try:
        while True:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        print('Shutting down')
        srv.shutdown()
        srv.server_close()


if __name__ == '__main__':
    main()
