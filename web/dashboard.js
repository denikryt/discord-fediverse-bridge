const endpoint =
  document.body.getAttribute("data-dashboard-endpoint") || "/dashboard/data";

const communityFilterOrder = ["subscribed", "local", "all"];
const directoryViewOrder = ["communities", "guilds"];
const communitySectionTitles = {
  subscribed: "Federated communities",
  local: "Local communities",
  all: "All communities",
};

let activeCommunityFilter = "local";
let activeDirectoryView = "communities";
let dashboardData = null;

/**
 * Escape text before interpolating it into HTML strings.
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * Render one anchor tag for one URL-like value.
 */
function link(url, label, external = false) {
  const href = escapeHtml(url);
  const text = escapeHtml(label || url || "");
  const attributes = external ? ` target="_blank" rel="noopener noreferrer"` : "";
  return url ? `<a href="${href}"${attributes}>${text}</a>` : "";
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
 * Format Discord forum names with the public channel prefix used in Discord UI.
 */
function discordChannelName(name) {
  const fallback = name || "Unknown forum channel";
  if (fallback === "Unknown forum channel" || fallback.startsWith("#")) {
    return fallback;
  }
  return `#${fallback}`;
}

/**
 * Convert common ActivityPub actor URLs into compact public acct handles.
 */
function actorHandleFromUrl(actorUrl) {
  try {
    const url = new URL(actorUrl);
    const parts = url.pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
    const username = parts[parts.length - 1];
    return username ? `${username}@${url.hostname}` : actorUrl;
  } catch (_error) {
    return actorUrl;
  }
}

/**
 * Convert community actor URLs into Lemmy-style public community handles.
 */
function communityHandleFromUrl(actorUrl) {
  try {
    const url = new URL(actorUrl);
    const parts = url.pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
    const community = parts[parts.length - 1];
    return community ? `!${community}@${url.hostname}` : actorUrl;
  } catch (_error) {
    return actorUrl;
  }
}

/**
 * Build one clickable root URL for one federated instance hostname.
 */
function instanceUrlFromHost(host) {
  return host ? `https://${host}` : "";
}

/**
 * Build the copy affordance for one handle-like value.
 */
function copyButton(value, label) {
  const escapedValue = escapeHtml(value);
  const escapedLabel = escapeHtml(label);
  return `<div class="relay-pill">
    <span class="relay-pill-text">${escapedLabel}</span>
    <span class="copy-toast" role="status" aria-live="polite">Copied</span>
    <button class="copy-button" type="button" data-copy="${escapedValue}" aria-label="Copy value" title="Copy value">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M16 1H6a2 2 0 0 0-2 2v12h2V3h10V1Zm3 4H10a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2Zm0 16H10V7h9v14Z"></path>
      </svg>
    </button>
  </div>`;
}

/**
 * Render the nested subscriber breakdown for one local community.
 */
function subscriberBreakdown(community, subscriberCount) {
  const remoteCount = Number(community.remoteSubscriberCount || 0);
  const localCount = Number(community.localSubscriberCount || 0);
  return `<details class="detail-nested" data-nested-detail="subscribers">
    <summary class="detail-nested-summary">
      <span>Remote ${escapeHtml(String(remoteCount))} · Local ${escapeHtml(String(localCount))}</span>
      <span class="detail-nested-chevron" aria-hidden="true">›</span>
    </summary>
    <div class="detail-nested-body">
      <div class="detail-block">
        <span class="detail-label">Remote</span>
        ${(community.followers || []).length
          ? list(
              community.followers,
              (follower) => `<li>${link(follower.actorUrl, actorHandleFromUrl(follower.actorUrl))}</li>`,
            )
          : "<p class='muted'>None.</p>"}
      </div>
      <div class="detail-block">
        <span class="detail-label">Local</span>
        <p class="muted">${escapeHtml(String(localCount))}</p>
      </div>
    </div>
  </details>`;
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

/**
 * Convert the JSON payload into community directory rows for all filters.
 */
function buildCommunityRows(data) {
  const localRows = (data.localCommunities || []).map((community) => {
    const subscriberCount = Number(community.remoteSubscriberCount || 0) + Number(community.localSubscriberCount || 0);
    const detailBlocks = [
      {
        label: "Description",
        body: `<p>${escapeHtml(community.description || "No description.")}</p>`,
      },
      {
        label: "Hosted on",
        body: `<p>${escapeHtml(community.hostDiscord?.guildName || "Unknown guild")} · ${escapeHtml(discordChannelName(community.hostDiscord?.forumChannelName))}</p>`,
      },
    ];
    if (community.inviteUrl) {
      detailBlocks.push({
        label: "Invite",
        body: `<p><a class="invite-link" href="${escapeHtml(community.inviteUrl)}" target="_blank" rel="noopener noreferrer">Invite</a></p>`,
      });
    }
    detailBlocks.push({
      label: "Subscribers",
      body: subscriberBreakdown(community, subscriberCount ? String(subscriberCount) : "0"),
    });
    return {
      key: `local:${community.slug}`,
      sortName: (community.name || community.slug || "").toLowerCase(),
      name: community.name || community.slug || "Unnamed community",
      relayValue: community.relayHandle || community.actorUrl || "",
      relayLabel: community.relayHandle || community.actorUrl || "",
      subscriberLabel: subscriberCount ? String(subscriberCount) : "0",
      expandable: true,
      detailBlocks,
    };
  });

  const subscribedRows = (data.bridgeActorFollows || []).map((follow) => ({
    key: `subscribed:${follow.communityActorUrl}`,
    sortName: String(follow.communityName || communityHandleFromUrl(follow.communityActorUrl)).toLowerCase(),
    name: follow.communityName || communityHandleFromUrl(follow.communityActorUrl),
    nameUrl: follow.communityActorUrl || "",
    relayValue: follow.communityHandle || communityHandleFromUrl(follow.communityActorUrl),
    relayLabel: follow.communityHandle || communityHandleFromUrl(follow.communityActorUrl),
    subscriberLabel: "—",
    expandable: false,
    detailBlocks: [],
  }));

  return {
    subscribed: subscribedRows.sort((left, right) => left.sortName.localeCompare(right.sortName)),
    local: localRows.sort((left, right) => left.sortName.localeCompare(right.sortName)),
    all: [...localRows, ...subscribedRows].sort((left, right) => left.sortName.localeCompare(right.sortName)),
  };
}

/**
 * Render one community directory row.
 */
function renderCommunityRow(row) {
  const nameCellValue = row.nameUrl
    ? link(row.nameUrl, row.name, true)
    : escapeHtml(row.name);
  const columns = `<div class="row-columns community-columns-grid">
      <div class="row-cell">
        <span class="row-cell-label">Name</span>
        <span class="row-cell-value row-name">${nameCellValue}</span>
      </div>
      <div class="row-cell">
        <span class="row-cell-label">Handle</span>
        <span class="row-cell-value">${copyButton(row.relayValue, row.relayLabel)}</span>
      </div>
      <div class="row-cell">
        <span class="row-cell-label">Subscribers</span>
        <span class="row-cell-value">${escapeHtml(row.subscriberLabel)}</span>
      </div>
    </div>`;
  if (!row.expandable) {
    return `<article class="row-card row-card-static">
      <div class="row-summary row-summary-static">
        ${columns}
      </div>
    </article>`;
  }
  return `<details class="row-card row-card-expandable">
    <summary class="row-summary">
      ${columns}
      <span class="row-chevron" aria-hidden="true">›</span>
    </summary>
    <div class="row-detail">
      ${row.detailBlocks
        .map(
          (block) => `<div class="detail-block">
            <span class="detail-label">${escapeHtml(block.label)}</span>
            ${block.body}
          </div>`,
        )
        .join("")}
    </div>
  </details>`;
}

/**
 * Render one guild directory row.
 */
function renderGuildRow(guild) {
  const hostedCount = Number((guild.hostedCommunities || []).length);
  const subscriptionCount = Number((guild.remoteSubscriptions || []).length) + Number((guild.localSubscriptions || []).length);
  const inviteCell = guild.inviteUrl
    ? `<a class="invite-link" href="${escapeHtml(guild.inviteUrl)}" target="_blank" rel="noopener noreferrer">Invite</a>`
    : "<span class='muted'>—</span>";
  return `<details class="row-card row-card-expandable">
    <summary class="row-summary">
      <div class="row-columns guild-columns-grid">
        <div class="row-cell">
          <span class="row-cell-label">Guild</span>
          <span class="row-cell-value row-name">${escapeHtml(guild.guildName)}</span>
        </div>
        <div class="row-cell">
          <span class="row-cell-label">Hosted</span>
          <span class="row-cell-value">${escapeHtml(String(hostedCount))}</span>
        </div>
        <div class="row-cell">
          <span class="row-cell-label">Subscriptions</span>
          <span class="row-cell-value">${escapeHtml(String(subscriptionCount))}</span>
        </div>
        <div class="row-cell">
          <span class="row-cell-label">Invite</span>
          <span class="row-cell-value">${inviteCell}</span>
        </div>
      </div>
      <span class="row-chevron" aria-hidden="true">›</span>
    </summary>
    <div class="row-detail">
      <div class="detail-block">
        <span class="detail-label">Hosted communities</span>
        ${list(
          guild.hostedCommunities || [],
          (entry) => `<li>${escapeHtml(entry.relayHandle)} in ${escapeHtml(discordChannelName(entry.forumChannelName))}</li>`,
        )}
      </div>
      <div class="detail-block">
        <span class="detail-label">Remote subscriptions</span>
        ${list(
          guild.remoteSubscriptions || [],
          (entry) => `<li>${escapeHtml(discordChannelName(entry.forumChannelName))} → ${escapeHtml(entry.communityHandle)}</li>`,
        )}
      </div>
      <div class="detail-block">
        <span class="detail-label">Local subscriptions</span>
        ${list(
          guild.localSubscriptions || [],
          (entry) => `<li>${escapeHtml(discordChannelName(entry.forumChannelName))} → ${escapeHtml(entry.communityHandle)}</li>`,
        )}
      </div>
    </div>
  </details>`;
}

/**
 * Apply active-state styling for the top-level page tabs.
 */
function syncViewTabs() {
  directoryViewOrder.forEach((view) => {
    const button = document.querySelector(`[data-view-tab="${view}"]`);
    const section = document.getElementById(`view-${view}`);
    const isActive = activeDirectoryView === view;
    if (button) {
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", String(isActive));
    }
    if (section) {
      section.hidden = !isActive;
    }
  });
}

/**
 * Apply active-state styling for the communities sub-filter tabs.
 */
function syncCommunityFilterTabs() {
  communityFilterOrder.forEach((filterName) => {
    const button = document.querySelector(`[data-community-filter="${filterName}"]`);
    const isActive = activeCommunityFilter === filterName;
    if (button) {
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", String(isActive));
    }
  });
}

/**
 * Render the community directory rows for the active filter.
 */
function renderCommunitiesView(data) {
  const sectionTitle = document.getElementById("community-section-title");
  if (sectionTitle) {
    // The visible section heading should follow the active directory filter so
    // the page explains which slice of communities is currently on screen.
    sectionTitle.textContent = communitySectionTitles[activeCommunityFilter] || "Communities";
  }
  const communityRows = buildCommunityRows(data)[activeCommunityFilter];
  document.getElementById("community-table").innerHTML = communityRows.length
    ? communityRows.map(renderCommunityRow).join("")
    : "<p class='directory-empty muted'>No communities in this section.</p>";
}

/**
 * Render the guild directory rows.
 */
function renderGuildsView(data) {
  const guilds = (data.discordGuilds || []).slice().sort((left, right) => {
    return String(left.guildName || "").localeCompare(String(right.guildName || ""));
  });
  document.getElementById("guild-table").innerHTML = guilds.length
    ? guilds.map(renderGuildRow).join("")
    : "<p class='directory-empty muted'>No guild placements.</p>";
}

/**
 * Reset nested community detail state when the parent row closes.
 */
function bindNestedDetailReset() {
  document.querySelectorAll(".row-card-expandable").forEach((row) => {
    row.addEventListener("toggle", () => {
      if (row.open) {
        return;
      }
      row.querySelectorAll("[data-nested-detail]").forEach((nestedDetail) => {
        nestedDetail.open = false;
      });
    });
  });
}

/**
 * Render the summary cards and lower metadata panels.
 */
function renderMetaPanels(data) {
  document.getElementById("summary").innerHTML = [
    ["Registered users", data.instance.registeredUserCount],
    ["Local communities", data.instance.localCommunityCount],
    ["Remote followers", data.instance.localCommunityFollowerCount],
    ["Bridge follows", data.instance.bridgeActorFollowCount],
  ]
    .map(
      ([key, value]) =>
        `<article class="stat"><span class="stat-label">${escapeHtml(key)}</span><div class="stat-value">${escapeHtml(value)}</div></article>`,
    )
    .join("");

  const federatedInstances = data.federation.instances || [];
  document.getElementById("federation").innerHTML = `
    <details class="detail-nested">
      <summary class="detail-nested-summary">
        <span>Instances (${escapeHtml(String(federatedInstances.length))})</span>
        <span class="detail-nested-chevron" aria-hidden="true">›</span>
      </summary>
      <div class="detail-nested-body">
        ${list(
          federatedInstances.map((host) => ({ host, url: instanceUrlFromHost(host) })),
          (entry) => `<li>${link(entry.url, entry.host, true)}</li>`,
        )}
      </div>
    </details>`;
}

/**
 * Render the whole dashboard shell from the loaded payload.
 */
function renderDashboard(data) {
  dashboardData = data;
  renderMetaPanels(data);
  renderCommunitiesView(data);
  renderGuildsView(data);
  syncViewTabs();
  syncCommunityFilterTabs();
  bindCopyButtons();
  bindNestedDetailReset();

  const versionElement = document.getElementById("project-version");
  const version = data.instance?.version;
  if (versionElement && version) {
    versionElement.textContent = `Version ${version}`;
    versionElement.hidden = false;
  } else if (versionElement) {
    versionElement.hidden = true;
  }
}

/**
 * Attach tab handlers after the static shell has loaded.
 */
function bindTabs() {
  document.querySelectorAll("[data-view-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      activeDirectoryView = button.getAttribute("data-view-tab") || "communities";
      syncViewTabs();
    });
  });

  document.querySelectorAll("[data-community-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      activeCommunityFilter = button.getAttribute("data-community-filter") || "subscribed";
      syncCommunityFilterTabs();
      if (dashboardData !== null) {
        renderCommunitiesView(dashboardData);
        bindCopyButtons();
        bindNestedDetailReset();
      }
    });
  });
}

bindTabs();

fetch(endpoint)
  .then((response) => response.json())
  .then((data) => {
    renderDashboard(data);
  })
  .catch((error) => {
    document.getElementById("summary").innerHTML = `<article class="panel">Failed to load dashboard data: ${escapeHtml(error)}</article>`;
  });
