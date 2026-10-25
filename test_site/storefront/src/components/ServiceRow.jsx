import Description from "./Description.jsx";
import Price from "./Price.jsx";
import TrapNote from "./TrapNote.jsx";

export default function ServiceRow({ service, onOpen }) {
  const kind = service.trap ? "is-trap" : "is-clean";
  return (
    <li className={`row ${kind}`}>
      <button type="button" className="row-open" onClick={onOpen} aria-haspopup="dialog">
        <span className="row-main">
          <span className="row-name">{service.name}</span>
          <span className="row-meta">
            {service.seller} <span aria-hidden="true">/</span> {service.category}
          </span>
          <Description className="row-desc" text={service.description} />
        </span>
        <span className="row-side">
          <Price service={service} />
          <code className="endpoint">
            <span className="method">{service.method}</span> {service.path.replace("/test-site", "")}
          </code>
          {service.escrow && <span className="escrow">Escrow</span>}
        </span>
      </button>
      <TrapNote trap={service.trap} />
    </li>
  );
}
