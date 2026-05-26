const endpoint =
  document.body.getAttribute("data-dashboard-endpoint") || "/dashboard/data";

/**
 * Render one anchor tag for one URL-like value.
 */
function link(url, label) {
  return url ? `<a href="${url}">${label || url}</a>` : "";
}

/**
 * Render one UL list or a muted empty placeholder.
 */
function list(items, render) {
  return items.length
    ? `<ul>${items.map(render).join("")}</ul>`
    : "<p class='muted'>None.</p>";
}

/**
 * Build the copy affordance for one local-community relay handle.
 */
function copyButton(value) {
  return `<button class="copy-button" type="button" data-copy="${value}" aria-label="Copy relay URL" title="Copy relay URL">
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M16 1H6a2 2 0 0 0-2 2v12h2V3h10V1Zm3 4H10a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2Zm0 16H10V7h9v14Z"></path>
    </svg>
  </button>`;
}

/**
 * Wire copy buttons after the dashboard shell has rendered.
 */
function bindCopyButtons() {
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.getAttribute("data-copy");
      if (!value) {
        return;
      }

      await navigator.clipboard.writeText(value);
      const toast = button.parentElement?.querySelector(".copy-toast");
      if (!toast) {
        return;
      }

      toast.classList.add("visible");
      const previousTimer = Number(button.dataset.toastTimer || "0");
      if (previousTimer) {
        window.clearTimeout(previousTimer);
      }

      const nextTimer = window.setTimeout(() => {
        toast.classList.remove("visible");
      }, 1100);
      button.dataset.toastTimer = String(nextTimer);
    });
  });
}

fetch(endpoint)
  .then((response) => response.json())
  .then((data) => {
    document.getElementById("summary").innerHTML = [
      ["Registered users", data.instance.registeredUserCount],
      ["Local communities", data.instance.localCommunityCount],
      ["Remote subscribers", data.instance.localCommunityFollowerCount],
      ["Local subscribers", data.localCommunities.reduce(
        (count, community) => count + (community.localSubscriberCount || 0),
        0,
      )],
      ["Bridge follows", data.instance.bridgeActorFollowCount],
    ]
      .map(
        ([key, value]) =>
          `<article class="stat"><span class="stat-label">${key}</span><div class="stat-value">${value}</div></article>`,
      )
      .join("");

    document.getElementById("communities").innerHTML = data.localCommunities.length
      ? data.localCommunities
          .map(
            (community) => `
    <article class="community">
      <h3>${community.name}</h3>
      <div class="handle">
        <span class="handle-text">${community.relayHandle}</span>
        <span class="copy-toast" role="status" aria-live="polite">Copied</span>
        ${copyButton(community.relayHandle)}
      </div>
      <p class="description">${community.description || "No description."}</p>
      <details>
        <summary>Remote subscribers (${community.remoteSubscriberCount || community.followers.length})</summary>
        ${list(community.followers, (follower) => `<li>${link(follower.actorUrl, follower.actorUrl)}</li>`)}
      </details>
    </article>`,
          )
          .join("")
      : "<p class='muted'>No local communities.</p>";

    document.getElementById("follows").innerHTML = data.bridgeActorFollows.length
      ? `<div class="rowlist">${data.bridgeActorFollows
          .map(
            (follow) => `
    <div class="follow-row">
      <p>${link(follow.communityActorUrl, follow.communityActorUrl)}</p>
    </div>`,
          )
          .join("")}</div>`
      : "<p class='muted'>No bridge actor follows.</p>";

    document.getElementById("federation").innerHTML = `
    <p>Federation mode: <strong>${data.federation.mode === "open" ? "open" : "restricted allowlist"}</strong></p>
    <details open><summary>Allowlist</summary>${list(data.federation.allowlist, (host) => `<li>${host}</li>`)}</details>`;

    bindCopyButtons();
  })
  .catch((error) => {
    document.getElementById("summary").innerHTML = `<article class="panel">Failed to load dashboard data: ${error}</article>`;
  });
