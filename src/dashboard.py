"""Public dashboard aggregation and rendering for bridge instance metadata.

This module owns the public-data boundary for `/dashboard` and `/dashboard/data`.
It explains instance state without exposing Discord-internal identifiers,
secrets, private keys, raw database paths, or internal service URLs.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
from typing import Any
from urllib.parse import urlparse


def build_dashboard_payload(runtime: Any) -> dict[str, object]:
    """Build the public dashboard payload from safe runtime state."""
    settings = runtime.settings
    origin = str(getattr(settings, "normalized_fedify_origin", settings.fedify_origin)).rstrip("/")
    actor_identifier = getattr(settings, "fedify_actor_identifier", "bridge")
    local_communities = runtime.database.list_local_communities()
    followers = runtime.database.list_local_community_followers_for_all(status="accepted")
    bridge_follows = runtime.database.list_bridge_actor_follows()

    followers_by_community: dict[int, list[object]] = defaultdict(list)
    for follower in followers:
        followers_by_community[getattr(follower, "local_community_id")].append(follower)

    community_payloads = []
    follower_hosts: set[str] = set()
    for community in local_communities:
        community_followers = followers_by_community.get(getattr(community, "id"), [])
        follower_payloads = []
        for follower in community_followers:
            host = _hostname_from_url(getattr(follower, "remote_actor_id", "")) or _hostname_from_url(
                getattr(follower, "remote_inbox_url", "")
            )
            if host:
                follower_hosts.add(host)
            follower_payloads.append(
                {
                    "actorUrl": getattr(follower, "remote_actor_id"),
                    "instanceHost": host,
                }
            )
        community_payloads.append(
            {
                "slug": getattr(community, "slug"),
                "name": getattr(community, "display_name"),
                "description": getattr(community, "summary"),
                "actorUrl": getattr(community, "actor_url"),
                "aliasUrl": f"{origin}/c/{getattr(community, 'slug')}",
                "followersUrl": getattr(community, "followers_url"),
                "subscriberCount": len(community_followers),
                "followers": follower_payloads,
            }
        )

    allowlist = _normalize_host_list(getattr(settings, "federation_allowlist", []))
    bridge_follow_payloads = [
        {
            "communityActorUrl": getattr(follow, "community_actor_id"),
            "instanceHost": _hostname_from_url(getattr(follow, "community_actor_id", "")),
            "status": getattr(follow, "status"),
            "technicalDetails": {
                "communityInboxUrl": getattr(follow, "community_inbox_url"),
            },
        }
        for follow in bridge_follows
    ]

    return {
        "instance": {
            "title": "Discord/Fediverse Bridge Instance",
            "origin": origin,
            "bridgeActorUrl": f"{origin}/actors/{actor_identifier}",
            "localCommunityCount": len(community_payloads),
            "localCommunityFollowerCount": len(followers),
            "bridgeActorFollowCount": len(bridge_follow_payloads),
        },
        "localCommunities": community_payloads,
        "bridgeActorFollows": bridge_follow_payloads,
        "federation": {
            "mode": "restricted_allowlist" if allowlist else "open",
            "allowlist": allowlist,
            "connectedFollowerInstances": sorted(follower_hosts),
        },
        "credits": {
            "label": "Made with passion by Nachitima",
            "url": "https://nachitima.com",
        },
    }


def render_dashboard_html(payload_endpoint: str = "/dashboard/data") -> str:
    """Render a minimal public dashboard shell backed by the JSON endpoint."""
    endpoint = escape(payload_endpoint, quote=True)
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Discord/Fediverse Bridge Instance</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; background: Canvas; color: CanvasText; }}
    main {{ max-width: 1080px; margin: 0 auto; padding: 2rem; }}
    header, section, footer {{ margin-bottom: 1.5rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }}
    .card {{ border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); border-radius: 14px; padding: 1rem; }}
    .muted {{ opacity: .75; }}
    a {{ color: LinkText; overflow-wrap: anywhere; }}
    code {{ overflow-wrap: anywhere; }}
    summary {{ cursor: pointer; font-weight: 600; }}
    ul {{ padding-left: 1.2rem; }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Discord/Fediverse Bridge Instance</h1>
    <p class=\"muted\">This instance bridges Discord communities into the Fediverse.</p>
  </header>
  <section id=\"summary\" class=\"grid\"></section>
  <section><h2>Local communities</h2><div id=\"communities\" class=\"grid\"></div></section>
  <section><h2>Bridge actor follows</h2><div id=\"follows\"></div></section>
  <section><h2>Federation policy</h2><div id=\"federation\" class=\"card\"></div></section>
  <footer class=\"muted\">Made with passion by <a href=\"https://nachitima.com\">Nachitima</a></footer>
</main>
<script>
const endpoint = "{endpoint}";
function link(url, label) {{ return url ? `<a href="${{url}}">${{label || url}}</a>` : ""; }}
function list(items, render) {{ return items.length ? `<ul>${{items.map(render).join("")}}</ul>` : "<p class='muted'>None.</p>"; }}
fetch(endpoint).then(r => r.json()).then(data => {{
  document.getElementById("summary").innerHTML = [
    ["Origin", link(data.instance.origin)],
    ["Bridge actor", link(data.instance.bridgeActorUrl)],
    ["Local communities", data.instance.localCommunityCount],
    ["Local followers", data.instance.localCommunityFollowerCount],
    ["Bridge follows", data.instance.bridgeActorFollowCount],
  ].map(([k, v]) => `<article class="card"><strong>${{k}}</strong><p>${{v}}</p></article>`).join("");

  document.getElementById("communities").innerHTML = data.localCommunities.length ? data.localCommunities.map(c => `
    <article class="card">
      <h3>${{c.name}}</h3>
      <p class="muted">/${{c.slug}}</p>
      <p>Subscribers: <strong>${{c.subscriberCount}}</strong></p>
      <p>${{link(c.actorUrl, "Actor")}} · ${{link(c.aliasUrl, "Alias")}} · ${{link(c.followersUrl, "Followers collection")}}</p>
      <details><summary>Description</summary><p>${{c.description || "No description."}}</p></details>
      <details><summary>Followers</summary>${{list(c.followers, f => `<li>${{link(f.actorUrl, f.instanceHost || f.actorUrl)}}</li>`)}}</details>
    </article>`).join("") : "<p class='muted'>No local communities.</p>";

  document.getElementById("follows").innerHTML = list(data.bridgeActorFollows, f =>
    `<li>${{link(f.communityActorUrl)}} — ${{f.status}} <details><summary>Technical details</summary><code>${{f.technicalDetails.communityInboxUrl || "No inbox URL"}}</code></details></li>`
  );
  document.getElementById("federation").innerHTML = `
    <p>Federation mode: <strong>${{data.federation.mode === "open" ? "open" : "restricted allowlist"}}</strong></p>
    <details open><summary>Allowlist</summary>${{list(data.federation.allowlist, h => `<li>${{h}}</li>`)}}</details>
    <details open><summary>Connected follower instances</summary>${{list(data.federation.connectedFollowerInstances, h => `<li>${{h}}</li>`)}}</details>`;
}}).catch(error => {{
  document.getElementById("summary").innerHTML = `<article class="card">Failed to load dashboard data: ${{error}}</article>`;
}});
</script>
</body>
</html>"""


def _hostname_from_url(value: str | None) -> str | None:
    """Extract a lowercase hostname from a URL-like value."""
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.hostname.lower() if parsed.hostname else None


def _normalize_host_list(values: list[str]) -> list[str]:
    """Normalize allowlist entries as sorted lowercase hostnames."""
    hosts = {_hostname_from_url(value.strip()) for value in values if value.strip()}
    return sorted(host for host in hosts if host)
