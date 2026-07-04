"use strict";

// Where to fetch the board-game data from. Configured in index.html
// (window.RBGA_API_BASE) so this file needn't change per deployment.
const API_BASE = (window.RBGA_API_BASE || "").replace(/\/$/, "");

const gallery = document.getElementById("gallery");
const statusEl = document.getElementById("status");
const countEl = document.getElementById("count");
const searchEl = document.getElementById("search");
const playersEl = document.getElementById("players");

let allGames = [];

const isUrl = (s) => typeof s === "string" && /^https?:\/\//i.test(s);
const initial = (title) => (title || "?").trim().charAt(0).toUpperCase() || "?";

function playerLabel(g) {
  const lo = g.min_players, hi = g.max_players;
  if (lo == null && hi == null) return null;
  if (lo != null && hi != null) return lo === hi ? `${lo} players` : `${lo}–${hi} players`;
  return `${lo ?? hi}${lo == null ? " max" : "+"} players`;
}

function makeCover(g) {
  const cover = document.createElement("div");
  cover.className = "cover";
  if (isUrl(g.image)) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = g.title || "";
    img.src = g.image;
    img.addEventListener("error", () => {
      cover.classList.add("cover--fallback");
      cover.textContent = initial(g.title); // swap to fallback tile
    });
    cover.appendChild(img);
  } else {
    // CSV imports store a bare filename here, not a usable URL — show a tile.
    cover.classList.add("cover--fallback");
    cover.textContent = initial(g.title);
  }
  return cover;
}

function makeCard(g) {
  const card = document.createElement("article");
  card.className = "card";
  card.appendChild(makeCover(g));

  const body = document.createElement("div");
  body.className = "card-body";

  const title = document.createElement("h2");
  title.className = "card-title";
  title.textContent = g.title || "Untitled";
  body.appendChild(title);

  if (g.publisher) {
    const pub = document.createElement("p");
    pub.className = "card-pub";
    pub.textContent = g.publisher;
    body.appendChild(pub);
  }

  const badges = document.createElement("div");
  badges.className = "badges";
  const players = playerLabel(g);
  if (players) badges.appendChild(badge(players));
  if (g.location) badges.appendChild(badge(`📍 ${g.location}`));
  if (badges.children.length) body.appendChild(badges);

  card.appendChild(body);
  return card;
}

function badge(text) {
  const b = document.createElement("span");
  b.className = "badge";
  b.textContent = text;
  return b;
}

function matches(g, q, p) {
  const textOk = !q ||
    (g.title && g.title.toLowerCase().includes(q)) ||
    (g.publisher && g.publisher.toLowerCase().includes(q));
  const playersOk = !p || (p >= (g.min_players ?? 1) && p <= (g.max_players ?? 99));
  return textOk && playersOk;
}

function render() {
  const q = searchEl.value.trim().toLowerCase();
  const p = parseInt(playersEl.value, 10) || 0;
  const shown = allGames.filter((g) => matches(g, q, p));

  gallery.replaceChildren(...shown.map(makeCard));
  countEl.textContent = `${shown.length} of ${allGames.length} games`;
  statusEl.textContent = shown.length ? "" : "No games match your search.";
  statusEl.className = "status";
}

async function load() {
  try {
    const res = await fetch(`${API_BASE}/board-games`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    allGames = await res.json();
    render();
  } catch (err) {
    statusEl.textContent = "Couldn't load the library right now. Please try again later.";
    statusEl.className = "status status--error";
    countEl.textContent = "";
  }
}

searchEl.addEventListener("input", render);
playersEl.addEventListener("input", render);
load();
