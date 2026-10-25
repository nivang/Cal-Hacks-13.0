// Shown only in reveal mode (CSS hides it otherwise): what a trap tries, or a quiet clean mark.
export default function TrapNote({ trap }) {
  if (!trap) return <p className="verdict verdict-clean">Clean listing</p>;
  return (
    <div className="verdict verdict-trap">
      <p className="verdict-kind">Trap: {trap.kind}</p>
      <p>{trap.tries}</p>
      <p><b>Sentinel:</b> {trap.sentinel}</p>
    </div>
  );
}
