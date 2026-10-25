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

  discover(document);
})();
