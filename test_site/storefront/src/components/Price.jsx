export default function Price({ service, big = false }) {
  if (service.free) return <span className={`price price-free${big ? " price-big" : ""}`}>Free</span>;
  return (
    <span className={`price${big ? " price-big" : ""}`}>
      ${service.price_usdc}<span className="price-unit"> / call</span>
    </span>
  );
}
