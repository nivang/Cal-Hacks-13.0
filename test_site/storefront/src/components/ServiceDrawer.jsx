import { useEffect, useRef } from "react";
import AskAgent, { promptFor } from "./AskAgent.jsx";
import CopyButton from "./CopyButton.jsx";
import Description from "./Description.jsx";
import Price from "./Price.jsx";
import TrapNote from "./TrapNote.jsx";

function shortAddress(address) {
  return address && address.length > 14 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}

function pretty(body) {
  return typeof body === "string" ? body : JSON.stringify(body, null, 2);
}

const FOCUSABLE = 'button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])';

export default function ServiceDrawer({ service, market, revealed, onClose }) {
  const panel = useRef(null);
  const closeButton = useRef(null);

  useEffect(() => {
    const opener = document.activeElement;
    closeButton.current?.focus();
    document.body.classList.add("drawer-open");

    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "Tab" && panel.current) {  // keep focus inside the dialog
        const items = [...panel.current.querySelectorAll(FOCUSABLE)];
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("drawer-open");
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, [onClose]);

  const curl = `curl -i "${service.example_request}"`;
  const titleId = "drawer-title";

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside
        ref={panel}
        className={`drawer ${service.trap ? "is-trap" : "is-clean"}${revealed ? " revealed" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div className="drawer-head">
          <div>
            <p className="drawer-meta">{service.seller} <span aria-hidden="true">/</span> {service.category}</p>
            <h2 id={titleId} className="drawer-title">{service.name}</h2>
          </div>
          <button ref={closeButton} type="button" className="close" onClick={onClose} aria-label="Close">×</button>
        </div>

        <div className="drawer-body">
          <div className="drawer-price">
            <Price service={service} big />
            {service.escrow && <span className="escrow">Escrow: paid once the delivery checks out</span>}
          </div>
          <p className="drawer-desc"><Description text={service.description} /></p>
          {revealed && <TrapNote trap={service.trap} />}

          <h3>Endpoint</h3>
          <code className="endpoint endpoint-block">
            <span className="method">{service.method}</span> {service.path}
          </code>

          {service.params.length > 0 && (
            <>
              <h3>Parameters</h3>
              <table className="params">
                <thead>
                  <tr><th scope="col">Name</th><th scope="col">Example</th><th scope="col">What it is</th></tr>
                </thead>
                <tbody>
                  {service.params.map((p) => (
                    <tr key={p.name}>
                      <td><code>{p.name}</code></td>
                      <td>{p.example}</td>
                      <td>{p.description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          <h3>Example request</h3>
          <div className="codebox">
            <pre><code>{curl}</code></pre>
            <CopyButton text={curl} />
          </div>
          <p className="fine">
            {service.free
              ? "Free: the endpoint answers right away."
              : "Without payment the endpoint answers 402 with its x402 terms. Agents pay through Sentinel, never with their own keys."}
          </p>

          <h3>Example response</h3>
          <pre className="response"><code>{pretty(service.example_response)}</code></pre>

          <h3>Payment terms</h3>
          <dl className="terms">
            <dt>Network</dt><dd>{market.network}</dd>
            <dt>Asset</dt><dd>{market.asset}</dd>
            <dt>Price</dt><dd>{service.free ? "Free" : `$${service.price_usdc} per call`}</dd>
            <dt>Pays</dt>
            <dd>{service.pay_to ? <code title={service.pay_to}>{shortAddress(service.pay_to)}</code> : "Nobody, it is free"}</dd>
          </dl>

          <AskAgent prompt={promptFor(service)} />
        </div>
      </aside>
    </div>
  );
}
