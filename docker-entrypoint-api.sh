#!/bin/sh
set -e

# Fix ownership of bind-mounted data directory at startup.
# This handles the common case where ./data is created by Docker (as root)
# or by the host user, and the container app user (uid 1000) can't write to it.
# Same pattern used by official postgres/redis images.
if [ "$(id -u)" = "0" ]; then
    # Only fix ownership if needed (avoids slow recursive chown on large data dirs)
    if [ "$(stat -c '%u' /app/data 2>/dev/null)" != "1000" ]; then
        chown -R sandclaude:sandclaude /app/data
    fi
    exec gosu sandclaude "$@"
fi

# If already running as sandclaude (e.g. docker run --user), just exec
exec "$@"
