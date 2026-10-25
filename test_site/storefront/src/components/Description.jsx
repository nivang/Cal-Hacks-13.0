// A listing's description, with call hints like ?company=<name> kept on one line as code.
const HINT = /(\?[a-z_]+=<[^>]+>(?:&[a-z_]+=<[^>]+>)*)/gi;

export default function Description({ text, className }) {
  const parts = text.split(HINT);
  return (
    <span className={className}>
      {parts.map((part, i) => (i % 2 === 1 ? <code key={i} className="hint">{part}</code> : part))}
    </span>
  );
}
