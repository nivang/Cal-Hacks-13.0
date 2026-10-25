(() => {
  const initialized = new WeakSet();
  const queue = [];
  let active = 0;
  let leaving = false;

  // Bound concurrency so a long seller lookup cannot monopolize the connection pool.
  function schedule(work) {
    queue.push(work);
    pump();
  }

  function pump() {
    while (!leaving && active < 4 && queue.length) {
      active += 1;
      queue.shift()().finally(() => { active -= 1; pump(); });
    }
  }

  // Sections marked data-refresh (Live, Payments) poll with the ETag they came with; the server answers 304 when
  // nothing changed, so a slow link carries almost nothing. Polls pause while the tab is hidden.
  const SLOW_MS = 15000;

  function cards(root) {
    return new Set([...root.querySelectorAll("[data-card]")].map(el => el.dataset.card));
  }

  function openDetails(root) {
    const open = new Set();
    root.querySelectorAll("details[open]").forEach(d => {
      const card = d.closest("[data-card]");
      const index = card ? [...card.querySelectorAll("details")].indexOf(d) : -1;
      if (card) open.add(`${card.dataset.card}:${index}`);
    });
    return open;
  }

  function restoreDetails(root, open) {
    root.querySelectorAll("[data-card]").forEach(card => {
      card.querySelectorAll("details").forEach((d, index) => {
        if (open.has(`${card.dataset.card}:${index}`)) d.open = true;
      });
    });
  }

  function setOnline(online) {
    document.querySelectorAll(".live-pulse").forEach(el => el.classList.toggle("is-offline", !online));
  }

  function poll(fragment, section, etag) {
    let current = fragment;
    // Faster while Sentinel is mid check (the fragment says 1000), else every 3 s.
    const every = () => Number(current.dataset.refresh) || 3000;
    let tag = etag;
    let busy = false;
    let timer = null;

    function later(ms) {
      clearTimeout(timer);
      timer = setTimeout(tick, ms);
    }

    async function tick() {
      if (leaving || !current.isConnected) return;
      if (document.hidden || busy) { later(every()); return; }
      busy = true;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 20000);
      try {
        const headers = { "X-Sentinel-Section": section, "Accept": "text/html" };
        if (tag) headers["If-None-Match"] = tag;
        const response = await fetch(window.location.href, {
          headers, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (response.redirected) {
          leaving = true;
          window.location.replace(response.url);
          return;
        }
        if (response.status === 304) { setOnline(true); later(every()); return; }
        if (!response.ok) throw new Error("Section could not refresh");
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const next = page.querySelector("[data-account-fragment]");
        if (!next || next.dataset.accountFragment !== section) throw new Error("Incomplete section");
        if (leaving || !current.isConnected) return;
        const seen = cards(current);
        const open = openDetails(current);
        current.replaceWith(next);
        restoreDetails(next, open);
        next.querySelectorAll("[data-card]").forEach(el => {
          if (!seen.has(el.dataset.card)) el.classList.add("is-new");
        });
        current = next;
        tag = response.headers.get("ETag") || tag;
        setOnline(true);
        tickClocks();
        later(every());
      } catch {
        setOnline(false);
        later(SLOW_MS);
      } finally {
        busy = false;
        clearTimeout(timeout);
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && current.isConnected) later(0);
    });
    later(every());
  }

  // Escrow deadlines count down and "when" times read as "12 s ago", ticking in the browser between polls.
  function tickClocks() {
    const now = Date.now();
    document.querySelectorAll("[data-deadline]").forEach(el => {
      const left = Math.max(0, Math.round((Date.parse(el.dataset.deadline) - now) / 1000));
      if (Number.isNaN(left)) return;
      const h = Math.floor(left / 3600);
      const m = Math.floor((left % 3600) / 60);
      const sec = String(left % 60).padStart(2, "0");
      el.textContent = h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m}:${sec}`;
    });
    document.querySelectorAll("time[data-ago]").forEach(el => {
      const ago = Math.round((now - Date.parse(el.getAttribute("datetime"))) / 1000);
      if (Number.isNaN(ago) || ago > 86400) return;
      el.textContent = ago < 5 ? "just now" : ago < 60 ? `${ago} s ago`
        : ago < 3600 ? `${Math.floor(ago / 60)} min ago` : `${Math.floor(ago / 3600)} h ago`;
    });
  }

  function discover(root) {
    root.querySelectorAll("[data-account-section]").forEach(panel => {
      if (initialized.has(panel)) return;
      initialized.add(panel);
      const section = panel.dataset.accountSection;
      const message = panel.querySelector("[data-loading-message]");
      const retry = panel.querySelector("[data-loading-retry]");
      const placeholder = panel.querySelector("[data-loading-placeholder]");

      async function load() {
        if (leaving || !panel.isConnected) return;
        panel.setAttribute("aria-busy", "true");
        if (message) message.textContent = "Loading…";
        if (retry) retry.hidden = true;
        if (placeholder) placeholder.hidden = false;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 60000);
        try {
          const headers = { "X-Sentinel-Section": section, "Accept": "text/html" };
          if (panel.dataset.wallet) headers["X-Sentinel-Wallet"] = panel.dataset.wallet;
          const response = await fetch(window.location.href, {
            headers,
            credentials: "same-origin",
            cache: "no-store",
            signal: controller.signal,
          });
          if (response.redirected) {
            leaving = true;
            window.location.replace(response.url);
            return;
          }
          if (!response.ok) throw new Error("Section could not load");
          const html = await response.text();
          const page = new DOMParser().parseFromString(html, "text/html");
          const fragment = page.querySelector("[data-account-fragment]");
          if (!fragment || fragment.dataset.accountFragment !== section) {
            throw new Error("Incomplete section");
          }
          if (leaving || !panel.isConnected) return;
          if (section === "identity") {
            panel.replaceChildren(...fragment.childNodes);
            document.querySelectorAll("[data-account-email]").forEach(el => {
              el.textContent = panel.textContent.trim();
            });
            panel.removeAttribute("data-account-section");
            panel.setAttribute("aria-busy", "false");
          } else {
            panel.replaceWith(fragment);
            discover(fragment);
            if (fragment.dataset.refresh) poll(fragment, section, response.headers.get("ETag"));
            tickClocks();
          }
          const anchor = document.getElementById(window.location.hash.slice(1));
          if (anchor && fragment.contains(anchor)) anchor.scrollIntoView();
        } catch {
          panel.setAttribute("aria-busy", "false");
          if (message) message.textContent = "This section could not load. Try again.";
          if (placeholder) placeholder.hidden = true;
          if (retry) retry.hidden = false;
        } finally {
          clearTimeout(timeout);
        }
      }

      retry?.addEventListener("click", () => {
        retry.hidden = true;
        schedule(load);
      });
      schedule(load);
    });
  }

  // A setup link should reveal its destination, including collapsed connection help.
  function revealHash() {
    const id = window.location.hash.slice(1);
    if (!id) return;
    let target;
    try { target = document.getElementById(decodeURIComponent(id)); } catch { return; }
    if (!target) return;
    let details = target.closest("details");
    while (details) {
      details.open = true;
      details = details.parentElement?.closest("details");
    }
    target.scrollIntoView();
  }

  setInterval(tickClocks, 1000);
  window.addEventListener("hashchange", revealHash);
  revealHash();
  discover(document);
})();
