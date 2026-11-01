# Options model validation

    Model vs Angel's real 5-minute option bars: 48 contracts, 581 contract-days, 30032 bars.
      median abs error 6.0 % (clamped 5.2 %, with VIX drift 6.1 %); 90th percentile 17.7 %.
      median signed error -0.8 % (model minus real: negative = the model is cheaper than the market).
      stop/target outcome agreement 93.1 % of 1265 trials (VIX drift 92.8 %); average trial return before costs: real 1.58 %, model 0.99 % (-25 % stop, +50 % target, entries 09:35 / 11:00 / 13:00).
      by moneyness: OTM >2% 8.9 % (5508); OTM 0.5-2% 6.7 % (9115); ATM +-0.5% 5.5 % (4499); ITM 0.5-2% 5.3 % (7917); ITM >2% 3.4 % (2993)
      by trading days to expiry: 3-5 9.6 % (1460); 6-10 9.5 % (3572); 11+ 5.5 % (25000)
      by time of day (the vol is anchored at the open): 09:15-10:00 4.9 %; 10:00-12:00 5.7 %; 12:00-14:00 6.6 %; 14:00-15:30 6.6 %
      HDFCBANK: median 7.3 % (signed 1.2 %), agreement 94.3 %, trial return real 2.14 % vs model 0.73 % (4627 bars)
      NIFTY: median 5.7 % (signed -1.4 %), agreement 90.1 %, trial return real 0.94 % vs model 0.86 % (15529 bars)
      RELIANCE: median 6.3 % (signed -1.0 %), agreement 97.4 %, trial return real 3.31 % vs model 1.95 % (5598 bars)
      SBIN: median 5.7 % (signed -0.6 %), agreement 97.3 %, trial return real 1.07 % vs model 0.55 % (4278 bars)
      contract                       days  bars    err%  clamp%  agree%
      NIFTY 2026-11-03 22850 CE         6   438    16.1    12.5    77.8
      NIFTY 2026-11-03 22850 PE         6   434     7.2     6.6    88.9
      NIFTY 2026-11-03 22900 CE         6   438    20.2    15.5    88.9
      NIFTY 2026-11-03 22900 PE         6   438    11.1    10.8    83.3
      NIFTY 2026-11-03 22950 CE         6   438    16.2    12.1    83.3
      NIFTY 2026-11-03 22950 PE         6   421     6.2     6.2    77.8
      NIFTY 2026-11-03 23000 CE         6   438    24.4    18.6    88.9
      NIFTY 2026-11-03 23000 PE         6   438     3.7     3.7    83.3
      NIFTY 2026-11-03 23050 CE         6   438     7.0     6.6    83.3
      NIFTY 2026-11-03 23050 PE         6   400     4.2     4.2    94.4
      NIFTY 2026-11-09 22850 CE         1    73     8.1     6.8    66.7
      NIFTY 2026-11-09 22850 PE         1    71    16.3    16.3   100.0
      NIFTY 2026-11-09 22900 CE         1    73     6.3     5.7    33.3
      NIFTY 2026-11-09 22900 PE         1    73     7.7     7.7   100.0
      NIFTY 2026-11-09 22950 CE         1    73     8.1     7.4    66.7
      NIFTY 2026-11-09 22950 PE         1    68    15.3    15.3    33.3
      NIFTY 2026-11-09 23000 CE         1    73     5.8     5.5    33.3
      NIFTY 2026-11-09 23000 PE         1    73     5.5     5.5   100.0
      NIFTY 2026-11-09 23050 CE         1    73     7.0     6.4    33.3
      NIFTY 2026-11-09 23050 PE         1    61    14.7    14.7    33.3
      NIFTY 2026-11-23 22850 CE        19   999     4.3     3.7    95.7
      NIFTY 2026-11-23 22850 PE        19   914     3.8     3.5    88.9
      NIFTY 2026-11-23 22900 CE        19  1355     4.7     4.1    96.5
      NIFTY 2026-11-23 22900 PE        19  1243     5.1     4.7    84.9
      NIFTY 2026-11-23 22950 CE        19  1011     5.3     4.3    97.8
      NIFTY 2026-11-23 22950 PE        19   663     4.3     3.5    96.6
      NIFTY 2026-11-23 23000 CE        19  1387     5.2     4.6    98.2
      NIFTY 2026-11-23 23000 PE        19  1387     3.5     3.3    93.0
      NIFTY 2026-11-23 23050 CE        18   963     6.0     5.0    97.7
      NIFTY 2026-11-23 23050 PE        18   575     4.5     3.8    90.9
      SBIN 2026-11-23 960 CE           19   951     5.5     3.9    97.6
      SBIN 2026-11-23 960 PE           19   753     4.8     3.9    96.9
      SBIN 2026-11-23 970 CE           19   679     5.1     3.9    96.3
      SBIN 2026-11-23 970 PE           14   506     4.7     4.4    95.0
      SBIN 2026-11-23 980 CE           19   850     8.3     6.0    97.4
      SBIN 2026-11-23 980 PE           17   539     6.6     4.6   100.0
      RELIANCE 2026-11-23 1160 CE      19   786     4.2     3.5   100.0
      RELIANCE 2026-11-23 1160 PE      19  1103     7.7     7.0    95.6
      RELIANCE 2026-11-23 1170 CE      19   771     4.9     3.7   100.0
      RELIANCE 2026-11-23 1170 PE      19   864     9.0     7.7    97.1
      RELIANCE 2026-11-23 1180 CE      19  1048     5.8     4.4    97.7
      RELIANCE 2026-11-23 1180 PE      19  1026     5.5     4.9    95.1
      HDFCBANK 2026-11-23 670 CE       10   355     7.3     5.6   100.0
      HDFCBANK 2026-11-23 670 PE       19   943     8.0     7.5    87.2
      HDFCBANK 2026-11-23 680 CE       18   651     4.1     3.6   100.0
      HDFCBANK 2026-11-23 680 PE       19  1106    10.6     8.7    93.5
      HDFCBANK 2026-11-23 690 CE       17   643     5.1     4.1   100.0
      HDFCBANK 2026-11-23 690 PE       19   929     9.5     8.0    92.3
