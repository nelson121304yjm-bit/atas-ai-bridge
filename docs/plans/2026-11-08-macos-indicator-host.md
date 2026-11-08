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

## Acceptance evidence (2026-11-08)

- Version `0.3.0-macos.2` loaded in ATAS X 8.0.15.663 on the user's SIM BTCUSDT chart.
- The status label rendered fully after replacing the fixed-height text clipping
  rectangle with a measured background and the SDK point-based DrawString overload.
- Actual MCP queries returned fresh status, quote, DOM (15 levels each side),
  candles (300), CVD (300), current footprint, profile (200 bars, top 10 levels),
  and immutable settings. No order or settings mutation was requested.
- One on-demand historical candles query timed out awaiting a chart tick; retry
  succeeded. Default snapshots remained usable. This limitation is documented.
- Data interpretation notes now state chart aggregation, window-relative CVD,
  non-atomic endpoint sampling and DOM coverage limits.
- The independent SIM journal receiver was restarted using its existing config;
  its health remained connected after the indicator settings window closed.
  No new simulated fill was requested or used as part of this bridge acceptance.
- Mac build: 0 warnings/errors; Python tests: 22; indicator DLL contract: 34 checks.
  Legacy conditional build: 0 warnings/errors; strategy DLL contract: 2 checks.
