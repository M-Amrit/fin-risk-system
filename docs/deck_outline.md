# 10-minute deck outline (6 slides)

1. **Executive summary & scope** — one line: lender's P&L, budget variance and credit-risk scorecard in one governed pipeline with an automated, self-gating board pack. State up front: demonstrated on synthetic data.
2. **Architecture & pipeline** — Python scorecard → `stg_scored_loans` → PostgreSQL star schema (GL + loans + bridge) → DAX / Qlik → Power Automate. Show the run order 01 → 00 → 00b → 02 → 03 → 04 and the 18-check audit gate. Mention 790 vs 312k ledger lines.
3. **Financial analytics & variance** — P&L bridge, YTD, favourable/unfavourable flags (sample Dec-2021 table: revenue −1.2%, marketing +11.0% over). Explain direction-aware variance.
4. **Credit scorecard & metrics** — 600 @ 50:1, PDO 20; bands with observed vs predicted PD (monotone); train/test AUC 0.64, Gini 0.27, KS 0.20; PSI 0.002. Callout: leakage inflates Gini to 0.42; IV>0.5 caught it.
5. **Automated board reporting** — flow diagram incl. gate; show the gate refusing to send (EL 33 months stale). Be explicit that the flow is a spec.
6. **ROI, vulnerabilities, roadmap**
   - **Estimated ROI (assumption-based, NOT measured, in INR):** no real hours have been collected and the pipeline has only
     run on synthetic data, so no saving has been observed. `Annual saving = (X - Y) x 12 x Z - annual run cost`
     (X = manual hours/month, Y = review hours/month, Z = loaded hourly cost).
   - **Hourly cost Z (derived, not measured):** published India financial-analyst pay varies widely: about Rs 5.64 lakh/yr
     average base (Payscale, 2026), Rs 7.3 lakh/yr median all-in CTC (Flexiple, Aug 2026; middle 50% Rs 4.8-12.3 lakh), and
     Rs 7-10 lakh/yr for FP&A freshers (CA MONK, via Quintedge). Z = annual cost x 1.2 loading / 2,000 working hours
     (both the 1.2 and the 2,000 are my assumptions). Result: **low Rs 340/h, base Rs 440/h, high Rs 600/h.**
     One aggregator listed about Rs 16.9 lakh/yr; I treated it as an outlier and did not use it.
   - **Scenarios (hours are assumptions for illustration), gross annual saving at base Z = Rs 440/h:**

     | Scenario | Manual X -> Review Y | Hours saved/yr | Saving at Rs 340/h | at Rs 440/h (base) | at Rs 600/h |
     |---|---|---|---|---|---|
     | Conservative | 10 h -> 4 h | 72 | Rs 24,480 | **Rs 31,680** | Rs 43,200 |
     | Mid | 16 h -> 4 h | 144 | Rs 48,960 | **Rs 63,360** | Rs 86,400 |
     | Optimistic | 24 h -> 4 h | 240 | Rs 81,600 | **Rs 105,600** | Rs 144,000 |

   - **Read this before quoting the numbers:** these are *gross* savings. They exclude the cost of building and maintaining
     the system, PostgreSQL hosting, and Qlik / Power Automate licences (Premium connectors), none of which have been priced.
     At Rs 30,000-1,00,000 a year of labour saved, licence and maintenance costs could consume most or all of it. The honest
     pitch for this project is control and risk reduction, not cost savings. Replace X and Y with real hours before presenting.
   - **Value not captured in the formula:** 18 automated data-quality checks, and a gate that blocks a stale risk number
     (sample: 33 months old) from reaching the board.
   - **Top vulnerabilities:** built and tested on synthetic data; PD is not 12-month; modelled EL is ~3x the booked
     allowance; DAX, Qlik and Power Automate not yet validated in the real tools; SQL not yet re-run on the author's own machine.
   - **Roadmap:** run on real LendingClub / Home Credit data, 12-month and lifetime PD with IFRS 9 staging, CI pipeline,
     ledger partitioning, validate the BI layer.
