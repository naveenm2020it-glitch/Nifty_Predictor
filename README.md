# NIFTY Next-Day AI Intelligence System

Institutional-style research terminal for next-session NIFTY 50 analysis.

## Current UI scaffold
- Nine screens: Market Overview, Next-Day Prediction, Options Intelligence, FII & Participant Flow, Futures & Volatility, Pattern Intelligence, Scenario Planner, NSE Data Centre and Model Performance.
- Responsive desktop-first layout, light theme and optional dark mode.
- Integrity-first display: no fabricated prices, predictions, probabilities, performance metrics or backtest results.
- This is the initial frontend scaffold. Market-data ingestion, five-year historical storage, AI model training, walk-forward validation, evening scheduling, and PDF/Excel generation remain to be implemented.

## Local development
```bash
npm install
npm run dev
```

## Production build
```bash
npm run build
```

## Render static-site settings
- Build command: `npm install && npm run build`
- Publish directory: `dist`
- Branch: `main`

## Data integrity
Only publish market data with source provenance, exchange/session timestamps, validation status and freshness metadata. Missing or stale data must remain visibly unavailable. Model probabilities and performance metrics require chronological walk-forward and out-of-sample validation.

Research software only. No broker integration or order execution.
