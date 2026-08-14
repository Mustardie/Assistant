"""StockLLM command-line interface (V1-V4).

Subcommands:
  download        fetch market data into data/raw/ (yfinance)
  prep            build the causal feature matrix -> datasets/feature_matrix.csv.gz
  train           fit the numerical forecaster on the chronological train split
  backtest        walk-forward backtest over the unseen test window
  evaluate        metrics/calibration report from the latest backtest results
  holdout-eval    FINAL frozen-holdout evaluation of the selected V3 variant (C)
  predict         one forecast + analysis for a ticker (Qwen optional; --horizon)
  save            add a ticker to the daily watchlist (JARVIS: "Save NVIDIA")
  report          daily reports for all watchlist tickers (JARVIS entry)
  paper           paper-trade the backtest signals (simulation only)
  news            add/list sourced web research items (JARVIS research)
  experiment      V3/V4 feature-set variants A/B/C/D/E on the dev window
  track/untrack   V4 monitoring watchlist + scheduled prediction loop
  watchlist       show both watchlists (daily-report + monitoring)
  tracking-report V4 ledger: open/resolved prediction outcomes
  jarvis          V4 NL -> intent -> run (--request)
  build-dataset   build the synthetic reasoning dataset for future Qwen SFT
  finetune        QLoRA fine-tuning foundation script (needs extra deps)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pandas as pd

import config
from utils.logging import get_logger

log = get_logger(__name__)


def _ticker_list(raw: str | None) -> list[str]:
    if not raw:
        return config.DEFAULT_TICKERS
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def _load_matrix() -> pd.DataFrame:
    from marketdata.features import load_matrix
    return load_matrix()


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def cmd_download(args) -> int:
    tickers = _ticker_list(args.tickers)
    if not tickers and not args.benchmarks:
        raise SystemExit("no tickers given (use --tickers or config.DEFAULT_TICKERS)")
    all_tickers = list(dict.fromkeys(tickers + args.benchmarks))
    from marketdata import loader
    frames = loader.download_all(all_tickers, args.start or config.DOWNLOAD_START,
                                 args.end or config.DOWNLOAD_END, force=args.force)
    log.info("downloaded %d/%d symbols", len(frames), len(all_tickers))
    return 0


def cmd_prep(args) -> int:
    from marketdata import loader
    from marketdata.features import make_feature_matrix, get_features
    from research.layers import load_layers
    tickers = _ticker_list(args.tickers)
    frames, benches = loader.load_market_data(tickers, args.start, args.end,
                                              benchmarks=args.benchmarks)
    if not frames:
        raise SystemExit("no market data found -- run `python main.py download` first")
    fund, news, sectors = load_layers(tickers)
    matrix = make_feature_matrix(frames, benches, fundamentals=fund, news=news,
                                 sectors=sectors, horizon=args.horizon,
                                 with_candles=args.candles)
    out = (config.DATASETS_DIR / "feature_matrix.csv.gz" if args.horizon == config.HORIZON
           else config.DATASETS_DIR / f"feature_matrix_{args.horizon}d.csv.gz")
    matrix.to_csv(out, index=False)
    layers_used = {
        "fundamentals": sum(1 for f in fund.values() if not f.empty),
        "news": sum(1 for n in news.values() if not n.empty),
        "sectors": len(sectors),
        "candles": bool(args.candles),
    }
    meta = {
        "tickers": sorted(frames.keys()),
        "benchmarks": sorted(benches.keys()),
        "rows": int(len(matrix)),
        "n_features": len(get_features()),
        "horizon": args.horizon,
        "date_min": str(matrix["date"].min().date()),
        "date_max": str(matrix["date"].max().date()),
        "split": {"train_end": config.TRAIN_END, "val_end": config.VAL_END,
                  "dev": f"{config.DEV_START}..{config.DEV_END}",
                  "holdout_start": config.HOLDOUT_START},
        "features": get_features(),
        "layers": layers_used,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_out = (config.DATASETS_DIR / "meta.json" if args.horizon == config.HORIZON
                else config.DATASETS_DIR / f"meta_{args.horizon}d.json")
    with open(meta_out, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    log.info("feature matrix: %d rows, %d features -> %s (layers %s)", len(matrix),
             len(get_features()), out, layers_used)
    return 0


def cmd_train(args) -> int:
    from marketdata.features import get_features
    from marketdata.splits import chronological_split
    from forecasting.numerical import NumericalForecaster
    from evaluation import metrics
    h = args.horizon
    if h != config.HORIZON or args.candles:
        # horizon-specific model: build the matrix fresh for that horizon
        from marketdata import loader
        from marketdata.features import make_feature_matrix
        from research.layers import load_layers
        tickers = _ticker_list(args.tickers)
        frames, benches = loader.load_market_data(tickers, args.start, args.end,
                                                  benchmarks=args.benchmarks)
        if not frames:
            raise SystemExit("no market data found -- run `python main.py download` first")
        fund, news, sectors = load_layers(tickers)
        matrix = make_feature_matrix(frames, benches, fundamentals=fund, news=news,
                                     sectors=sectors, horizon=h, with_candles=args.candles)
    else:
        matrix = _load_matrix()
    train, val, test = chronological_split(matrix, config.TRAIN_END, config.VAL_END)
    log.info("train rows=%d | val rows=%d | test rows=%d", len(train), len(val), len(test))
    features = [f for f in get_features() if f in matrix.columns]
    model = NumericalForecaster(features, horizon=h).fit(train)
    from inference.pipeline import save_model
    out = save_model(model, h)
    log.info("saved model -> %s (version %s)", out, model.version)
    if len(val):
        preds = model.predict(val)
        va = val.copy().reset_index(drop=True)
        va["pred_ret"] = preds["pred_ret"].values
        va["prob_up"] = preds["prob_up"].values
        va["actual_ret"] = va[model.label_col]
        va["direction_correct"] = (va["prob_up"] >= 0.5) == (va[model.label_col] > 0)
        log.info("validation -> directional accuracy %.1f%% | MAE %.2fpp | "
                 "calibration error %.3f",
                 metrics.directional_accuracy(va) * 100, metrics.mae(va) * 100,
                 metrics.calibration_error(va))
    log.info("dev window %s..%s is for model selection; holdout %s onwards is "
             "frozen and evaluated once (see `backtest --window`)",
             config.DEV_START, config.DEV_END, config.HOLDOUT_START)
    return 0


def cmd_backtest(args) -> int:
    from backtesting.engine import BacktestEngine
    from marketdata.features import load_matrix
    matrix = load_matrix(horizon=args.horizon)
    engine = BacktestEngine(matrix, window=args.window,
                            refit_every=args.refit_every or config.REFIT_EVERY_DAYS,
                            scope=args.scope, target=args.target,
                            horizon=args.horizon)
    results = engine.run()
    if results.empty:
        raise SystemExit("no forecasts recorded -- check the window dates")
    path = BacktestEngine.save(results, args.window)
    log.info("recorded %d forecasts -> %s", len(results), path)
    log.info("directional accuracy %.1f%% | MAE %.2fpp",
             (results["direction_correct"].mean() * 100),
             (results["abs_error"].mean() * 100))
    return 0


def cmd_evaluate(args) -> int:
    from evaluation import metrics
    import glob
    base = config.RESULTS_HOLDOUT_DIR if args.window == "holdout" else config.RESULTS_DEV_DIR
    files = sorted(glob.glob(str(base / "backtest_*.csv")))
    if not files:
        raise SystemExit(f"no backtest results found for window '{args.window}' "
                         f"in {base} -- run `python main.py backtest`")
    path = files[-1]
    df = pd.read_csv(path)
    log.info("loading %s (%d forecasts)", path, len(df))
    title = f"StockLLM evaluation [{args.window}] -- {path.split('backtest_')[1][:-4]}"
    report = metrics.summarize(df, title)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.LOGS_DIR / f"evaluation_{args.window}_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    log.info("report saved -> %s", out)
    return 0


def cmd_holdout_eval(args) -> int:
    """FINAL evaluation of the selected V3 variant (C) on the frozen holdout.

    Frozen by design: variant, feature set, scope, target, thresholds and
    selectivity rules are read from config/selection constants -- nothing is
    tuned from holdout observations.  Every training row used by the
    walk-forward refits is audited to be dated before HOLDOUT_START.
    """
    from backtesting.engine import BacktestEngine
    from evaluation import metrics, selectivity
    from evaluation.experiment import (_fmt_paper, _markdown_table,
                                       _paper_summary, _tier_table,
                                       variant_features)
    from marketdata import loader
    from marketdata.features import get_features, make_feature_matrix
    from marketdata.splits import allowed_train_rows
    from paper_trading.simulator import PaperPortfolio
    from research.layers import load_layers

    tickers = _ticker_list(args.tickers)
    frames, benches = loader.load_market_data(tickers, args.start, args.end,
                                              benchmarks=args.benchmarks)
    if not frames:
        raise SystemExit("no market data found -- run `python main.py download` first")
    fund, news, sectors = load_layers(tickers)
    matrix = make_feature_matrix(frames, benches, fundamentals=fund, news=news,
                                 sectors=sectors)
    vfeats = variant_features(get_features(), "C")  # exactly as selected
    scope, target = config.EXPERIMENT_SCOPE, config.EXPERIMENT_TARGET

    engine = BacktestEngine(matrix, window="holdout", scope=scope, target=target,
                            features_override=vfeats)
    results = engine.run()
    results = selectivity.apply_tiers(results)
    path = BacktestEngine.save(results, "holdout")

    # -- audit: holdout isolation --------------------------------------------
    # (a) the first holdout refit must be trained exclusively on rows dated
    #     before HOLDOUT_START (first forecasts are fully out-of-sample);
    # (b) every training row at every refit must have its label window
    #     (HORIZON trading days after the row's own date) complete at the
    #     cutoff -- the engine's no-lookahead contract, checked independently.
    cutoffs = sorted(pd.to_datetime(results["refit_cutoff"].unique()))
    first_train = allowed_train_rows(matrix, cutoffs[0], config.HORIZON)
    max_first_train = (first_train["date"].max()
                       if not first_train.empty else None)
    if (max_first_train is not None
            and max_first_train >= pd.Timestamp(config.HOLDOUT_START)):
        raise SystemExit("AUDIT FAILED: the first holdout refit was trained on "
                         "holdout-dated rows")
    rank_dates = {}
    for _, g in matrix.groupby("ticker", sort=False):
        rank_dates[g["ticker"].iloc[0]] = dict(zip(g["row_rank"], g["date"]))
    violations = []
    for c in cutoffs:
        tr = allowed_train_rows(matrix, c, config.HORIZON)
        for ticker, g in tr.groupby("ticker"):
            dates = rank_dates[ticker]
            for r in g["row_rank"]:
                end = dates.get(r + config.HORIZON)
                if end is None or end > c:
                    violations.append((ticker, r, end))
    if violations:
        raise SystemExit(f"AUDIT FAILED: {len(violations)} training rows with "
                         "incomplete labels at their refit cutoff (lookahead)")
    audit = (f"first holdout refit trained only on rows dated before "
             f"{config.HOLDOUT_START} (max train row: "
             f"{max_first_train.date() if max_first_train is not None else 'n/a'}); "
             f"all {len(cutoffs)} refits verified no-lookahead (each training "
             "label ends at or before its refit cutoff); selection used the "
             "dev window only; holdout data read-only")

    # -- portfolio simulations (indicative only) ------------------------------
    frames, _ = loader.load_market_data(sorted(results["ticker"].unique()))
    prices = {t: f["close"] for t, f in frames.items()}
    all_trades = results[results["prob_up"] >= config.BUY_PROB_THRESHOLD]
    hi_sig = results[results["tier"].isin(["HIGH", "MEDIUM"])]
    paper_all = _paper_summary(all_trades, prices)
    paper_tier = _paper_summary(hi_sig, prices)
    paper_stats_all = PaperPortfolio().run(all_trades, prices)
    paper_stats_tier = PaperPortfolio().run(hi_sig, prices)

    title = (f"StockLLM V3 FINAL holdout evaluation -- Variant C "
             f"(scope={scope}, target={target}, window from "
             f"{config.HOLDOUT_START})")
    lines = [f"# {title}", "",
             "> **Frozen configuration.** Variant C exactly as selected on the "
             "dev window: feature set, scope, target, thresholds and "
             "selectivity rules are unchanged (config.EXPERIMENT_*, "
             "config.SELECTIVITY_*, config.BUY_PROB_THRESHOLD). Nothing is "
             "retrained or tuned from holdout observations.",
             "",
             "> **Holdout isolation audit:** " + audit + ".",
             "",
             "> All portfolio numbers below are **SIMULATIONS** -- indicative "
             "backtests with transaction costs, not trading recommendations.",
             ""]
    lines += ["## Metrics (all forecasts)", ""]
    report = metrics.summarize(results, title)
    lines.append(report.split("\n", 1)[1].lstrip())
    lines += ["", "## Selectivity tiers (evidence coverage + edge gating)",
              "",
              "> Tiers are applied post-hoc with the exact rules used in "
              "selection (selectivity.signal_tier).  Coverage = fraction of "
              "fundamentals/news/sector evidence available at forecast time.",
              ""]
    lines.append(_markdown_table(_tier_table(results), {
        "n": "{:.0f}", "coverage_share": "{:.2f}", "dir_acc": "{:.3f}",
        "base_rate": "{:.3f}", "mae": "{:.4f}", "mean_ret": "{:+.4f}"}))
    lines += ["", "## Portfolio simulations (indicative only)", "",
              f"- All signals P(up) >= {config.BUY_PROB_THRESHOLD}: "
              f"{_fmt_paper(paper_all)}",
              f"- HIGH+MEDIUM tier only: {_fmt_paper(paper_tier)}", ""]
    for name, stats in (("all signals", paper_stats_all),
                        ("HIGH+MEDIUM", paper_stats_tier)):
        if stats.get("n_round_trips", 0) == 0:
            continue
        bh = stats.get("buy_and_hold", {})
        lines.append(
            f"- Buy-and-hold benchmark vs **{name}** sim: equal-weight "
            f"buy-and-hold {bh.get('net_pct', float('nan')):+.2f}% "
            f"(gross {bh.get('gross_pct', float('nan')):+.2f}%) over the "
            f"{bh.get('n', 0)} traded tickers for the simulated period")
    report = "\n".join(lines)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.LOGS_DIR / f"holdout_eval_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    log.info("holdout results -> %s", path)
    log.info("holdout report saved -> %s", out)
    return 0


def cmd_diagnose(args) -> int:
    """Dev-window diagnostics: variant comparison + permutation importance.

    Runs on the DEV window only.  The final holdout is never used here.
    """
    from evaluation import diagnostics
    from backtesting.engine import BacktestEngine
    from marketdata.features import get_features
    matrix = _load_matrix()
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    lines = [f"# StockLLM diagnostics (dev window only, {datetime.now():%Y-%m-%d %H:%M})",
             "",
             "> Model-selection window only. The frozen final holdout "
             f"({config.HOLDOUT_START} onwards) was NOT used for any decision here.",
             "",
             "## Variant comparison (scope x target, dev window)", ""]
    table = diagnostics.variant_comparison(matrix, scopes=scopes, targets=targets)
    lines.append(diagnostics.to_markdown_table(table))
    lines += ["", "## Permutation importance (pooled, out-of-sample, walk-forward)", ""]
    engine = BacktestEngine(matrix, window="dev", scope="pooled", target="raw")
    results = engine.run()
    perm = diagnostics.permutation_importance(results, matrix, engine,
                                              get_features(), n_perm=args.n_perm)
    lines.append(diagnostics.to_markdown_table(perm.head(args.top)))
    if not perm.empty:
        weakest = perm.tail(args.top).iloc[::-1]
        lines += ["", "### Weakest features (negative/zero importance)", ""]
        lines.append(diagnostics.to_markdown_table(weakest))
    report = "\n".join(lines)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.LOGS_DIR / f"diagnose_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    log.info("diagnostics saved -> %s", out)
    return 0


# ---------------------------------------------------------------------------
# inference
# ---------------------------------------------------------------------------
def cmd_predict(args) -> int:
    from inference import pipeline
    try:
        forecast = pipeline.forecast_ticker(args.ticker, as_of=args.as_of,
                                            benchmark=args.benchmark,
                                            horizon=args.horizon)
    except Exception as exc:
        raise SystemExit(f"{args.ticker}: {exc}")
    digest = pipeline.digest_row(pipeline.feature_row_latest(args.ticker))
    text = pipeline.generate_analysis_report(args.ticker, forecast, digest,
                                             use_llm=not args.no_llm)
    if args.json:
        print(json.dumps({"forecast": forecast.to_dict(), "report": text}, indent=2))
    else:
        print(text)
    return 0


def cmd_save(args) -> int:
    from inference import pipeline
    forecast = pipeline.forecast_ticker(args.ticker)
    entry = pipeline.save_stock(args.ticker, forecast)
    log.info("saved %s to watchlist at %.2f (7d exp %.2f%%, P(up) %.0f%%)",
             entry["ticker"], entry["added_price"],
             entry["forecast"]["expected_return"] * 100,
             entry["forecast"]["prob_up"] * 100)
    return 0


def cmd_report(args) -> int:
    from inference import pipeline
    entries = pipeline.load_watchlist()
    if not entries:
        raise SystemExit(
            "watchlist is empty -- use `python main.py save NVDA` "
            "(or JARVIS: \"Save NVIDIA\") first"
        )
    if args.tickers:
        wanted = {t.upper() for t in args.tickers.split(",")}
        entries = [e for e in entries if e["ticker"] in wanted]
    reports = []
    for entry in entries:
        rep = pipeline.daily_report(entry, use_llm=not args.no_llm)
        reports.append(rep)
        if "error" in rep:
            log.warning("%s: %s", rep["ticker"], rep["error"])
        else:
            log.info("%s -> %s", rep["ticker"], rep["status"])
            if args.json:
                print(json.dumps(rep, indent=2, default=str))
            else:
                print("\n" + rep["report_text"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.LOGS_DIR / f"daily_report_{stamp}.json"
    out.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    log.info("daily report saved -> %s", out)
    return 0


def cmd_paper(args) -> int:
    from backtesting.engine import BacktestEngine
    from inference import pipeline
    from marketdata import loader
    from paper_trading.simulator import PaperPortfolio
    matrix = _load_matrix()
    engine = BacktestEngine(matrix, window=args.window,
                            refit_every=args.refit_every or config.REFIT_EVERY_DAYS,
                            scope=args.scope, target=args.target)
    signals = engine.run()
    if signals.empty:
        raise SystemExit("no signals -- nothing to paper trade")
    frames, _ = loader.load_market_data(sorted(signals["ticker"].unique()))
    prices = {t: f["close"] for t, f in frames.items()}
    portfolio = PaperPortfolio()
    stats = portfolio.run(signals, prices)
    md = portfolio.summary_markdown(stats)
    print(md)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    (config.LOGS_DIR / f"paper_{stamp}.md").write_text(md, encoding="utf-8")
    if stats:
        stats["trades"].to_csv(config.LOGS_DIR / f"paper_trades_{stamp}.csv", index=False)
        stats["equity"].to_csv(config.LOGS_DIR / f"paper_equity_{stamp}.csv",
                               header=["equity"])
        log.info("paper trading files saved under logs/ (%s)", stamp)
    return 0


# ---------------------------------------------------------------------------
# research / training
# ---------------------------------------------------------------------------
def cmd_fetch_news(args) -> int:
    """Historical news ingestion only (EDGAR filings / NSE announcements).

    Resume-safe: already-fetched windows are skipped; failed windows are
    reported and re-running the command resumes where it left off.
    """
    from news.service import fetch_and_cache
    from research.audit_store import AuditStore
    tickers = _ticker_list(args.tickers)
    audit = AuditStore()
    failures = 0
    try:
        for t in tickers:
            try:
                _df, stats = fetch_and_cache(
                    t, start=args.start or config.NEWS_FETCH_START,
                    force=args.force, audit=audit)
                if stats["unsupported"]:
                    log.warning("%s: no news provider available (skipped)", t)
                elif stats["failed_windows"]:
                    failures += 1
                    log.warning("%s: %d failed windows%s (resume-safe)", t,
                                len(stats["failed_windows"]),
                                " -- aborted early" if stats["aborted"] else "")
            except Exception as exc:
                failures += 1
                log.warning("%s: news fetch failed: %s", t, exc)
    finally:
        audit.close()
    if failures:
        log.warning("news fetch finished with %d ticker(s) having failures", failures)
    else:
        log.info("news fetch complete for %d tickers", len(tickers))
    return 0 if failures == 0 else 1


def cmd_data_download(args) -> int:
    """V3 data layers: fundamentals, news (EDGAR/NSE), sector indices + audit."""
    from datetime import datetime as _dt
    from fundamentals.fetcher import fetch_and_cache_fundamentals
    from marketdata import loader, sector as sector_mod
    from news.service import fetch_and_cache
    from research.audit_store import AuditStore
    tickers = _ticker_list(args.tickers)
    audit = AuditStore()
    now = _dt.utcnow().isoformat(timespec="seconds")
    try:
        all_syms = list(dict.fromkeys(tickers + list(args.benchmarks)))
        frames = loader.download_all(all_syms, args.start or config.DOWNLOAD_START,
                                     args.end)
        for t, df in frames.items():
            audit.record_many([(t, "series", str(df.index[0].date()), "yfinance",
                                {"series": "price", "rows": len(df),
                                 "end": str(df.index[-1].date())}, now)])
        for t in tickers:
            try:
                fetch_and_cache_fundamentals(t, force=args.force, audit=audit)
            except Exception as exc:
                log.warning("%s: fundamentals failed: %s", t, exc)
        for t in tickers:
            try:
                _df, stats = fetch_and_cache(
                    t, start=args.news_start or config.NEWS_FETCH_START,
                    force=args.force, audit=audit)
                if stats["failed_windows"]:
                    log.warning(
                        "%s: news collection had %d failed windows%s -- "
                        "resume by re-running the same command",
                        t, len(stats["failed_windows"]),
                        " (run aborted early)" if stats["aborted"] else "")
            except Exception as exc:
                log.warning("%s: news failed: %s", t, exc)
        sector_mod.ensure_sector_data(force=args.force)
        for sym, df in sector_mod.load_sectors().items():
            audit.record_many([(sym, "series", str(df.index[0].date()), "yfinance",
                                {"series": "sector", "rows": len(df),
                                 "end": str(df.index[-1].date())}, now)])
    finally:
        audit.close()
    log.info("data layers complete for %d tickers (audit -> %s)",
             len(tickers), config.DATA_DIR / "audit.sqlite")
    return 0


def cmd_audit(args) -> int:
    """Coverage/provenance report from the audit store (V3 requirement)."""
    from research.audit_store import AuditStore
    audit = AuditStore()
    try:
        counts = audit.count_by_kind()
        lines = [f"# StockLLM data coverage audit ({datetime.now():%Y-%m-%d %H:%M})",
                 "",
                 "> Every external information item (fundamental statement, earnings "
                 "event, news article, series download) is recorded here with its "
                 "availability timestamp and source.",
                 "",
                 "## Item counts by kind", ""]
        for kind in ("fundamental", "earnings", "news", "series"):
            lines.append(f"- **{kind}**: {counts.get(kind, 0)}")
        for kind, label in (("fundamental", "Fundamental statements"),
                            ("earnings", "Earnings events"),
                            ("news", "News articles")):
            first = audit.first_seen(kind)
            if first.empty:
                continue
            lines += ["", f"## {label}: first available date per ticker", "",
                      "| ticker | first item |", "|--------|------------|"]
            for t in sorted(first.index):
                lines.append(f"| {t} | {str(first[t])[:10]} |")
        n_tickers = len(counts)
        lines += ["", "## Dev-window coverage (news/fundamentals, 2023-01-01+)", ""]
        for kind, label in (("news", "news"), ("fundamental", "fundamentals")):
            cov = audit.coverage(kind, start=config.DEV_START)
            lines.append(f"- **{label}** rows since {config.DEV_START}: "
                         f"{int(cov.sum()) if len(cov) else 0} across "
                         f"{len(cov)} tickers")
        lines += [
            "",
            "> A low news count before the fetch start "
            f"({config.NEWS_FETCH_START}) means those rows have NaN news "
            "features -- the selectivity layer marks them as low-evidence.",
        ]
        report = "\n".join(lines)
    finally:
        audit.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.LOGS_DIR / f"audit_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    log.info("audit report saved -> %s", out)
    return 0


def cmd_experiment(args) -> int:
    """V3 experiment: feature-set variants A/B/C on the dev window."""
    from evaluation import experiment as exp_mod
    from marketdata import loader
    from marketdata.features import make_feature_matrix
    from research.layers import load_layers
    tickers = _ticker_list(args.tickers)
    frames, benches = loader.load_market_data(tickers, args.start, args.end,
                                              benchmarks=args.benchmarks)
    if not frames:
        raise SystemExit("no market data found -- run `python main.py download` first")
    fund, news, sectors = load_layers(tickers)
    missing_fund = [t for t in tickers if fund.get(t, pd.DataFrame()).empty]
    missing_news = [t for t in tickers if news.get(t, pd.DataFrame()).empty]
    if missing_fund or missing_news:
        log.warning("layers missing (features will be NaN): fund=%s news=%s",
                    missing_fund, missing_news)
    matrix = make_feature_matrix(frames, benches, fundamentals=fund, news=news,
                                 sectors=sectors, with_candles=True)
    report = exp_mod.run_variants(matrix, variants=config.EXPERIMENT_VARIANTS,
                                  scope=args.scope, target=args.target)
    print(report)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.LOGS_DIR / f"experiment_v3_{stamp}.md"
    out.write_text(report, encoding="utf-8")
    log.info("experiment report saved -> %s", out)
    return 0


# ---------------------------------------------------------------------------
# V4: monitoring + JARVIS intents
# ---------------------------------------------------------------------------
def cmd_track(args) -> int:
    from monitoring.service import (add_to_watchlist, load_monitor_watchlist,
                                    run_monitor)
    watchlist = load_monitor_watchlist()
    if args.tickers:
        for t in args.tickers:
            entry = add_to_watchlist(watchlist, t, interval_min=args.interval,
                                     horizon=args.horizon)
            log.info("tracking %s every %d min (horizon %s)", entry["ticker"],
                     entry["interval_min"], entry["horizon"])
        return 0
    if not watchlist:
        raise SystemExit("no stocks tracked -- use `track --tickers ...` first")
    return run_monitor(watchlist, interval_min=args.interval, once=args.once,
                       max_cycles=args.cycles)


def cmd_untrack(args) -> int:
    from monitoring.service import load_monitor_watchlist, remove_from_watchlist
    watchlist = load_monitor_watchlist()
    removed = [t for t in args.tickers if remove_from_watchlist(watchlist, t)]
    if removed:
        log.info("untracked: %s", ", ".join(removed))
    else:
        log.info("none removed (not tracked: %s)", ", ".join(args.tickers))
    return 0


def cmd_watchlist(args) -> int:
    from inference import pipeline
    from monitoring.service import load_monitor_watchlist
    daily = pipeline.load_watchlist()
    monitor = load_monitor_watchlist()
    lines = ["## Daily-report watchlist (`save`/`report`)", ""]
    for w in daily:
        fc = w.get("forecast") or {}
        lines.append(f"- {w['ticker']} (added {w.get('added_on', '?')}, "
                     f"horizon {fc.get('horizon', '7d')})")
    if not daily:
        lines.append("  (empty)")
    lines += ["", "## Monitoring watchlist (`track`/`untrack --tickers ...`)", ""]
    for w in monitor:
        lines.append(f"- {w['ticker']} (interval {w['interval_min']}m, "
                     f"horizon {w['horizon']})")
    if not monitor:
        lines.append("  (empty)")
    print("\n".join(lines))
    return 0


def cmd_tracking_report(args) -> int:
    from monitoring.ledger import PredictionLedger
    from monitoring.service import tracking_report_markdown
    from marketdata import loader
    ledger = PredictionLedger(config.TRACKING_DB)
    try:
        tickers = ledger.tickers()
        if not tickers:
            print("# Prediction tracking report\n\nNo predictions recorded yet "
                  "-- use `python main.py track --tickers ...`.")
            return 0
        frames, _ = loader.load_market_data(tickers)
        prices = {t: f["close"] for t, f in frames.items()}
        resolved = ledger.resolve_due(prices)
        if resolved:
            log.info("resolved %d due predictions", resolved)
        print(tracking_report_markdown(ledger))
    finally:
        ledger.close()
    return 0


def cmd_jarvis(args) -> int:
    """JARVIS-facing command: natural language -> structured intent -> run it."""
    from jarvis.intents import IntentError, describe, parse_request
    try:
        intent = parse_request(args.request)
    except IntentError as exc:
        if args.json:
            print(json.dumps({"action": "error", "error": str(exc)}, indent=2))
        else:
            print(str(exc))
        return 1
    result = run_intent(intent)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print(result.get("text", json.dumps(result, default=str)))
    return 0


def run_intent(intent) -> dict:
    """Execute a parsed intent; returns a JSON-safe result dict."""
    from inference import pipeline
    from monitoring.ledger import PredictionLedger
    from monitoring.service import (add_to_watchlist, load_monitor_watchlist,
                                    remove_from_watchlist, tracking_report_markdown)
    action = intent.action
    if action == "watchlist":
        return {"action": "watchlist",
                "text": "\n".join(f"- {e['ticker']} (interval {e['interval_min']}m, "
                                  f"horizon {e['horizon']})"
                                  for e in load_monitor_watchlist())
                        or "nothing tracked yet"}
    if action == "untrack":
        watchlist = load_monitor_watchlist()
        removed = [t for t in intent.tickers if remove_from_watchlist(watchlist, t)]
        return {"action": "untrack", "removed": removed,
                "text": f"untracked {', '.join(removed) or 'nothing'}."}
    if action == "track":
        watchlist = load_monitor_watchlist()
        entries = [add_to_watchlist(watchlist, t, interval_min=intent.interval_min
                                    or config.DEFAULT_TRACK_INTERVAL_MIN,
                                    horizon=intent.horizon) for t in intent.tickers]
        return {"action": "track", "tickers": [e["ticker"] for e in entries],
                "text": "tracking " + ", ".join(e["ticker"] for e in entries) +
                        (f" every {intent.interval_min} min"
                         if intent.interval_min else "") +
                        (f" (horizon {intent.horizon})" if intent.horizon else "") + "."}
    if action == "tracking":
        ledger = PredictionLedger(config.TRACKING_DB)
        try:
            report = tracking_report_markdown(ledger)
        finally:
            ledger.close()
        return {"action": "tracking", "text": report}
    # predict (default)
    forecasts = []
    for ticker in intent.tickers:
        try:
            fc = pipeline.forecast_ticker(ticker, horizon=intent.horizon)
            forecasts.append({"ticker": ticker, "as_of": fc.as_of_date,
                              "price": fc.price,
                              "expected_return": fc.expected_return,
                              "prob_up": fc.prob_up, "direction": fc.direction,
                              "horizon": fc.horizon, "status": fc.confidence_level})
        except Exception as exc:
            forecasts.append({"ticker": ticker, "error": str(exc)})
    text = _intent_forecast_text(forecasts)
    return {"action": "predict", "forecasts": forecasts, "text": text}


def _intent_forecast_text(forecasts: list[dict]) -> str:
    lines = []
    for fc in forecasts:
        if "error" in fc:
            lines.append(f"{fc['ticker']}: {fc['error']}")
            continue
        lines.append(
            f"{fc['ticker']}: {fc['direction']} over {fc['horizon']} "
            f"(P(up) {fc['prob_up'] * 100:.0f}%, expected "
            f"{fc['expected_return'] * 100:+.1f}%, "
            f"price {fc['price']:,.2f} as of {fc['as_of']}, "
            f"confidence {fc['status']})")
    return "\n".join(lines)


def cmd_news(args) -> int:
    from research.news_store import NewsStore
    store = NewsStore()
    try:
        if args.action == "add":
            if not args.text:
                raise SystemExit("--text required for `news add`")
            idx = store.add(args.ticker, args.text, source=args.source, url=args.url,
                            title=args.title, relevance=args.relevance,
                            verified=args.verified)
            log.info("stored item #%d (total %d)", idx, store.count())
        else:
            items = store.recent(ticker=args.ticker, limit=args.limit)
            for item in items:
                print(json.dumps(item, indent=2, ensure_ascii=False))
            log.info("%d items", len(items))
    finally:
        store.close()
    return 0


def cmd_build_dataset(args) -> int:
    from training import build_reasoning_dataset
    out = build_reasoning_dataset.main(limit=args.limit, seed=args.seed,
                                       use_teacher_llm=args.use_teacher_llm,
                                       out=args.out)
    log.info("reasoning dataset -> %s", out)
    return 0


def cmd_finetune(args) -> int:
    from training import qwen_finetune
    return qwen_finetune.main([
        "--dataset", args.dataset,
        "--base-model", args.base_model,
        "--output-dir", args.output_dir,
        "--epochs", str(args.epochs),
        "--lora-r", str(args.lora_r),
        "--lora-alpha", str(args.lora_alpha),
    ] + (["--dry-run"] if args.dry_run else []))


def cmd_info(_args) -> int:
    from inference.pipeline import registered_horizons
    info = {
        "horizon_days": config.HORIZON,
        "horizons": sorted(registered_horizons()),
        "split": {"train_end": config.TRAIN_END, "val_end": config.VAL_END,
                  "dev": f"{config.DEV_START}..{config.DEV_END}",
                  "holdout_start": config.HOLDOUT_START},
        "ollama_model": config.OLLAMA_MODEL,
        "ollama_url": config.OLLAMA_BASE_URL,
        "default_tickers": config.DEFAULT_TICKERS,
        "model": config.MODEL_VERSION,
        "default_window": config.DEFAULT_BACKTEST_WINDOW,
        "model_scopes": list(config.MODEL_SCOPES),
        "target_variants": list(config.TARGET_VARIANTS),
        "monitoring": {"interval_min": config.DEFAULT_TRACK_INTERVAL_MIN,
                       "ledger": str(config.TRACKING_DB)},
        "paper": {"capital": config.INITIAL_CAPITAL,
                  "buy_threshold": config.BUY_PROB_THRESHOLD},
    }
    print(json.dumps(info, indent=2))
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockllm", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("download", help="fetch market data into data/raw/")
    d.add_argument("--tickers", help="comma-separated symbols (default: config)")
    d.add_argument("--benchmarks", nargs="*", default=config.BENCHMARK_TICKERS)
    d.add_argument("--start")
    d.add_argument("--end")
    d.add_argument("--force", action="store_true")
    d.set_defaults(func=cmd_download)

    fn = sub.add_parser("fetch-news",
                        help="historical news ingestion only (EDGAR / NSE), resume-safe")
    fn.add_argument("--tickers", help="comma-separated symbols (default: config)")
    fn.add_argument("--start", default=config.NEWS_FETCH_START)
    fn.add_argument("--force", action="store_true")
    fn.set_defaults(func=cmd_fetch_news)

    dd = sub.add_parser("data-download",
                        help="V3 layers: fundamentals, news (EDGAR/NSE), sector indices + audit")
    dd.add_argument("--tickers", help="comma-separated symbols (default: config)")
    dd.add_argument("--benchmarks", nargs="*", default=config.BENCHMARK_TICKERS)
    dd.add_argument("--start")
    dd.add_argument("--end")
    dd.add_argument("--news-start", default=config.NEWS_FETCH_START)
    dd.add_argument("--force", action="store_true")
    dd.set_defaults(func=cmd_data_download)

    au = sub.add_parser("audit", help="coverage/provenance report of all data items")
    au.set_defaults(func=cmd_audit)

    ex = sub.add_parser("experiment",
                        help="V3/V4 feature-set variants A/B/C/D/E on the dev window")
    ex.add_argument("--tickers", help="comma-separated symbols (default: config)")
    ex.add_argument("--benchmarks", nargs="*", default=config.BENCHMARK_TICKERS)
    ex.add_argument("--start")
    ex.add_argument("--end")
    ex.add_argument("--scope", choices=list(config.MODEL_SCOPES),
                    default=config.EXPERIMENT_SCOPE)
    ex.add_argument("--target", choices=list(config.TARGET_VARIANTS),
                    default=config.EXPERIMENT_TARGET)
    ex.set_defaults(func=cmd_experiment)

    pr = sub.add_parser("prep", help="build the causal feature matrix")
    pr.add_argument("--tickers", help="comma-separated symbols (default: config)")
    pr.add_argument("--benchmarks", nargs="*", default=config.BENCHMARK_TICKERS)
    pr.add_argument("--start")
    pr.add_argument("--end")
    pr.add_argument("--horizon", type=int, default=config.HORIZON,
                    help="trading-day horizon (default 7; 3/5/14/21/63/126)")
    pr.add_argument("--candles", action="store_true",
                    help="include the causal candlestick geometry layer (V4)")
    pr.set_defaults(func=cmd_prep)

    t = sub.add_parser("train", help="fit the numerical forecaster (train split)")
    t.add_argument("--horizon", type=int, default=config.HORIZON,
                   help="trading-day horizon (default 7; 3/5/14/21/63/126)")
    t.add_argument("--candles", action="store_true",
                   help="include the causal candlestick geometry layer (V4)")
    t.add_argument("--tickers", help="comma-separated symbols (default: config)")
    t.add_argument("--benchmarks", nargs="*", default=config.BENCHMARK_TICKERS)
    t.add_argument("--start")
    t.add_argument("--end")
    t.set_defaults(func=cmd_train)

    b = sub.add_parser("backtest", help="walk-forward backtest on a window")
    b.add_argument("--window", choices=list(config.BACKTEST_WINDOWS),
                   default=config.DEFAULT_BACKTEST_WINDOW,
                   help="'dev' = model selection window, 'holdout' = frozen final holdout")
    b.add_argument("--scope", choices=list(config.MODEL_SCOPES),
                   default=config.DEFAULT_MODEL_SCOPE)
    b.add_argument("--target", choices=list(config.TARGET_VARIANTS),
                   default=config.DEFAULT_TARGET)
    b.add_argument("--refit-every", type=int, help="override config.REFIT_EVERY_DAYS")
    b.add_argument("--horizon", type=int, default=config.HORIZON,
                   help="trading-day horizon (uses feature_matrix_{h}d.csv.gz)")
    b.set_defaults(func=cmd_backtest)

    e = sub.add_parser("evaluate", help="metrics + calibration from latest backtest")
    e.add_argument("--window", choices=list(config.BACKTEST_WINDOWS),
                   default=config.DEFAULT_BACKTEST_WINDOW)
    e.set_defaults(func=cmd_evaluate)

    he = sub.add_parser(
        "holdout-eval",
        help="FINAL frozen-holdout evaluation of the selected V3 variant (C); "
             "frozen config only, audits that no holdout row entered training")
    he.add_argument("--tickers", help="comma-separated symbols (default: config)")
    he.add_argument("--benchmarks", nargs="*", default=config.BENCHMARK_TICKERS)
    he.add_argument("--start")
    he.add_argument("--end")
    he.set_defaults(func=cmd_holdout_eval)

    dg = sub.add_parser("diagnose",
                        help="dev-window diagnostics: variant comparison + permutation importance")
    dg.add_argument("--scopes", default="pooled,id,per_stock")
    dg.add_argument("--targets", default="raw")
    dg.add_argument("--top", type=int, default=15)
    dg.add_argument("--n-perm", type=int, default=2)
    dg.set_defaults(func=cmd_diagnose)

    prd = sub.add_parser("predict", help="forecast + analysis for one ticker")
    prd.add_argument("--ticker", required=True)
    prd.add_argument("--as-of", help="YYYY-MM-DD: re-forecast a historical date (no future info)")
    prd.add_argument("--benchmark")
    prd.add_argument("--horizon", default="7d",
                     help="horizon: '7d', '3d', '2 weeks', '1 month', 'next week' "
                          "(default 7d; non-7d needs a saved model)")
    prd.add_argument("--no-llm", action="store_true", help="skip Qwen, use template")
    prd.add_argument("--json", action="store_true")
    prd.set_defaults(func=cmd_predict)

    sv = sub.add_parser("save", help='watchlist: "Save NVIDIA"')
    sv.add_argument("--ticker", required=True)
    sv.set_defaults(func=cmd_save)

    rp = sub.add_parser("report", help="daily reports for watchlist (JARVIS)")
    rp.add_argument("--tickers", help="comma-separated subset")
    rp.add_argument("--no-llm", action="store_true")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(func=cmd_report)

    pp = sub.add_parser("paper", help="paper-trade backtest signals (SIMULATION ONLY)")
    pp.add_argument("--window", choices=list(config.BACKTEST_WINDOWS),
                    default=config.DEFAULT_BACKTEST_WINDOW)
    pp.add_argument("--scope", choices=list(config.MODEL_SCOPES),
                    default=config.DEFAULT_MODEL_SCOPE)
    pp.add_argument("--target", choices=list(config.TARGET_VARIANTS),
                    default=config.DEFAULT_TARGET)
    pp.add_argument("--refit-every", type=int)
    pp.set_defaults(func=cmd_paper)

    nw = sub.add_parser("news", help="add/list sourced web research items")
    nw.add_argument("action", choices=["add", "list"])
    nw.add_argument("--ticker", required=True)
    nw.add_argument("--text")
    nw.add_argument("--source", default="web-research")
    nw.add_argument("--url", default="")
    nw.add_argument("--title", default="")
    nw.add_argument("--relevance", type=float, default=0.5)
    nw.add_argument("--verified", action="store_true")
    nw.add_argument("--limit", type=int, default=10)
    nw.set_defaults(func=cmd_news)

    tk = sub.add_parser(
        "track",
        help="add tickers to the monitoring watchlist and run the prediction loop")
    tk.add_argument("--tickers", nargs="*",
                    help="symbols to track (omit to run the monitor loop)")
    tk.add_argument("--interval", type=int, default=config.DEFAULT_TRACK_INTERVAL_MIN,
                    help="minutes between cycles (default %(default)s)")
    tk.add_argument("--horizon", default=config.DEFAULT_TRACK_HORIZON,
                    help="prediction horizon, e.g. 7d/3d/2 weeks (default 7d)")
    tk.add_argument("--once", action="store_true",
                    help="run a single cycle and exit")
    tk.add_argument("--cycles", type=int, default=None,
                    help="run at most N cycles and exit")
    tk.set_defaults(func=cmd_track)

    ut = sub.add_parser("untrack", help="remove tickers from the monitoring watchlist")
    ut.add_argument("--tickers", nargs="*", required=True)
    ut.set_defaults(func=cmd_untrack)

    wl = sub.add_parser("watchlist", help="show both watchlists")
    wl.set_defaults(func=cmd_watchlist)

    tr = sub.add_parser("tracking-report",
                        help="prediction ledger: open/resolved outcomes")
    tr.set_defaults(func=cmd_tracking_report)

    jv = sub.add_parser(
        "jarvis",
        help="JARVIS-facing: natural language -> intent -> run (--request)")
    jv.add_argument("--request", required=True,
                    help='e.g. "what do you think about NVIDIA", '
                         '"track RELIANCE every 15 minutes", "stop tracking AAPL"')
    jv.add_argument("--json", action="store_true", help="emit structured JSON")
    jv.set_defaults(func=cmd_jarvis)

    bd = sub.add_parser("build-dataset",
                        help="build synthetic reasoning dataset for future Qwen SFT")
    bd.add_argument("--limit", type=int, default=1000)
    bd.add_argument("--seed", type=int, default=42)
    bd.add_argument("--use-teacher-llm", action="store_true",
                    help="use local Qwen to write rationales (distillation)")
    bd.add_argument("--out")
    bd.set_defaults(func=cmd_build_dataset)

    ft = sub.add_parser("finetune", help="QLoRA fine-tuning foundation (extra deps)")
    ft.add_argument("--dataset", default=str(config.DATASETS_DIR / "reasoning_train.jsonl"))
    ft.add_argument("--base-model", default="Qwen/Qwen2.5-7B-Instruct")
    ft.add_argument("--output-dir", default=str(config.MODELS_DIR / "qwen-lora"))
    ft.add_argument("--lora-r", type=int, default=16)
    ft.add_argument("--lora-alpha", type=int, default=32)
    ft.add_argument("--epochs", type=int, default=2)
    ft.add_argument("--dry-run", action="store_true")
    ft.set_defaults(func=cmd_finetune)

    i = sub.add_parser("info", help="print current configuration")
    i.set_defaults(func=cmd_info)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
