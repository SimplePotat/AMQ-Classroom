"""LAN request boundaries and short-lived host authorization."""
import hashlib
import ipaddress
import secrets
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponseBadRequest, JsonResponse
from django.http.request import validate_host


def is_host(request):
    grant = request.session.get("host_grant")
    if not isinstance(grant, dict):
        return False
    fingerprint = hashlib.sha256(settings.HOST_KEY.encode()).hexdigest()
    return (isinstance(grant.get("key"), str)
            and secrets.compare_digest(grant["key"], fingerprint)
            and isinstance(grant.get("expires"), (int, float))
            and time.time() < grant["expires"])


def grant_host(request):
    request.session["host_grant"] = {
        "key": hashlib.sha256(settings.HOST_KEY.encode()).hexdigest(),
        "expires": time.time() + 12 * 3600,
    }


class LANBoundaryMiddleware:
    """Literal LAN IPs work without a hostname wildcard vulnerable to rebinding."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Always force Django's own syntax/configured-host validation, even on assets.
        host = urlsplit("http://" + request.get_host()).hostname
        if "*" in settings.ALLOWED_HOSTS:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                named_hosts = [name for name in settings.ALLOWED_HOSTS if name != "*"]
                if host != "localhost" and not validate_host(host, named_hosts):
                    return HttpResponseBadRequest("Use the server's IP address or an explicitly allowed hostname.")
        if request.path.startswith("/api/"):
            length = request.META.get("CONTENT_LENGTH", "0")
            try:
                oversized = int(length or 0) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE
            except ValueError:
                return HttpResponseBadRequest("Invalid content length.")
            if oversized:
                response = JsonResponse({"error": "Request body is too large."}, status=413)
                response["Cache-Control"] = "no-store"
                return response
        return self.get_response(request)
