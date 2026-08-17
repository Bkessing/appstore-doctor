# Runs the MCP server. This exists for registry infrastructure (Glama runs it
# to verify the server starts and answers introspection); local users are
# better served by `python3 -m appstore_doctor.mcp` directly, since the
# signing checks read the host keychain, which a container cannot see.
#
# ASC credentials arrive as env vars plus a mounted key:
#   docker run -i --rm \
#     -e ASC_KEY_ID -e ASC_ISSUER_ID \
#     -v ~/.appstoreconnect/private_keys:/root/.appstoreconnect/private_keys:ro \
#     appstore-doctor
# Without credentials the server still starts and lists tools; ASC-backed
# tools return a clear credentials-missing message, by design.
FROM python:3.12-slim

WORKDIR /app
COPY appstore_doctor/ appstore_doctor/

# stdlib-only by design: no pip install, nothing to resolve, nothing to pin.
ENTRYPOINT ["python", "-m", "appstore_doctor.mcp"]
