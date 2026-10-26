"""HTTPS helpers shared by the modules that fetch from the internet."""

from __future__ import annotations

import functools
import os
import ssl

SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """The default context, plus the system CA bundle when there is one.

    python.org builds on macOS have no CA bundle until "Install Certificates" is run, so every
    https request fails with CERTIFICATE_VERIFY_FAILED; the system bundle covers that (harmless
    where the default store already works).
    """
    ctx = ssl.create_default_context()
    if os.path.exists(SYSTEM_CA_BUNDLE):
        try:
            ctx.load_verify_locations(SYSTEM_CA_BUNDLE)
        except ssl.SSLError:
            pass
    return ctx
