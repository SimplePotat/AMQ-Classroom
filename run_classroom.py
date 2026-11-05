#!/usr/bin/env python3
"""Run the persistent classroom server. No Redis or Node build step required."""
import argparse
import os
import socket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: all LAN interfaces)")
    parser.add_argument("--port", default=8009, type=int)
    parser.add_argument("--advertise", help="Player URL shown on the board, e.g. http://192.168.1.50:8009")
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "classroom.settings")
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from waitress import serve
    call_command("migrate", verbosity=0)
    call_command("check", verbosity=0)
    from classroom.engine import recover_after_restart
    recover_after_restart()
    if args.advertise:
        url = args.advertise.rstrip("/")
    else:
        address = args.host
        if address == "0.0.0.0":
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.connect(("192.0.2.1", 80))
                    address = sock.getsockname()[0]
            except OSError:
                address = "127.0.0.1"
        url = f"http://{address}:{args.port}"
    os.environ["CLASSROOM_JOIN_URL"] = url
    print(f"\n  ANIME QUIZ / CLASSROOM\n  Players:  {url}\n  Host:     http://localhost:{args.port}/host\n  Host key: {settings.HOST_KEY}\n  Data:     {settings.DATA_DIR}\n", flush=True)
    print("  Keep this terminal open. Press Ctrl+C to stop.\n", flush=True)
    serve(get_wsgi_application(), host=args.host, port=args.port, threads=16,
          max_request_body_size=1024 * 1024, clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
