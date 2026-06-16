"""
barra_build_frames_v1.py  --  RETURNS-BASED (time-series beta) frame builder
============================================================================
v1 of the factor model: exposures are per-name BETAS estimated against PUBLISHED daily factor
returns (JKP clusters / Ken French), instead of characteristic z-scores + our own cross-sectional
regression (that remains v2, in barra_build_frames.py). Same six output frames, same cube.

THE TRANSFORM (as walked through):
  0. Align: JKP daily factor matrix F (T x K) inner-joined with each name's Stooq daily returns
     on a common trading calendar; excess returns where applicable.
  1. Per name n, per COB d: OLS on the trailing window ending at d
         b = (Z'Z)^-1 Z'y,   Z = [1 | F_window],  y = stock returns
     -> beta vector = that name's leaf loadings at COB d.   (point-in-time: data <= d only)
  2. Residuals u_n(t) = y - Z b  ->  EWMA of u^2  ->  SpecificVar  (orthogonal by construction).
  3. Shrinkage: beta = KAPPA*b_ols + (1-KAPPA)*prior  (prior: 1 for Market, 0 for styles).
  4. factor_returns = the downloaded daily series VERBATIM (the scenario cache; never regressed).

Frames out (identical schemas to v2):
  exposures (Date,Position,Factor)->Loading | positions (Date,Book,Position)->Weight,MV
  securities | factor_meta | factor_returns (Date,Factor)->Return | specific_var (Date,Position)->SpecificVar

DATA SOURCES (free):
  * Factors: tries JKP daily US theme-cluster file first (jkpfactors.com parquet/csv endpoints move
    occasionally -- pin JKP_DAILY_URL after one manual download), falls back to Ken French daily
    (FF5 + momentum + ST reversal, stable bulk URL since forever).
  * Stock prices: Stooq (as in v2).  * Positions/crosswalk: reuses v2's EDGAR 13F + OpenFIGI code.
"""
from __future__ import annotations
import io, zipfile, pathlib
import numpy as np
import pandas as pd

# reuse v2's EDGAR/OpenFIGI/Stooq plumbing (same folder)
from barra_build_frames import (positions_from_13f, crosswalk_cusips, ticker_to_cik,
                                stooq_daily, SOROS_CIK, UNIVERSE_CAP, _get)

# ----------------------------------------------------------------- config
LOOKBACK_D   = 504          # ~2y trailing window for the beta regressions
MIN_OBS      = 120          # below this, no beta (proxy from sector in a later pass)
EWMA_HL_D    = 63           # specific-variance half-life (days)
KAPPA        = 0.75         # shrink: beta = k*ols + (1-k)*prior
COB_FREQ     = "ME"         # month-end COBs; set "B" for daily COBs (heavier)
START, END   = "2022-01-01", "2024-12-31"
JKP_DAILY_URL = None        # pin after manual download from jkpfactors.com (clusters_daily, US)
FF_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
          "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
FF_MOM_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
              "F-F_Momentum_Factor_daily_CSV.zip")


# ----------------------------------------------------------------- factor cache download
def _read_ff_zip(url: str) -> pd.DataFrame:
    raw = _get(url, headers={"User-Agent": "Mozilla/5.0"})
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        txt = z.read(z.namelist()[0]).decode("latin1")
    lines = txt.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip()[:8].replace(",", "").isdigit())
    end = next((i for i in range(start, len(lines)) if not lines[i].strip()
                or not lines[i].strip()[:8].isdigit()), len(lines))
    df = pd.read_csv(io.StringIO("\n".join([lines[start - 1]] + lines[start:end])))
    df = df.rename(columns={df.columns[0]: "Date"})
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str.strip(), format="%Y%m%d")
    return df.set_index("Date").apply(pd.to_numeric, errors="coerce") / 100.0   # % -> decimal

def load_factor_daily() -> tuple[pd.DataFrame, pd.Series]:
    """Returns (F: T x K decimal daily factor returns, rf: daily risk-free)."""
    if JKP_DAILY_URL:                                     # preferred: JKP 13 clusters
        f = pd.read_parquet(JKP_DAILY_URL) if JKP_DAILY_URL.endswith(".parquet") \
            else pd.read_csv(JKP_DAILY_URL, parse_dates=["date"])
        f = f.pivot_table(index="date", columns="name", values="ret")
        rf = pd.Series(0.0, index=f.index)                # JKP LMS factors are already excess
    else:                                                  # fallback: Ken French daily
        ff5 = _read_ff_zip(FF_URL)                         # Mkt-RF SMB HML RMW CMA RF
        mom = _read_ff_zip(FF_MOM_URL)                     # Mom
        f = ff5.join(mom, how="inner")
        rf = f.pop("RF")
        f = f.rename(columns={"Mkt-RF": "Market", "SMB": "Size", "HML": "Value",
                              "RMW": "Profitability", "CMA": "Investment", "Mom   ": "Momentum",
                              "Mom": "Momentum"})
    return f.loc[START:END].dropna(how="any"), rf.loc[START:END]


# ----------------------------------------------------------------- the transform
def rolling_betas(stock_ret: pd.Series, F: pd.DataFrame, rf: pd.Series,
                  cobs: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Stages 1-3 for one name: per-COB trailing OLS -> shrunk betas + EWMA specific var."""
    df = pd.concat([stock_ret.rename("r"), F, rf.rename("rf")], axis=1, join="inner").dropna()
    df["r"] = df["r"] - df["rf"]                           # excess on excess
    K = F.shape[1]
    prior = np.array([1.0 if c == "Market" else 0.0 for c in F.columns])
    beta_rows, sv_rows = [], []
    lam = 0.5 ** (1.0 / EWMA_HL_D)
    for d in cobs:
        win = df.loc[:d].tail(LOOKBACK_D)
        if len(win) < MIN_OBS:
            continue
        Z = np.column_stack([np.ones(len(win)), win[F.columns].values])     # [1 | F]
        y = win["r"].values
        b, *_ = np.linalg.lstsq(Z, y, rcond=None)                            # (Z'Z)^-1 Z'y
        beta = KAPPA * b[1:] + (1 - KAPPA) * prior                           # shrink
        u = y - Z @ np.concatenate([[b[0]], beta])                           # residuals w/ shrunk beta
        # EWMA of u^2 across the window -> specific variance as of d (daily variance)
        wts = lam ** np.arange(len(u) - 1, -1, -1); wts /= wts.sum()
        sv = float(np.dot(wts, u ** 2))
        beta_rows += [{"Date": d, "Factor": c, "Loading": float(beta[k])}
                      for k, c in enumerate(F.columns)]
        sv_rows.append({"Date": d, "SpecificVar": sv})
    return pd.DataFrame(beta_rows), pd.DataFrame(sv_rows)


def build_frames():
    F, rf = load_factor_daily()
    cobs = pd.date_range(START, END, freq=COB_FREQ)
    cobs = pd.DatetimeIndex([F.index[F.index <= d][-1] for d in cobs if (F.index <= d).any()])

    # positions + identity (reused v2 plumbing)
    pos13f = positions_from_13f(SOROS_CIK)
    xw = crosswalk_cusips(pos13f["cusip"].tolist())
    t2c = ticker_to_cik()
    sec = (xw.dropna(subset=["figi", "ticker"]).merge(t2c, on="ticker", how="left")
             .dropna(subset=["cik"]).drop_duplicates("figi").head(UNIVERSE_CAP))

    exp_rows, sv_frames = [], []
    for _, s in sec.iterrows():
        px = stooq_daily(s["ticker"])
        if px is None:
            continue
        ret = px["Close"].pct_change().loc[START:END]
        betas, sv = rolling_betas(ret, F, rf, cobs)
        if betas.empty:
            continue
        betas["Position"] = s["figi"]; sv["Position"] = s["figi"]
        exp_rows.append(betas); sv_frames.append(sv)
    exposures = pd.concat(exp_rows, ignore_index=True)[["Date", "Position", "Factor", "Loading"]]
    specific_var = pd.concat(sv_frames, ignore_index=True)[["Date", "Position", "SpecificVar"]]

    # Soros weights as-of each COB (same as-of logic as v2, condensed)
    p = pos13f.merge(sec[["cusip", "figi"]], on="cusip").rename(columns={"figi": "Position"})
    p["Weight"] = p.groupby("filing_date")["value"].transform(lambda v: v / v.sum())
    cal = pd.DataFrame({"Date": cobs})
    positions = (p.sort_values("filing_date").groupby("Position", group_keys=False)
                   .apply(lambda g: pd.merge_asof(cal, g, left_on="Date", right_on="filing_date",
                                                  direction="backward").assign(Position=g.name))
                   .dropna(subset=["Weight"]).assign(Book="Soros")
                 )[["Date", "Book", "Position", "Weight", "value"]].rename(columns={"value": "MV"})

    securities = (sec.rename(columns={"figi": "Position", "ticker": "Ticker", "cusip": "CUSIP",
                                      "title": "Issuer", "cik": "CIK"})
                  [["Position", "Ticker", "CIK", "CUSIP", "Issuer"]]
                  .assign(Sector="Unknown", Country="US"))
    factor_meta = pd.DataFrame({"Factor": list(F.columns),
                                "FactorGroup": ["Market" if c == "Market" else "Style"
                                                for c in F.columns]})
    factor_returns = F.stack().rename("Return").reset_index()
    factor_returns.columns = ["Date", "Factor", "Return"]
    return exposures, positions, securities, factor_meta, factor_returns, specific_var


if __name__ == "__main__":
    frames = build_frames()
    out = pathlib.Path(__file__).resolve().parent.parent / "data"
    out.mkdir(exist_ok=True)
    for nm, df in zip(["exposures", "positions", "securities", "factor_meta",
                       "factor_returns", "specific_var"], frames):
        df.to_parquet(out / f"{nm}.parquet", index=False)
        print(f"{nm:14s} {df.shape}")
    print("\nDaily scenario cache:", frames[4]["Date"].nunique(), "days ->",
          "feed straight into barra_factor_risk_cube.py (unchanged).")
