import AskAgent, { BRIEFING } from "./AskAgent.jsx";

export default function Hero({ market, traps, revealed }) {
  return (
    <header className="hero wrap">
      <div className="hero-copy">
        <h1 className="wordmark">
          Malicious <span className="sticker">Marketplace</span>
        </h1>
        <p className="tagline">
          Pay per call APIs for AI agents. <strong>Some of them bite.</strong> Company data, news, court records and
          more, sold over x402. Sentinel decides which ones your agent may pay.
        </p>
        {market && (
          <p className={`hero-count${revealed ? " hero-count-revealed" : ""}`}>
            {revealed
              ? `${traps} of ${market.services.length} listings are out to get your agent`
              : `${market.services.length} services from ${market.sellers?.length ?? "several"} sellers`}
          </p>
        )}
      </div>
      <AskAgent prompt={BRIEFING} title="Try it with your agent" />
    </header>
  );
}
