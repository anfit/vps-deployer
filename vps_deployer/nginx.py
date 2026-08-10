from __future__ import annotations

from .models import Deployment


def nginx_name(deployment: Deployment) -> str:
    if not deployment.http_proxy:
        raise ValueError("deployment has no HTTP proxy")
    return deployment.http_proxy.name


def render_proxy(deployment: Deployment) -> str:
    proxy = deployment.http_proxy
    if not proxy:
        raise ValueError("deployment has no HTTP proxy")
    headers = """        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;"""
    http = f"""server {{
    listen 80;
    server_name {proxy.domain};
    location / {{
        proxy_pass {proxy.upstream};
{headers}
    }}
}}

"""
    if not proxy.tls:
        return http
    return http + f"""server {{
    listen 443 ssl;
    server_name {proxy.domain};
    ssl_certificate {proxy.certificate};
    ssl_certificate_key {proxy.certificate_key};
    location / {{
        proxy_pass {proxy.upstream};
{headers}
    }}
}}
"""
