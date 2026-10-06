"""HTTP sessions that verify TLS against the operating system's certificate store.

Python's bundled `certifi` list can be outdated, and antivirus / corporate proxies install their root certificate
into the OS store only. Either one makes requests fail with CERTIFICATE_VERIFY_FAILED
("unable to get local issuer certificate"). `truststore` asks Windows/macOS/Linux itself, which fixes both.
Verification is never disabled; without `truststore` the normal `requests` behaviour applies.
"""
import ssl
import requests
from requests.adapters import HTTPAdapter

try:
    import truststore
except ImportError:  # optional: fall back to certifi
    truststore = None


class _SystemTrustAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return super().proxy_manager_for(proxy, **kwargs)


def trust_source() -> str:
    return "OS certificate store (truststore)" if truststore is not None else "certifi bundle"


def make_session() -> requests.Session:
    session = requests.Session()
    if truststore is not None:
        session.mount("https://", _SystemTrustAdapter())
    return session
