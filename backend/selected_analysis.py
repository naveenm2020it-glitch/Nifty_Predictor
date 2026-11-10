"""Date/expiry-selected NIFTY data loading and independent research agents."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from .db import connect
from . import pipeline as src

log = logging.getLogger("nifty_iq.selected_analysis")


def _rows(dataset_key: str, trade_date: date):
    with connect() as conn:
        return [dict(r["payload"]) for r in conn.execute(
            "SELECT payload FROM dataset_records WHERE dataset_key=%s AND trade_date=%s ORDER BY id",
            (dataset_key, trade_date)
        ).fetchall()]


def _latest_spot(trade_date: date):
    with connect() as conn:
        records = conn.execute("""
          SELECT trade_date,payload FROM dataset_records
          WHERE dataset_key='spot_ohlc' AND trade_date<=%s
          ORDER BY trade_date DESC LIMIT 80
        """, (trade_date,)).fetchall()
    grouped = {}
    for record in records:
        p = record["payload"]
        vals = [src._clean_number(src._get(p, aliases)) for aliases in
                (["OPEN","Open","open"],["HIGH","High","high"],["LOW","Low","low"],["CLOSE","Close","close"])]
        if None in vals or min(vals) <= 0:
            continue
        o,h,l,c = vals
        if h < max(o,c,l) or l > min(o,c,h):
            continue
        grouped[record["trade_date"]] = {"date":record["trade_date"],"open":o,"high":h,"low":l,"close":c}
    return [grouped[d] for d in sorted(grouped)]


def available_dates(limit: int = 500):
    with connect() as conn:
        rows = conn.execute("""
          SELECT DISTINCT trade_date FROM dataset_records
          WHERE dataset_key='spot_ohlc' ORDER BY trade_date DESC LIMIT %s
        """, (max(1,min(limit,1500)),)).fetchall()
    dates = [r["trade_date"].isoformat() for r in rows]
    return {"dates":dates,"latest_date":dates[0] if dates else None,
            "earliest_date":dates[-1] if dates else None,
            "note":"Only dates with stored NIFTY spot records are listed. Select a missing valid session to request an official-source download."}


def expiries_for_date(trade_date: date):
    contracts = _rows("fo_bhavcopy", trade_date)
    found = set()
    for row in contracts:
        symbol = str(src._get(row,["TckrSymb","Symbol","Ticker","Undrlyg"]) or "").upper()
        opt = str(src._get(row,["OptnTp","Option Type","OptionType"]) or "").upper()
        expiry = src._parse_date(src._get(row,["XpryDt","Expiry Date","ExpiryDate"]))
        if symbol == "NIFTY" and opt in ("CE","PE") and expiry:
            found.add(expiry)
    return sorted(found)


def _download_selected(trade_date: date):
    outcomes = []
    spot = _rows("spot_ohlc", trade_date)
    if not spot:
        session = src.requests.Session()
        session.headers.update({"User-Agent":src.UA,"Content-Type":"application/json; charset=UTF-8",
                                "X-Requested-With":"XMLHttpRequest","Referer":src.NIFTY_INDICES+"/reports/historical-data"})
        rows = src._nifty_index_rows(session,"getHistoricaldatatabletoString",trade_date,trade_date,"NIFTY 50")
        rows = [r for r in rows if src._parse_date(r.get("HistoricalDate")) == trade_date]
        if not rows:
            rows = src._nse_index_archive_rows(session,trade_date,trade_date,"NIFTY 50")
        if not rows:
            raise RuntimeError("Official NIFTY 50 OHLC data was not returned for this date.")
        outcomes.append(src._insert_source("spot_ohlc","nifty50_daily_ohlc","NSE Indices — NIFTY 50 daily OHLC",
            rows[0].get("_source_url") or src.NIFTY_INDICES+"/reports/historical-data",
            "nifty50_"+trade_date.isoformat()+".json",trade_date,json.dumps(rows).encode(),rows))
    else:
        outcomes.append({"source":"nifty50_daily_ohlc","status":"already_archived","rows":len(spot)})

    if not src._source_exists("india_vix",trade_date):
        session = src.requests.Session()
        session.headers.update({"User-Agent":src.UA,"Referer":src.NIFTY_INDICES+"/reports/historical-data"})
        vix = [r for r in src._vix_rows(session,trade_date,trade_date)
               if src._parse_date(r.get("date") or r.get("HistoricalDate")) == trade_date]
        if vix:
            outcomes.append(src._insert_source("vix_volatility","india_vix","NSE Indices — India VIX historical OHLC",
                src.NIFTY_INDICES+"/reports/historical-data","india_vix_"+trade_date.isoformat()+".json",
                trade_date,json.dumps(vix).encode(),vix))
        else:
            outcomes.append({"source":"india_vix","status":"missing","reason":"No official VIX row returned for selected date."})

    nse = src._session()
    ymd=trade_date.strftime("%Y%m%d"); dmy=trade_date.strftime("%d%m%Y")
    tasks=[
      ("fo_bhavcopy","fo_bhavcopy","F&O - UDiFF Common Bhavcopy Final (zip)",
       "BhavCopy_NSE_FO_0_0_0_"+ymd+"_F_0000.csv.zip",["content/fo/BhavCopy_NSE_FO_0_0_0_"+ymd+"_F_0000.csv.zip"],"zip"),
      ("participant_oi","participant_oi","F&O - Participant wise Open Interest(csv)",
       "fao_participant_oi_"+dmy+".csv",["content/nsccl/fao_participant_oi_"+dmy+".csv"],"participant"),
      ("fii_derivatives","fii_derivatives","F&O - FII Derivatives Statistics",
       "fii_stats_"+trade_date.strftime("%d-%b-%Y")+".xls",[],"xls"),
      ("vix_volatility","fo_settlement","F&O - Daily Settlement Prices",
       "FOSett_prce_"+dmy+".csv",["content/fo/FOSett_prce_"+dmy+".csv"],"csv"),
      ("vix_volatility","fo_volatility","F&O - Daily Volatility",
       "FOVOLT_"+dmy+".csv",["content/fo/FOVOLT_"+dmy+".csv"],"csv"),
    ]
    for dataset_key,source_key,report_name,filename,paths,kind in tasks:
        if src._source_exists(source_key,trade_date):
            outcomes.append({"source":source_key,"status":"already_archived"})
            continue
        try:
            raw,url=src._download_nse(nse,trade_date,report_name,paths,timeout=(6,18))
            if kind=="zip":
                rows=src._csv_rows(raw,zipped=True)
                rows=[r for r in rows if str(src._get(r,["TckrSymb","Symbol","Ticker","Undrlyg"]) or "").upper()=="NIFTY"]
                if not rows: raise ValueError("Bhavcopy has no NIFTY contracts.")
            elif kind=="participant":
                rows=[dict(r,_trade_date=trade_date.isoformat()) for r in src._participant_rows(raw)]
            elif kind=="xls":
                rows=[dict(r,_scope_note="Aggregate FII index-segment context; not NIFTY-only.") for r in src._xls_rows(raw)]
            else:
                rows=src._csv_rows(raw)
                rows=[r for r in rows if src._get(r,["TckrSymb","Symbol","Ticker","Underlying","Undrlyg"]) is None
                      or "NIFTY" in str(src._get(r,["TckrSymb","Symbol","Ticker","Underlying","Undrlyg"])).upper()]
            if not rows: raise ValueError("Downloaded report contained no usable rows.")
            outcomes.append(src._insert_source(dataset_key,source_key,"NSE India — "+report_name,url,filename,
                                               trade_date,raw,rows))
        except Exception as exc:
            log.warning("Selected-date source %s failed: %s",source_key,exc)
            outcomes.append({"source":source_key,"status":"missing","reason":str(exc)[:240]})
    return outcomes


def _option_rows(trade_date: date, expiry: date):
    contracts={}
    for row in _rows("fo_bhavcopy",trade_date):
        symbol=str(src._get(row,["TckrSymb","Symbol","Ticker","Undrlyg"]) or "").upper()
        opt=str(src._get(row,["OptnTp","Option Type","OptionType"]) or "").upper()
        exp=src._parse_date(src._get(row,["XpryDt","Expiry Date","ExpiryDate"]))
        strike=src._clean_number(src._get(row,["StrkPric","Strike Price","StrikePrice"]))
        if symbol!="NIFTY" or opt not in ("CE","PE") or exp!=expiry or strike is None:
            continue
        contracts.setdefault(strike,{})[opt]={
          "close":src._clean_number(src._get(row,["ClsPric","Close","Close Price"])),
          "oi":src._clean_number(src._get(row,["OpnIntrst","Open Interest","OpenInterest"])) or 0,
          "change_oi":src._clean_number(src._get(row,["ChngInOpnIntrst","Change in OI","ChangeInOpenInterest"])) or 0,
          "volume":src._clean_number(src._get(row,["TtlTradgVol","Total Traded Volume","Volume"])) or 0,
          "raw":row
        }
    return contracts


def analyse_selected_session(trade_date: date, expiry: date, itm_count: int=7, otm_count: int=7, download_if_missing: bool=True):
    if not src.is_nse_trading_session(trade_date):
        raise ValueError("Selected date is not an NSE trading session.")
    if download_if_missing:
        download_outcomes=_download_selected(trade_date)
    else:
        download_outcomes=[]
    spot_rows=_latest_spot(trade_date)
    selected=[x for x in spot_rows if x["date"]==trade_date]
    if not selected:
        raise ValueError("No verified NIFTY 50 OHLC record is available for selected date.")
    spot=selected[0]; close=spot["close"]
    contracts=_option_rows(trade_date,expiry)
    if not contracts:
        raise ValueError("No NIFTY option contracts found for the selected date and expiry. Check NSE source availability.")
    strikes=sorted(contracts)
    atm=min(strikes,key=lambda x:abs(x-close))
    atm_index=strikes.index(atm)
    start=max(0,atm_index-max(0,min(itm_count,25)))
    end=min(len(strikes),atm_index+max(0,min(otm_count,25))+1)
    chosen=strikes[start:end]
    chain=[]
    for strike in chosen:
        sides=contracts[strike]; ce=sides.get("CE",{}); pe=sides.get("PE",{})
        chain.append({"strike":strike,"moneyness":"ATM" if strike==atm else ("BELOW SPOT" if strike<close else "ABOVE SPOT"),
          "ce_ltp":ce.get("close"),"ce_oi":ce.get("oi"),"ce_change_oi":ce.get("change_oi"),"ce_volume":ce.get("volume"),
          "pe_ltp":pe.get("close"),"pe_oi":pe.get("oi"),"pe_change_oi":pe.get("change_oi"),"pe_volume":pe.get("volume")})
    all_ce=sum(v.get("CE",{}).get("oi",0) for v in contracts.values())
    all_pe=sum(v.get("PE",{}).get("oi",0) for v in contracts.values())
    ce_oi=[(v.get("CE",{}).get("oi",0),k) for k,v in contracts.items() if "CE" in v]
    pe_oi=[(v.get("PE",{}).get("oi",0),k) for k,v in contracts.items() if "PE" in v]
    pain=[]
    for settle in strikes:
        value=sum(v.get("CE",{}).get("oi",0)*max(0,settle-k)+v.get("PE",{}).get("oi",0)*max(0,k-settle) for k,v in contracts.items())
        pain.append((value,settle))
    option_agent={"status":"analysed","bias":"bullish" if all_pe>all_ce*1.1 else "bearish" if all_ce>all_pe*1.1 else "neutral",
      "evidence":{"expiry":expiry.isoformat(),"atm_strike":atm,"contracts":len(contracts),"call_oi":all_ce,"put_oi":all_pe,
      "pcr":round(all_pe/all_ce,3) if all_ce else None,"max_pain":min(pain)[1] if pain else None,
      "highest_put_oi_support":max(pe_oi)[1] if pe_oi else None,"highest_call_oi_resistance":max(ce_oi)[1] if ce_oi else None,
      "selected_strike_count":len(chosen)},"chain":chain,
      "note":"Historical EOD bhavcopy values; premiums are closing prices, not live executable quotes."}

    recent=spot_rows[-60:]
    closes=[x["close"] for x in recent]
    sma5=sum(closes[-5:])/min(5,len(closes)); sma20=sum(closes[-20:])/min(20,len(closes))
    atr=sum(x["high"]-x["low"] for x in recent[-14:])/min(14,len(recent))
    price_score=(1 if close>sma20 else -1 if close<sma20 else 0)+(1 if sma5>sma20 else -1 if sma5<sma20 else 0)
    price_agent={"status":"analysed","bias":"bullish" if price_score>0 else "bearish" if price_score<0 else "neutral",
      "evidence":{"open":spot["open"],"high":spot["high"],"low":spot["low"],"close":close,"sma5":round(sma5,2),
      "sma20":round(sma20,2),"atr14_or_available":round(atr,2),"history_sessions":len(recent)}}

    participant_rows=_rows("participant_oi",trade_date); fii_rows=_rows("fii_derivatives",trade_date)
    participant_net={}
    for row in participant_rows:
        label=str(src._get(row,["Client Type","Participant","Participant Type"]) or "unknown").upper()
        longv=src._clean_number(src._get(row,["Future Index Long","FutureIndexLong"]))
        shortv=src._clean_number(src._get(row,["Future Index Short","FutureIndexShort"]))
        if longv is not None and shortv is not None: participant_net[label]=longv-shortv
    fii_agent={"status":"analysed" if participant_net or fii_rows else "missing_or_unmapped",
      "bias":"bullish" if any("FII" in k and v>0 for k,v in participant_net.items()) else "bearish" if any("FII" in k and v<0 for k,v in participant_net.items()) else "neutral",
      "evidence":{"participant_net_index_futures":participant_net,"fii_report_rows":len(fii_rows),
      "fii_scope":"Aggregate index-segment report, not NIFTY-only."}}

    vix_rows=_rows("vix_volatility",trade_date)
    vix_close=next((src._clean_number(src._get(r,["CLOSE","Close","ClsPric","India VIX","VIX"])) for r in vix_rows
                    if src._clean_number(src._get(r,["CLOSE","Close","ClsPric","India VIX","VIX"])) is not None),None)
    fut_rows=[]
    for r in _rows("fo_bhavcopy",trade_date):
        symbol=str(src._get(r,["TckrSymb","Symbol","Undrlyg"]) or "").upper()
        typ=str(src._get(r,["FinInstrmTp","Instrument","InstType"]) or "").upper()
        if symbol=="NIFTY" and "FUT" in typ:fut_rows.append(r)
    future=next((r for r in fut_rows if src._parse_date(src._get(r,["XpryDt","Expiry Date"]))==expiry),None)
    future_close=src._clean_number(src._get(future or {},["ClsPric","Close","SttlmPric"]))
    volatility_agent={"status":"analysed" if vix_close is not None or future_close is not None else "missing_or_unmapped",
      "bias":"neutral","evidence":{"india_vix_close":vix_close,"nifty_future_close":future_close,
      "future_basis_points":round(future_close-close,2) if future_close is not None else None}}

    # Transparent candle-pattern agent: last completed candle shape and recent directional count.
    last=recent[-1]; body=abs(last["close"]-last["open"]); span=max(last["high"]-last["low"],1e-9)
    lower=min(last["open"],last["close"])-last["low"]; upper=last["high"]-max(last["open"],last["close"])
    pattern_name="none"
    if lower>=max(body*2,span*.45) and upper<=span*.2 and last["close"]>=last["open"]: pattern_name="hammer_like"
    elif upper>=max(body*2,span*.45) and lower<=span*.2 and last["close"]<=last["open"]: pattern_name="shooting_star_like"
    pattern_agent={"status":"analysed","bias":"bullish" if pattern_name=="hammer_like" else "bearish" if pattern_name=="shooting_star_like" else "neutral",
      "evidence":{"latest_candle_pattern":pattern_name,"positive_sessions_last_20":sum(1 for x,y in zip(recent[-20:],recent[-19:]) if y["close"]>x["close"]),
      "negative_sessions_last_20":sum(1 for x,y in zip(recent[-20:],recent[-19:]) if y["close"]<x["close"])}}

    agents={"price_structure":price_agent,"options_expiry":option_agent,"participant_flow":fii_agent,
            "futures_volatility":volatility_agent,"candle_pattern":pattern_agent}
    weights={"price_structure":1.5,"options_expiry":1.5,"participant_flow":1.0,"futures_volatility":0.5,"candle_pattern":0.75}
    scores={"bullish":0.0,"bearish":0.0,"neutral":0.0}
    for name,agent in agents.items():
        if agent["status"]!="analysed":continue
        scores[agent["bias"]]+=weights[name]
    total=sum(scores.values()); bull=scores["bullish"]; bear=scores["bearish"]
    bias="bullish" if bull>bear and bull>=total*.45 else "bearish" if bear>bull and bear>=total*.45 else "range_or_wait"
    quality={"selected_trade_date":trade_date.isoformat(),"expiry":expiry.isoformat(),"spot_rows":len(selected),
      "option_contracts":len(contracts),"participant_rows":len(participant_rows),"fii_rows":len(fii_rows),
      "vix_rows":len(vix_rows),"agents_available":sum(1 for a in agents.values() if a["status"]=="analysed"),
      "agents_missing":[k for k,v in agents.items() if v["status"]!="analysed"]}
    report={"status":"completed","trade_date":trade_date.isoformat(),"next_session":src.next_nse_trading_session(trade_date).isoformat(),
      "expiry":expiry.isoformat(),"spot_close":close,"atm_strike":atm,"itm_strikes":itm_count,"otm_strikes":otm_count,
      "downloaded_sources":download_outcomes,"agents":agents,"coordinator":{"bias":bias,"weighted_scores":scores,
      "consensus_note":"Coordinator combines only agents with mapped data; unavailable agents do not vote.",
      "option_trade_plan":{"status":"skipped_no_live_market_feed","reason":"Selected-date EOD option prices cannot be treated as tomorrow's executable premium or entry."}},
      "data_quality":quality,"warning":"Historical end-of-day analysis. No live quote, guaranteed prediction, or executable option entry is generated."}
    with connect() as conn:
        conn.execute("""INSERT INTO selected_analysis_reports(trade_date,expiry,report)
          VALUES (%s,%s,%s) ON CONFLICT(trade_date,expiry) DO UPDATE SET report=EXCLUDED.report,created_at=NOW()""",
          (trade_date,expiry,json.dumps(report,default=str)))
    return report
