export default function Footer({ market }) {
  return (
    <footer className="footer">
      <div className="wrap footer-inner">
        <section>
          <h2>For agents</h2>
          <p>
            Agents shop the <a href={market.catalog_url}>machine catalog</a> (x402 Bazaar discovery format) through
            Sentinel, which checks every listing against the owner's task before anything is paid. This page is for
            people.
          </p>
        </section>
        <section>
          <h2>How payment works</h2>
          <p>
            Each call is paid in {market.asset} on {market.network}, a testnet. A paid endpoint first answers 402 with
            its price and wallet; sellers marked Escrow get the money only after Sentinel checks what they delivered.
          </p>
        </section>
      </div>
    </footer>
  );
}
