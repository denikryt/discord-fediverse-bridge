"""Shared test domains used to build stable fake ActivityPub URLs."""

# The file lives under `tests/` so all test-only helpers stay together, but it
# remains a plain module rather than a package import target to avoid
# reintroducing pytest import-path conflicts with vendored test suites.
LEMMY_EXAMPLE_DOMAIN = "lemmy.example"
LEMMY_WORLD_DOMAIN = "lemmy.world"
BRIDGE_EXAMPLE_DOMAIN = "bridge.example"
BRIDGE_HOST_DOMAIN = "discord-bridge.example.com"
