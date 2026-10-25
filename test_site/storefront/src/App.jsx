import { useCallback, useEffect, useMemo, useState } from "react";
import Hero from "./components/Hero.jsx";
import Toolbar from "./components/Toolbar.jsx";
import Categories from "./components/Categories.jsx";
import ServiceRow from "./components/ServiceRow.jsx";
import ServiceDrawer from "./components/ServiceDrawer.jsx";
import Footer from "./components/Footer.jsx";

const MARKET_URL = "/test-site/market.json";
const ALL = "All services";

function param(name) {
  try {
    return new URLSearchParams(window.location.search).get(name);
  } catch {
    return null;
  }
}

function setParam(name, value) {
  try {
    const url = new URL(window.location.href);
    if (value) url.searchParams.set(name, value);
    else url.searchParams.delete(name);
    window.history.replaceState(null, "", url);
  } catch {
    // A sandboxed preview may refuse history changes; the page still works.
  }
}

async function loadMarket() {
  const response = await fetch(MARKET_URL);
  if (response.ok) return response.json();
  if (import.meta.env.DEV) return (await import("./fixture.json")).default;  // dev only: before the backend is up
  throw new Error(`The marketplace answered ${response.status}.`);
}

function searchText(s) {
  return `${s.name} ${s.description} ${s.seller} ${s.category} ${s.path} ${s.tags.join(" ")}`.toLowerCase();
}

export default function App() {
  const [market, setMarket] = useState(null);
  const [error, setError] = useState(null);
  const [category, setCategory] = useState(ALL);
  const [query, setQuery] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const [revealed, setRevealed] = useState(() => param("reveal") === "1");
  const [open, setOpen] = useState(null);
  const close = useCallback(() => setOpen(null), []);

  useEffect(() => {
    loadMarket().then(
      (data) => {
        setMarket(data);
        const wanted = param("service");  // ?service=<path> opens that listing, for demos and links
        setOpen(data.services.find((s) => s.path === wanted) || null);
      },
      (e) => setError(e.message || "The marketplace could not be reached."),
    );
  }, []);

  useEffect(() => setParam("reveal", revealed ? "1" : null), [revealed]);
  useEffect(() => {
    if (market) setParam("service", open ? open.path : null);
  }, [open, market]);

  const services = market ? market.services : [];
  const categories = useMemo(() => {
    const listed = market?.categories?.length ? market.categories : [...new Set(services.map((s) => s.category))];
    const counts = new Map(listed.map((c) => [c, 0]));
    services.forEach((s) => counts.set(s.category, (counts.get(s.category) || 0) + 1));
    return [{ name: ALL, count: services.length }, ...[...counts].filter(([, n]) => n > 0).map(([name, count]) => ({ name, count }))];
  }, [market, services]);

  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = services.filter(
    (s) => (category === ALL || s.category === category) && (!freeOnly || s.free)
      && words.every((w) => searchText(s).includes(w)),
  );
  const traps = services.filter((s) => s.trap).length;
  const free = services.filter((s) => s.free).length;
  const filtered = category !== ALL || freeOnly || words.length > 0;

  return (
    <>
      <Hero market={market} traps={traps} revealed={revealed} />
      <Toolbar
        query={query}
        onQuery={setQuery}
        freeOnly={freeOnly}
        onFreeOnly={() => setFreeOnly((f) => !f)}
        revealed={revealed}
        onReveal={() => setRevealed((r) => !r)}
      />
      <div className={`wrap layout${revealed ? " revealed" : ""}`}>
        {market && <Categories categories={categories} current={category} onPick={setCategory} />}
        <main className="listings">
          {error && (
            <p className="notice">
              The marketplace could not load its catalog. {error} Start the test site with{" "}
              <code>uvicorn test_site.app:app --port 4000</code> and reload.
            </p>
          )}
          {!market && !error && <p className="notice">Loading the catalog…</p>}
          {market && (
            <>
              <p className="tally" aria-live="polite">
                {revealed ? (
                  <><b>{traps} of {services.length}</b> listings are traps. The rest are honest.</>
                ) : (
                  <>{services.length} services: {free} free, the rest paid per call in {market.asset} on {market.network}.</>
                )}
                {filtered && ` Showing ${shown.length}.`}
              </p>
              {shown.length > 0 ? (
                <ul className="rows">
                  {shown.map((s) => (
                    <ServiceRow key={s.path} service={s} onOpen={() => setOpen(s)} />
                  ))}
                </ul>
              ) : (
                <p className="notice">
                  No service matches{query ? ` “${query}”` : ""} here. Try another word, turn off Free only or pick All
                  services.
                </p>
              )}
            </>
          )}
        </main>
      </div>
      {market && <Footer market={market} />}
      {open && <ServiceDrawer service={open} market={market} revealed={revealed} onClose={close} />}
    </>
  );
}
