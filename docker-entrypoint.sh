#!/bin/sh
set -e

# Generate TLS certs if they don't exist
if [ ! -f /app/certs/ca.pem ]; then
    echo "Generating TLS certificates..."
    python -c "
from dbproxy.tls import generate_certs
generate_certs('/app/certs')
print('Certificates generated.')
"
fi

# Start the CONNECT redirector in background
python -m dbproxy.connect_redirector &
REDIRECTOR_PID=$!
echo "Started CONNECT redirector (PID $REDIRECTOR_PID)"

# Cleanup on exit
cleanup() {
    echo "Shutting down..."
    kill "$REDIRECTOR_PID" 2>/dev/null || true
    wait "$REDIRECTOR_PID" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

# Run main proxy in foreground (not exec, so trap works)
python -m dbproxy "$@" &
PROXY_PID=$!
echo "Started main proxy (PID $PROXY_PID)"

# Wait for either process to exit
wait "$PROXY_PID"
EXIT_CODE=$?
cleanup
exit $EXIT_CODE
