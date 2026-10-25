export default function Toolbar({ query, onQuery, freeOnly, onFreeOnly, revealed, onReveal }) {
  return (
    <div className="bar">
      <div className="wrap bar-inner">
        <input
          className="search"
          type="search"
          placeholder="Search services: funding, court records, weather…"
          aria-label="Search services"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
        />
        <div className="bar-tools">
          <button type="button" className="toggle" role="switch" aria-checked={freeOnly} onClick={onFreeOnly}>
            <span className="switch switch-free" aria-hidden="true" />
            Free only
          </button>
          <button type="button" className="toggle" role="switch" aria-checked={revealed} onClick={onReveal}>
            <span className="switch switch-trap" aria-hidden="true" />
            Reveal traps
          </button>
        </div>
      </div>
    </div>
  );
}
