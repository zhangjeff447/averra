# ETF Agent V3

SMH / QQQ / SGOV · Release 2026-10-15 · Public downloads are read-only.

## Three ETFs, three market phases

Normal: SMH 70%, QQQ 20%, SGOV 10%, with an absolute 0.5 percentage-point tolerance. Active SGOV purchases stop at 10%. SMH and QQQ are buy-only.

Pullback: retain the existing SMH pullback and QQQ score rules. Verified cash funds eligible core buys first. SGOV may grow gradually to at most 20%; this is a ceiling, not a required holding. Sell SGOV only when an eligible core purchase needs additional funding.

Recovery: use separate recovery conditions to buy the core ETFs. Cash comes first, then staged SGOV rotation. Complete the rotation with SGOV at most 5%, including when earlier rounds already used cash. After sustained recovery and completion, rebuild SGOV toward 10% from subsequent confirmed funds.

## What the downloads do

The macOS and Windows apps include the current three-ETF rules, configuration preview and native read-only account validation. They do not install a scheduler, start a live cycle, preview orders or submit orders. The website cannot activate trading.

The local production service and the downloadable desktop apps have different execution permissions. A production deployment on one computer does not authorize another computer or account. The public entry rejects --live even if environment variables are changed.

## Setup

Choose your platform in Tools. Extract the complete ZIP. On macOS, open Averra Agent.app and verify your Agent key. Python is included; Apple Silicon and macOS 13+ are required. This build is not Apple-notarized.

Windows is a cross-compiled x64 preview. Install .NET 8 Desktop Runtime x64, extract the entire ZIP and open AverraDesktop.exe. Python is included. The build is unsigned and has not been accepted on physical Windows hardware.

Select your existing Codex MCP configuration and sign in to your own Robinhood connection there. Connect MCP, then run read-only account validation during regular market hours. Missing or stale quotes, an unresolved intent, pending approval or inconsistent funds stop validation. Keys, account data and financial records are not included in a download.

## Allocation animation

The website retains the animated line-drawn chip trays, now one each for SMH, QQQ and SGOV. Fields and sliders always total 100%. The normal preset is 70 / 20 / 10. Pullback and recovery presets illustrate SGOV ceilings; they are not fixed targets for every phase.

Custom percentages and copied JSON are browser drafts only. They do not overwrite the frozen policy or your installed app. Reset returns the study to 70 / 20 / 10.

## Funds and execution rules in the production strategy

The verified Robinhood Agentic limited-margin account permits reuse of eligible unsettled sale proceeds without margin borrowing. Use the smaller current cash and unleveraged buying-power amount, retain the fee buffer, and wait on pending external credits. Never add estimated SGOV sales or historical proceeds to cash again. A filled sale must be reconciled before a fresh cash read funds another purchase.

Approved production windows are 10:05, 12:30 and 15:30 America/New_York. Combined daily purchases and each order are capped at $100; SGOV sales have a separate $100 daily cap. One order per window and one unresolved intent for the account. SMH allows one buy per day; QQQ allows at most two with its existing confirmed-fill and score-increase rule. Only SGOV can be sold.

Fresh account, complete order history, holdings, funds, quotes and native broker pre-checks precede submission. Approval stays in Robinhood. Partial, pending and uncertain orders remain reserved; an uncertain submission is not repeated. Trading decisions use deterministic code and zero trading LLM calls.

## Moving computers

Use the preparation guide in Tools. Stop and verify the source executor first. Preserve the current ledger, fills, reservations and audit history. Restore encrypted private records locally and repeat fresh account reconciliation before any private production activation. Do not overwrite forward financial facts with an older database.

The public app does not overwrite an existing workspace automatically. A legacy two-ETF workspace requires migration review; a new V3 workspace can be used for read-only setup without replacing the old records.

## Historical research and limits

Public daily data: 2015-01-02 to 2026-10-13. The research simulation uses completed-session signals, next-session execution, equal monthly contributions and 5 basis points of one-way slippage.

Time-weighted maximum drawdown: dynamic cycle 41.63%; fixed 70 / 20 / 10 40.37%; previous 80 / 20 rules 43.10%. The cycle did not outperform every comparison. These are simulations, not a private account's returns.

Adjusted-price research assumes same-ETF dividend reinvestment and does not reproduce payable-date cash, intraday volatility, taxes, actual approvals or broker execution. Real account validation and deterministic tests are separate evidence. Neither certifies a new device or future returns.

[Tools and preparation](/agent/#agent)

[Robinhood Agentic account capabilities](https://robinhood.com/us/en/support/articles/setting-up-an-agent/)


SGOV holds U.S. Treasury bills with remaining maturities of 0–3 months. This release replaces the defensive symbol only; 70/20/10 targets, 20% pullback cap, 5% recovery cap, order rules and safeguards are unchanged. Includes 1,602 SGOV daily bars from 2020-06-01 through 2026-10-14; ex-date dividends are not spendable cash. The public entry remains read-only and does not modify existing workspaces or running services.


## 2026-10-15 Operations refresh

ETF keeps SMH 70% / QQQ 20% / SGOV 10% and the existing SGOV rules. Includes BUY / SELL / WAIT report formatting, plan/submission/fill amounts, reference/fill prices, remaining cash, market regime, a separate read-only daily-holdings component, delivery deduplication, run and setting history. Holdings reports are due from 15:31 ET on trading days after private setup. The public entry remains configuration/read-only account validation only; it does not install a background service or send Telegram automatically. Existing live installations require owner-managed migration and acceptance without replacing ledgers. No monthly $300 allowance or legacy cumulative virtual budget is displayed.
