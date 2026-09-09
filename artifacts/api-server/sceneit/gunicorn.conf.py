"""Production Gunicorn bounds; importing this file has no application side effects."""
import os


bind = f"0.0.0.0:{os.getenv('PORT', '8080')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "2"))

# Provider work is bounded at 45 seconds and uncertain work is recoverable after
# 75 seconds. Keep the total request ceiling at 90 seconds, never below either.
timeout = 90
graceful_timeout = 90
keepalive = 5

# Trust forwarding headers only from operator-declared proxy addresses.
forwarded_allow_ips = os.getenv("SCENEIT_FORWARDED_ALLOW_IPS", "127.0.0.1")

# Never log URLs, query strings, cookies, authorization, source addresses, or
# request bodies. The request ID is generated/validated by the application.
accesslog = "-"
access_log_format = (
    '{"event":"http_access","request_id":"%({x-request-id}o)s",'
    '"method":"%(m)s","status":%(s)s,"duration_us":%(D)s}'
)
errorlog = "-"
capture_output = False

limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190