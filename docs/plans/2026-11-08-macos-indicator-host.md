# macOS read-only indicator host

## Observed problem

The macOS DLL builds, and the MCP client initializes and calls tools, but the
ChartStrategy DLL in `Strategies` has not been loaded in the tested ATAS X UI.
The same host successfully loads custom Indicators. The existing SIM recorder is
a separate, intentionally hidden indicator; applying it does not start AI Bridge.

## Implementation contract

- On macOS, expose `AI Bridge — read only` in the custom indicator list and install
  in `Indicators`. Back up the earlier Strategies copy during explicit migration.
- Reuse the existing market-data builders and GET API schemas. Keep the Windows
  ChartStrategy build and its installation script available without behavior changes.
- Start the local listener from the indicator lifecycle; stop it on disposal.
- The indicator build has no trading implementation. Reject HTTP mutations and
  account-position/order endpoints explicitly, rather than reporting fictitious flatness.
- Show listener/data status in settings and a small chart label. Report snapshot
  and market-event reception age; do not present capture time as exchange tick time.
- Keep the existing manual SIM-trade recorder and its journal unchanged.

## Increments and acceptance

1. Test the built host type and read-only request boundary; compile against the
   installed SDK and verify the legacy strategy build still compiles.
2. Add lifecycle, visible status and freshness. Exercise real HTTP on the isolated
   test host where possible; add installer migration regression coverage.
3. Install the new indicator, load it in the SIM BTCUSDT chart, and use the actual
   MCP tools to query status, quote, DOM, CVD, candles, footprint and profile.
4. Document verified limits and push only source, tests and documentation to the
   user's existing fork branch. Do not publish local accounts, records or vendor DLLs.
