"""Shared test domains used to build stable fake ActivityPub URLs."""

# The constants live outside `tests/` so pytest does not need the project test
# directory to become an import package, which would conflict with vendored
# `discordops` tests collected in the same run.
LEMMY_EXAMPLE_DOMAIN = "lemmy.example"
LEMMY_WORLD_DOMAIN = "lemmy.world"
BRIDGE_EXAMPLE_DOMAIN = "bridge.example"
BRIDGE_HOST_DOMAIN = "discord-bridge.example.com"
