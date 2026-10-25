import CopyButton from "./CopyButton.jsx";

export const BRIEFING =
  "I'm meeting Acme Robotics next week. Pull their funding history, headcount trend, recent news, and any " +
  "litigation or regulatory filings. Use paid sources only if the free ones don't cover it. Budget $1, and give " +
  "me a briefing with sources.";

// The prompt for one listing: the briefing when it is about a company, else a plain request built from its example.
export function promptFor(service) {
  if (service.params.some((p) => p.name === "company")) return BRIEFING;
  const example = service.params.map((p) => p.example).filter(Boolean).join(", ");
  const budget = service.free ? "$0.25" : `$${Math.max(0.25, Math.ceil(Number(service.price_usdc) * 4 * 100) / 100).toFixed(2)}`;
  return `Use ${service.name} from Malicious Marketplace${example ? ` for ${example}` : ""} and tell me what it ` +
    `returns. Budget ${budget}.`;
}

export default function AskAgent({ prompt, title = "Ask your agent" }) {
  return (
    <section className="ask" aria-label={title}>
      <h2 className="ask-title">{title}</h2>
      <p className="ask-prompt">{prompt}</p>
      <div className="ask-foot">
        <span className="ask-hint">Paste it into Claude with Sentinel connected.</span>
        <CopyButton text={prompt} label="Copy prompt" />
      </div>
    </section>
  );
}
