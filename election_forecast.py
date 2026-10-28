#!/usr/bin/env python3
"""Recommendation-only 2026 election forecast with reproducible joint simulation.

All Super Market calls in this module are GET requests.  The model never imports
or calls the live order executor.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "cache"
BASE = os.getenv("SUPER_MARKET_BASE_URL", "https://sig.thesuper.market/api/v1").rstrip("/")
TOURNAMENT = os.getenv("SUPER_MARKET_TOURNAMENT", "midterm-elections")
KEY = os.getenv("SUPER_MARKET_API_KEY", "")
SIMULATIONS = int(os.getenv("FORECAST_SIMULATIONS", "200000"))
SEED = int(os.getenv("FORECAST_SEED", "20261001"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.05"))
FEE_BPS = float(os.getenv("TRADING_COST_BPS", "0"))
MAX_ORDER = float(os.getenv("MAX_ORDER_SIZE", "1000"))
MAX_MARKET = float(os.getenv("MAX_EXPOSURE_PER_MARKET", "2000"))
MAX_TOTAL = float(os.getenv("MAX_TOTAL_EXPOSURE", "10000"))
ELECTION_DAY = date(2026, 11, 3)

PUBLIC_SOURCES = {
    "races": "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/main/races.csv",
    "polls": "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/main/race_polls.csv",
    "generic": "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/main/gcb_polls.csv",
    "history": "https://raw.githubusercontent.com/ElectIndex/26_us_forecast_data/main/historical.csv",
    "validation": "https://raw.githubusercontent.com/fivethirtyeight/data/master/forecast-methodology/historical-senate-predictions.csv",
}
UPSTREAM_REFERENCES = [
    "https://electionlab.mit.edu/data",
    "https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/",
    "https://github.com/ElectIndex/26_us_forecast_data",
    "https://github.com/fivethirtyeight/data/tree/master/forecast-methodology",
]

STATE_NAMES = {
    "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA","Colorado":"CO",
    "Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA","Hawaii":"HI","Idaho":"ID",
    "Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY","Louisiana":"LA",
    "Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
    "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV",
    "New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC",
    "North Dakota":"ND","Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA",
    "Rhode Island":"RI","South Carolina":"SC","South Dakota":"SD","Tennessee":"TN","Texas":"TX",
    "Utah":"UT","Vermont":"VT","Virginia":"VA","Washington":"WA","West Virginia":"WV",
    "Wisconsin":"WI","Wyoming":"WY",
}
REGIONS = {
    "NE": {"CT","ME","MA","NH","RI","VT","NJ","NY","PA"},
    "MW": {"IL","IN","IA","KS","MI","MN","MO","NE","ND","OH","SD","WI"},
    "S": {"AL","AR","DE","FL","GA","KY","LA","MD","MS","NC","OK","SC","TN","TX","VA","WV"},
    "W": {"AK","AZ","CA","CO","HI","ID","MT","NV","NM","OR","UT","WA","WY"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def rows_from_csv(raw: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


class PublicCache:
    def __init__(self, cache_dir: Path = CACHE, max_age_hours: float = 6.0):
        self.cache_dir = cache_dir
        self.max_age = max_age_hours * 3600
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.provenance: dict[str, dict[str, Any]] = {}

    def fetch(self, name: str, url: str) -> bytes:
        path = self.cache_dir / f"{name}.csv"
        meta_path = self.cache_dir / f"{name}.meta.json"
        now = time.time()
        if path.exists() and now - path.stat().st_mtime <= self.max_age:
            raw = path.read_bytes()
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            meta.update({"url": url, "cache_hit": True, "sha256": hashlib.sha256(raw).hexdigest()})
            self.provenance[name] = meta
            return raw

        headers = {"Accept": "text/csv", "User-Agent": "sig-cup-election-forecast/2.0"}
        if meta_path.exists():
            old = json.loads(meta_path.read_text(encoding="utf-8"))
            if old.get("etag"): headers["If-None-Match"] = old["etag"]
            if old.get("last_modified"): headers["If-Modified-Since"] = old["last_modified"]
        else:
            old = {}
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=45) as response:
                    raw = response.read()
                    if not raw: raise RuntimeError(f"empty public dataset: {name}")
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(raw); tmp.replace(path)
                    meta = {
                        "url": url,
                        "fetched_at": utcnow().isoformat(),
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "cache_hit": False,
                    }
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    self.provenance[name] = meta
                    return raw
            except urllib.error.HTTPError as error:
                if error.code == 304 and path.exists():
                    raw = path.read_bytes(); os.utime(path, None)
                    old.update({"url": url, "cache_hit": True, "validated_at": utcnow().isoformat()})
                    self.provenance[name] = old
                    return raw
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    if path.exists():
                        raw = path.read_bytes()
                        self.provenance[name] = {**old, "url": url, "cache_hit": True, "stale_cache": True,
                                                 "warning": f"refresh HTTP {error.code}"}
                        return raw
                    raise
                retry = error.headers.get("Retry-After")
                delay = float(retry) if retry and retry.isdigit() else min(16, 2 ** attempt)
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    if path.exists():
                        raw = path.read_bytes()
                        self.provenance[name] = {**old, "url": url, "cache_hit": True, "stale_cache": True,
                                                 "warning": "refresh network failure"}
                        return raw
                    raise
                delay = min(16, 2 ** attempt)
            time.sleep(delay)
        raise RuntimeError(f"could not load {name}")


class ReadOnlyAPI:
    # The account-wide limit is ~100 reads/min shared by every key; 0.65s keeps this under it
    # even while sigbot runs alongside. A 429 costs a 60s lockout.
    def __init__(self, key: str, base: str = BASE, min_interval: float = 0.65):
        if not key: raise SystemExit("SUPER_MARKET_API_KEY is missing.")
        self.key = key; self.base = base; self.min_interval = min_interval; self.last = 0.0
        self.lock = threading.Lock()

    def get(self, path: str) -> Any:
        for attempt in range(4):
            with self.lock:
                wait = self.min_interval - (time.monotonic() - self.last)
                if wait > 0: time.sleep(wait)
                self.last = time.monotonic()
            req = urllib.request.Request(self.base + path, headers={
                "Authorization": f"Bearer {self.key}", "Accept": "application/json",
                "User-Agent": "sig-cup-election-forecast/2.0"}, method="GET")
            assert req.method == "GET"
            try:
                with urllib.request.urlopen(req, timeout=35) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                body = error.read().decode("utf-8", "replace")[:500]
                if error.code in (401, 403): raise RuntimeError(f"read API rejected credential (HTTP {error.code})")
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f"GET {path} failed HTTP {error.code}: {body}") from error
                retry = error.headers.get("Retry-After")
                delay = float(retry) if retry and retry.isdigit() else min(12, 2 ** attempt)
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == 3: raise RuntimeError(f"GET {path} failed after bounded retries") from error
                delay = min(12, 2 ** attempt)
            time.sleep(delay)
        raise RuntimeError(f"GET {path} failed")


@dataclass
class RaceInput:
    code: str
    race_type: str
    state: str
    region: str
    mean_margin: float
    national_loading: float
    regional_sigma: float
    race_sigma: float
    mean_uncertainty: float
    polls_used: int
    latest_poll: str | None
    baseline_margin: float
    poll_margin: float | None
    incumbent_party: str | None
    caveat: str | None = None


@dataclass
class RaceForecast:
    p_dem: float
    model_low: float
    model_high: float
    simulation_se: float
    margin_mean: float
    margin_sd: float
    polls_used: int
    latest_poll: str | None
    caveat: str | None = None


def race_caveat(race: dict[str, str]) -> str | None:
    """Races where a two-party Dem-minus-Rep margin does not describe who wins.

    The race is still simulated (chamber totals need every seat) but its per-race probability
    must not be traded on its own.
    """
    reasons = []
    if str(race.get("threeway", "")).lower() == "true": reasons.append("competitive three-way race")
    if str(race.get("rcv", "")).lower() == "true": reasons.append("ranked-choice/top-four election")
    if race.get("same_party"): reasons.append(f"same-party general ({race['same_party']})")
    for side in ("dem_name", "rep_name"):
        if (race.get(side) or "").lower().startswith("(no "): reasons.append(f"no {side[:3].upper()} nominee")
    if parse_float(race.get("ind_frac"), 0) and parse_float(race.get("ind_frac"), 0) > 0:
        reasons.append("independent with major-party support")
    return "; ".join(dict.fromkeys(reasons)) or None


def state_region(state: str) -> str:
    return next((name for name, states in REGIONS.items() if state in states), "OTHER")


def weighted_average(values: list[tuple[float, float]]) -> tuple[float | None, float]:
    total = sum(w for _, w in values)
    if total <= 0: return None, 0.0
    mean = sum(v * w for v, w in values) / total
    return mean, total


def generic_ballot(rows: list[dict[str, str]], as_of: date) -> tuple[float, float, str | None, int]:
    weighted = []
    latest = None
    used = 0
    for row in rows:
        try: end = date.fromisoformat(row["end_date"])
        except (KeyError, ValueError): continue
        if end > as_of or (as_of - end).days > 90: continue
        dem, rep = parse_float(row.get("dem")), parse_float(row.get("rep"))
        if dem is None or rep is None: continue
        n = max(100.0, parse_float(row.get("n"), 600.0) or 600.0)
        rating = max(0.5, parse_float(row.get("rating"), 1.0) or 1.0)
        age_weight = math.exp(-(as_of - end).days / 21.0)
        sponsor_weight = 0.55 if row.get("lean") in ("D", "R") else 1.0
        w = age_weight * rating * math.sqrt(n) * sponsor_weight
        weighted.append((dem - rep, w)); used += 1
        latest = max(latest, end) if latest else end
    mean, eff = weighted_average(weighted)
    if mean is None: raise RuntimeError("No generic-ballot polls dated on or before the forecast timestamp.")
    sampling_se = max(0.6, 4.0 / math.sqrt(max(1.0, eff / 20.0)))
    return mean, sampling_se, latest.isoformat() if latest else None, used


def build_race_inputs(races: list[dict[str, str]], polls: list[dict[str, str]], history: list[dict[str, str]],
                      generic_margin: float, generic_se: float, as_of: date) -> list[RaceInput]:
    history_by = {r.get("code"): r for r in history}
    polls_by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for poll in polls: polls_by[poll.get("race_code", "")].append(poll)
    days = max(0, (ELECTION_DAY - as_of).days)
    output = []
    for race in races:
        code = race.get("race_code", ""); race_type = race.get("race_type", "")
        if race_type not in ("house", "senate", "governor"): continue
        h = history_by.get(code if race_type == "house" else race.get("state", ""))
        if not h: continue
        margins = [parse_float(h.get("margin_2016")), parse_float(h.get("margin_2020")), parse_float(h.get("margin_2024"))]
        if any(x is None for x in margins): continue
        baseline = .15 * margins[0] + .30 * margins[1] + .55 * margins[2]
        elasticity = {"house": 1.0, "senate": .80, "governor": .60}[race_type]
        fundamental = baseline + elasticity * (generic_margin - (-1.48))
        incumbent = (race.get("incumbent_party") or "").upper() or None
        if incumbent == "DEM": fundamental += 1.5
        elif incumbent == "REP": fundamental -= 1.5

        values=[]; latest=None; unique=set()
        for poll in polls_by.get(code, []):
            try: end=date.fromisoformat(poll["end_date"])
            except (KeyError, ValueError): continue
            if end > as_of or (as_of-end).days > 180: continue
            margin=parse_float(poll.get("margin")); n=max(100.0,parse_float(poll.get("n"),600.0) or 600.0)
            rating=max(.5,parse_float(poll.get("rating"),1.0) or 1.0)
            if margin is None: continue
            key=(poll.get("poll_id"),poll.get("dem_name"),poll.get("rep_name"))
            if key in unique: continue
            unique.add(key)
            w=math.exp(-(as_of-end).days/35.0)*rating*math.sqrt(n)*(0.55 if poll.get("lean") in ("D","R") else 1.0)
            values.append((margin,w)); latest=max(latest,end) if latest else end
        poll_mean, poll_eff=weighted_average(values)
        poll_count=len(values)
        if poll_mean is None:
            weight=0.0; mean=fundamental; poll_sampling=5.0
        else:
            weight=min(.78, poll_count/(poll_count+4.0+days/50.0))
            mean=(1-weight)*fundamental+weight*poll_mean
            poll_sampling=max(1.0,5.0/math.sqrt(max(1.0,poll_eff/20.0)))
        national_loading=1.0 if race_type=="house" else (.8 if race_type=="senate" else .6)
        regional_sigma=1.8 if race_type!="governor" else 2.2
        base_race_sigma={"house":4.8,"senate":4.3,"governor":5.2}[race_type]
        race_sigma=max(2.8,base_race_sigma*(1-.35*weight)+.8*math.sqrt(days/365.0))
        mean_uncertainty=math.sqrt(((1-weight)*2.5)**2+(weight*poll_sampling)**2+(generic_se*elasticity)**2)
        output.append(RaceInput(code,race_type,race.get("state",""),state_region(race.get("state","")),mean,
                                national_loading,regional_sigma,race_sigma,mean_uncertainty,poll_count,
                                latest.isoformat() if latest else None,baseline,poll_mean,incumbent,race_caveat(race)))
    return output


def normal_cdf(x: float) -> float:
    return .5 * (1 + math.erf(x / math.sqrt(2)))


def simulate(inputs: list[RaceInput], simulations: int, seed: int) -> tuple[dict[str, RaceForecast], dict[str, Any]]:
    if simulations != 200000:
        raise ValueError("This validated runner requires exactly 200,000 simulations.")
    rng=np.random.default_rng(seed)
    n=len(inputs); means=np.array([x.mean_margin for x in inputs]); nat=np.array([x.national_loading for x in inputs])
    reg_sigma=np.array([x.regional_sigma for x in inputs]); race_sigma=np.array([x.race_sigma for x in inputs])
    regions=sorted({x.region for x in inputs}); reg_idx=np.array([regions.index(x.region) for x in inputs])
    wins=np.zeros(n,dtype=np.int64); house_control=0; senate_control=0
    margin_sum=np.zeros(n); margin_sq=np.zeros(n)
    code_index={x.code:i for i,x in enumerate(inputs)}
    house_idx=np.array([i for i,x in enumerate(inputs) if x.race_type=="house"])
    senate_idx=np.array([i for i,x in enumerate(inputs) if x.race_type=="senate"])
    done=0; chunk=5000
    while done<simulations:
        size=min(chunk,simulations-done)
        national=rng.normal(0,2.5,size=(size,1))
        regional=rng.normal(0,1,size=(size,len(regions)))
        race_noise=rng.normal(0,1,size=(size,n))
        margins=means+national*nat+regional[:,reg_idx]*reg_sigma+race_noise*race_sigma
        won=margins>0
        wins+=won.sum(axis=0); margin_sum+=margins.sum(axis=0); margin_sq+=(margins*margins).sum(axis=0)
        if house_idx.size: house_control+=int((won[:,house_idx].sum(axis=1)>=218).sum())
        if senate_idx.size: senate_control+=int((34+won[:,senate_idx].sum(axis=1)>=51).sum())
        done+=size
    forecasts={}
    for i,x in enumerate(inputs):
        p=float(wins[i]/simulations); sd=math.sqrt(max(0,margin_sq[i]/simulations-(margin_sum[i]/simulations)**2))
        low=normal_cdf((x.mean_margin-1.96*x.mean_uncertainty)/sd)
        high=normal_cdf((x.mean_margin+1.96*x.mean_uncertainty)/sd)
        forecasts[x.code]=RaceForecast(p,max(0,low),min(1,high),math.sqrt(p*(1-p)/simulations),x.mean_margin,sd,
                                       x.polls_used,x.latest_poll,x.caveat)
    chambers={
        "US House": {"p_dem":house_control/simulations},
        "US Senate": {"p_dem":senate_control/simulations},
        "simulations":simulations,"seed":seed,
    }
    for value in chambers.values():
        if isinstance(value,dict) and "p_dem" in value:
            p=value["p_dem"]; value["simulation_se"]=math.sqrt(p*(1-p)/simulations)
            value["model_low"]=max(0,p-.08); value["model_high"]=min(1,p+.08)
    return forecasts,chambers


def score_metrics(items: list[tuple[float,int]]) -> dict[str,float]:
    if not items: return {"n":0,"brier":math.nan,"log_loss":math.nan}
    eps=1e-6; brier=sum((p-y)**2 for p,y in items)/len(items)
    loss=-sum(y*math.log(min(1-eps,max(eps,p)))+(1-y)*math.log(min(1-eps,max(eps,1-p))) for p,y in items)/len(items)
    return {"n":len(items),"brier":brier,"log_loss":loss}


def validation_report(rows: list[dict[str,str]]) -> dict[str,Any]:
    events=[]
    for r in rows:
        p=parse_float(r.get("forecast_prob")); y=parse_float(r.get("winflag")); year=parse_float(r.get("year"))
        if p is None or y is None or year is None: continue
        events.append((int(year),min(.999,max(.001,p)),int(y)))
    years=sorted({year for year,_,_ in events}); holdouts=[]
    for year in years[1:]:
        train=[(p,y) for yr,p,y in events if yr<year]
        test=[(p,y) for yr,p,y in events if yr==year]
        if len(train)<20 or not test: continue
        # Shrink probabilities toward 0.5; select strength only from prior years.
        candidates=[i/20 for i in range(21)]
        strength=min(candidates,key=lambda a:score_metrics([(.5+a*(p-.5),y) for p,y in train])["log_loss"])
        calibrated=[(.5+strength*(p-.5),y) for p,y in test]
        holdouts.append({"year":year,"shrinkage":strength,"model":score_metrics(calibrated),
                         "raw_source_probability":score_metrics(test),"baseline_50":score_metrics([(.5,y) for _,y in test])})
    combined=[]; raw=[]; base=[]
    for h in holdouts:
        year=h["year"]; strength=h["shrinkage"]
        test=[(p,y) for yr,p,y in events if yr==year]
        combined.extend((.5+strength*(p-.5),y) for p,y in test); raw.extend(test); base.extend((.5,y) for _,y in test)
    calibration=[]
    for lo in np.arange(0,1,.1):
        bucket=[(p,y) for p,y in combined if lo<=p<(lo+.1) or (lo==.9 and p==1)]
        if bucket: calibration.append({"predicted":sum(p for p,_ in bucket)/len(bucket),"observed":sum(y for _,y in bucket)/len(bucket),"n":len(bucket)})
    return {"method":"expanding-window year holdouts; calibration shrinkage fitted only on earlier years",
            "holdouts":holdouts,"aggregate":{"calibrated_reference":score_metrics(combined),"raw_source_probability":score_metrics(raw),
            "baseline_50":score_metrics(base)},"calibration":calibration,
            "limitation":"This public file contains historical third-party forecast probabilities, not the feature snapshots needed to directly backtest this model. Metrics validate only the conservative probability-shrinkage procedure; they are not evidence that the 2026 structural model has the same accuracy."}


def market_event(title: str) -> tuple[str,str,str] | None:
    party_match=re.search(r"Will the (Democratic|Republican|Independent) Party win the (.+?)\?*$",title,re.I)
    if not party_match: return None
    party={"dem":"DEM","rep":"REP","ind":"IND"}[party_match.group(1).lower()[:3]]
    target=party_match.group(2).strip()
    if target in ("U.S. House","US House"): return "chamber","US House",party
    if target in ("U.S. Senate","US Senate"): return "chamber","US Senate",party
    m=re.fullmatch(r"(.+?) (Senate|Governor)",target,re.I)
    if m and m.group(1) in STATE_NAMES:
        return "race",f"{STATE_NAMES[m.group(1)]}-{'SEN' if m.group(2).lower()=='senate' else 'GOV'}",party
    m=re.fullmatch(r"([A-Z]{2})-(\d{1,2}) House race",target,re.I)
    if m: return "race",f"{m.group(1).upper()}-{int(m.group(2)):02d}",party
    return None


def levels(book: dict, *names: str) -> list:
    for name in names:
        if isinstance(book.get(name),list): return book[name]
    return []


def level_value(level: Any, names: tuple[str,...], index: int) -> float | None:
    if isinstance(level,(list,tuple)) and len(level)>index: return parse_float(level[index])
    if isinstance(level,dict):
        for name in names:
            if level.get(name) is not None: return parse_float(level[name])
    return None


def executable_book(book: dict) -> dict[str,float|None]:
    ya=levels(book,"yesAsks","asks","sellYes"); yb=levels(book,"yesBids","bids","buyYes")
    na=levels(book,"noAsks","sellNo"); nb=levels(book,"noBids","buyNo")
    yes_ask=level_value(ya[0],("price","px","limitPrice"),0) if ya else None
    yes_bid=level_value(yb[0],("price","px","limitPrice"),0) if yb else None
    no_ask=level_value(na[0],("price","px","limitPrice"),0) if na else (1-yes_bid if yes_bid is not None else None)
    no_bid=level_value(nb[0],("price","px","limitPrice"),0) if nb else (1-yes_ask if yes_ask is not None else None)
    return {"yes_ask":yes_ask,"yes_bid":yes_bid,"no_ask":no_ask,"no_bid":no_bid,
            "yes_qty":level_value(ya[0],("quantity","qty","size","shares"),1) if ya else None,
            "no_qty":level_value(na[0],("quantity","qty","size","shares"),1) if na else (level_value(yb[0],("quantity","qty","size","shares"),1) if yb else None)}


def unwrap_rows(value: Any, names=("data","orders","positions")) -> list:
    if isinstance(value,list): return value
    if isinstance(value,dict):
        for name in names:
            if isinstance(value.get(name),list): return value[name]
    return []


def fetch_markets(api: ReadOnlyAPI) -> tuple[list[dict],str]:
    tournament_response=api.get(f"/tournaments/{TOURNAMENT}")
    tournament=tournament_response.get("data",tournament_response); tid=str(tournament["id"])
    markets=[]; cursor=None
    while True:
        query={"limit":20,"status":"open"}
        if cursor: query["cursor"]=cursor
        page=api.get(f"/tournaments/{TOURNAMENT}/markets?"+urllib.parse.urlencode(query))
        markets.extend(page.get("data",[])); pagination=page.get("pagination",{})
        cursor=pagination.get("nextCursor")
        if not pagination.get("hasMore"): break
    return markets,tid


def resolution_text(nodes: dict) -> str:
    """Resolution wording lives on the contract leaves of GET /markets/{id}/nodes, not on /markets/{id}.
    `contract_details` has no fixed shape, so collect every string in it (plus the leaf title)."""
    parts: list[str] = []
    def strings(value: Any) -> None:
        if isinstance(value, str) and value.strip(): parts.append(value.strip())
        elif isinstance(value, dict):
            for v in value.values(): strings(v)
        elif isinstance(value, list):
            for v in value: strings(v)
    def walk(node: Any) -> None:
        if not isinstance(node, dict): return
        if node.get("node_type") == "contract":
            strings(node.get("contract_details"))
            if node.get("title"): parts.append(str(node["title"]).strip())
        for child in node.get("children") or []: walk(child)
    walk(nodes.get("root", nodes.get("data", {}).get("root") if isinstance(nodes.get("data"), dict) else None))
    return "\n".join(dict.fromkeys(parts))


def yes_exchange(market: dict) -> str | None:
    exchanges = market.get("exchanges") or []
    if not exchanges: return None
    return str(next((x for x in exchanges if str(x.get("option") or "YES").upper() == "YES"), exchanges[0])["id"])


def load_criteria(api: ReadOnlyAPI, markets: list[dict]) -> tuple[dict[str,str],dict[str,str]]:
    """Resolution text barely changes, so it is cached on disk; only new markets cost a read."""
    path = CACHE / "criteria.json"
    cached: dict[str,str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    errors: dict[str,str] = {}
    missing = [m for m in markets if str(m["id"]) not in cached]
    if missing: print(f"Reading resolution criteria for {len(missing)} new markets (cached afterwards)...", flush=True)
    for i, market in enumerate(missing, 1):
        try:
            cached[str(market["id"])] = resolution_text(api.get(f"/markets/{market['id']}/nodes"))
        except RuntimeError as error:
            errors[str(market["id"])] = str(error)
        if i % 25 == 0 or i == len(missing):
            path.write_text(json.dumps(cached, indent=1), encoding="utf-8")
            print(f"  criteria {i}/{len(missing)}", flush=True)
    return cached, errors


def bulk_quotes(api: ReadOnlyAPI, exchange_ids: list[str], tid: str) -> dict[str,dict]:
    out: dict[str,dict] = {}
    for i in range(0, len(exchange_ids), 100):
        query = urllib.parse.urlencode({"ids": ",".join(exchange_ids[i:i+100]), "tournamentId": tid})
        for row in api.get("/exchanges/prices?" + query).get("data", []):
            out[str(row["exchangeId"])] = row
    return out


def quote_book(quote: dict) -> dict[str,float|None]:
    """Top of book from the bulk endpoint, YES-normalized; sizes are filled in later from the full book."""
    bid, ask = parse_float(quote.get("bestBid")), parse_float(quote.get("bestAsk"))
    return {"yes_ask": ask, "yes_bid": bid, "no_ask": round(1-bid, 10) if bid is not None else None,
            "no_bid": round(1-ask, 10) if ask is not None else None, "yes_qty": None, "no_qty": None}


def make_recommendations(api: ReadOnlyAPI, market_rows: list[dict], tid: str, forecasts: dict[str,RaceForecast],
                         chambers: dict[str,Any], run_at: str) -> tuple[list[dict],dict[str,Any]]:
    mapped_markets=[m for m in market_rows if market_event(str(m.get("title","")))]
    criteria, criteria_errors = load_criteria(api, mapped_markets)
    xids = {str(m["id"]): yes_exchange(m) for m in mapped_markets}
    quotes = bulk_quotes(api, [x for x in xids.values() if x], tid)
    snapshots={}
    for market in mapped_markets:
        mid = str(market["id"]); xid = xids[mid]
        if not xid: snapshots[mid] = {"error": "No exchange supplied by tournament market endpoint."}; continue
        if xid not in quotes: snapshots[mid] = {"error": f"No quote for exchange {xid} in this tournament."}; continue
        text = criteria.get(mid, ""); source = "nodes"
        if not text and mid not in criteria_errors:
            # Contract carries no stored rules: the title itself names the party and the race.
            text = str(market.get("title","")); source = "title"
        snapshots[mid] = {"criteria": text, "criteria_source": source, "exchange_id": xid, "book": quote_book(quotes[xid])}
        if mid in criteria_errors: snapshots[mid] = {"error": "criteria read failed: " + criteria_errors[mid]}
    positions=unwrap_rows(api.get(f"/tournaments/{TOURNAMENT}/portfolio/positions"))
    orders=unwrap_rows(api.get("/orders?"+urllib.parse.urlencode({"limit":200,"tournamentId":tid})))
    existing_exposure=defaultdict(float); total_existing=0.0
    for p in positions:
        cost=abs(parse_float(p.get("costBasis",p.get("marketValue",0)),0) or 0)
        mapped=market_event(str(p.get("marketTitle",p.get("market_title",""))))
        group=f"{mapped[0]}:{mapped[1]}" if mapped else str(p.get("marketId",p.get("market_id","unknown")))
        existing_exposure[group]+=cost; total_existing+=cost
    open_orders=[o for o in orders if o.get("open", True)]
    fills=[]; fill_errors=[]
    for order in orders:
        order_id=order.get("orderId",order.get("id"))
        if order_id is None: continue
        try:
            response=api.get(f"/orders/{order_id}/fills")
            fills.extend(unwrap_rows(response,("data","fills")))
        except RuntimeError as error:
            fill_errors.append({"order_id":order_id,"error":str(error)})
    results=[]
    for market in market_rows:
        title=str(market.get("title","")); mapping=market_event(title)
        item={"market":title,"market_id":str(market.get("id")),"exchange_id":yes_exchange(market),
              "recommendation":"NO TRADE","reason":""}
        if not mapping:
            item["reason"]="No verified mapping from contract title to a modeled election outcome."; results.append(item); continue
        kind,key,party=mapping
        item["mapping"]={"kind":kind,"key":key,"yes_party":party}
        if kind=="race":
            f=forecasts.get(key)
            if not f: item["reason"]=f"No complete polling/baseline input for {key}."; results.append(item); continue
            caveat=f.caveat if party!="IND" else "independent candidate: the model has no independent vote share"
            if caveat:
                item.update({"model_caveat":caveat,
                             "reason":f"Two-party model does not apply ({caveat}); use outside markets only."})
                results.append(item); continue
            p_dem,lo_dem,hi_dem=f.p_dem,f.model_low,f.model_high
            metadata={"polls_used":f.polls_used,"latest_poll":f.latest_poll,"margin_mean":f.margin_mean,"margin_sd":f.margin_sd,
                      "simulation_se":f.simulation_se}
        else:
            f=chambers.get(key)
            if not f: item["reason"]=f"No simulated chamber result for {key}."; results.append(item); continue
            p_dem,lo_dem,hi_dem=f["p_dem"],f["model_low"],f["model_high"]
            metadata={"polls_used":None,"latest_poll":None,"simulation_se":f["simulation_se"]}
        p_yes=p_dem if party=="DEM" else 1-p_dem
        low=lo_dem if party=="DEM" else 1-hi_dem; high=hi_dem if party=="DEM" else 1-lo_dem
        snapshot=snapshots.get(str(market.get("id")),{"error":"snapshot missing"})
        if snapshot.get("error"):
            item.update({"estimated_probability":p_yes,"uncertainty_range":[low,high],
                         "reason":"Read-only market snapshot failed: "+snapshot["error"]}); results.append(item); continue
        criteria=snapshot["criteria"]
        if not criteria:
            item.update({"estimated_probability":p_yes,"uncertainty_range":[low,high],"reason":"Resolution criteria missing; mapping cannot be verified."}); results.append(item); continue
        party_word="democrat" if party=="DEM" else "republican"
        if party_word not in criteria.lower():
            item.update({"estimated_probability":p_yes,"uncertainty_range":[low,high],
                         "resolution_criteria":criteria,"reason":f"Resolution criteria do not explicitly confirm the {party} party mapping."})
            results.append(item); continue
        xid=snapshot["exchange_id"]; px=snapshot["book"]; fee=FEE_BPS/10000
        yes_edge=(low-px["yes_ask"]-fee) if px["yes_ask"] is not None else None
        no_edge=((1-high)-px["no_ask"]-fee) if px["no_ask"] is not None else None
        if max(e for e in (yes_edge,no_edge,-1) if e is not None)>=MIN_EDGE:
            # Only markets that clear the edge cost a full-book read (for executable size).
            try:
                full=executable_book(api.get(f"/exchanges/{xid}/orderbook?"+urllib.parse.urlencode({"tournamentId":tid})))
                px={**px,"yes_qty":full["yes_qty"],"no_qty":full["no_qty"]}
            except RuntimeError as error:
                item["book_error"]=str(error)
        choices=[]
        if yes_edge is not None: choices.append((yes_edge,"BUY YES",px["yes_ask"],px["yes_qty"]))
        if no_edge is not None: choices.append((no_edge,"BUY NO",px["no_ask"],px["no_qty"]))
        best=max(choices,default=None)
        group=f"{kind}:{key}"; group_existing=existing_exposure.get(group,0)
        reason=f"Mapped to {key} {party}; conservative edge uses the adverse end of the model interval and executable ask."
        quantity=0
        if best and best[0]>=MIN_EDGE and best[3] and best[3]>0:
            edge,side,price,liquidity=best
            budget=min(MAX_ORDER,max(0,MAX_MARKET-group_existing),max(0,MAX_TOTAL-total_existing))
            quantity=max(0,int(min(math.floor(budget/price),math.floor(liquidity)))) if price>0 else 0
            if quantity: item["recommendation"]=side
            else: reason="Positive conservative edge, but no capacity or executable liquidity within limits."
        elif best:
            edge,side,price,liquidity=best; reason+=f" Best conservative edge {edge:.1%} is below {MIN_EDGE:.1%}."
        else:
            edge=price=liquidity=None; reason="No executable YES or NO ask."
        spread=None
        if px["yes_ask"] is not None and px["yes_bid"] is not None: spread=px["yes_ask"]-px["yes_bid"]
        item.update({"exchange_id":xid,"resolution_criteria":criteria,"criteria_source":snapshot.get("criteria_source"),
                     "price":price,"estimated_probability":p_yes,"uncertainty_range":[low,high],"estimated_edge":edge,
                     "quantity":quantity,"available_liquidity":liquidity,"yes_ask":px["yes_ask"],"no_ask":px["no_ask"],
                     "spread":spread,"fees":fee,"data_timestamp":run_at,"model":metadata,"exposure_group":group,"reason":reason})
        results.append(item)
    # Equivalent/complementary contracts share one exposure group. Keep only the strongest actionable edge.
    grouped=defaultdict(list)
    for item in results:
        if item.get("recommendation")!="NO TRADE": grouped[item.get("exposure_group")].append(item)
    for group,items in grouped.items():
        keep=max(items,key=lambda x:x.get("estimated_edge") or -99)
        for item in items:
            if item is not keep:
                item["recommendation"]="NO TRADE"; item["quantity"]=0
                item["reason"]="Equivalent exposure suppressed in favor of the higher-edge contract in this group."
    results.sort(key=lambda x:(x.get("recommendation")=="NO TRADE",-(x.get("estimated_edge") or -99)))
    reconciliation={"positions_seen":len(positions),"orders_seen":len(orders),"open_orders_seen":len(open_orders),
                    "fills_seen":len(fills),"fill_read_errors":fill_errors,"existing_exposure":total_existing,
                    "orders": [{k:o.get(k) for k in ("id","orderId","status","exchangeId","side","quantity","price")} for o in open_orders]}
    return results,reconciliation


KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_API = "https://gamma-api.polymarket.com"
# Kalshi and Polymarket are real-money markets; this model's validation is only a calibration
# benchmark (see validation_report), so the blend leans on whichever market lists the race.
FAIR_WEIGHTS = {"kalshi": 3, "polymarket": 3, "model": 1}
STATE_SLUGS = {abbr: name.lower().replace(" ", "-") for name, abbr in STATE_NAMES.items()}
TAKE_EDGE_WITH_KALSHI = float(os.getenv("TAKE_EDGE_WITH_KALSHI", "0.03"))
TAKE_EDGE_MODEL_ONLY = float(os.getenv("TAKE_EDGE_MODEL_ONLY", "0.08"))


def kalshi_candidates(kind: str, key: str, party: str) -> list[str]:
    if party == "IND": return []   # irregular tickers (SENATENE-26-DOSB); see kalshi_independents
    s = "D" if party == "DEM" else "R"
    if kind == "chamber":
        return [f"CONTROL{'H' if key == 'US House' else 'S'}-2026-{s}"]
    state, seat = key.split("-")
    if seat == "SEN": return [f"SENATE{state}-26-{s}"]
    if seat == "GOV": return [f"GOVPARTY{state}-26-{s}", f"GOVPARTY{state}-27-{s}"]  # KS is dated by inauguration
    return [f"HOUSE{state}{int(seat)}-26-{s}", f"HOUSE{state}AL-26-{s}"]


def polymarket_candidates(kind: str, key: str, party: str) -> list[str]:
    """Polymarket's election market slugs follow fixed patterns (checked 2026-10-26), e.g.
    will-the-republican-party-win-the-co-04-house-seat, will-the-democrats-win-the-maine-senate-race-in-2026."""
    if party == "IND": return []   # candidate-named markets; no fixed pattern
    adj, plural = ("democratic", "democrats") if party == "DEM" else ("republican", "republicans")
    if kind == "chamber":
        chamber = "house" if key == "US House" else "senate"
        return [f"will-the-{adj}-party-control-the-{chamber}-after-the-2026-midterm-elections"]
    state, seat = key.split("-")
    if seat in ("SEN", "GOV"):
        office = "senate" if seat == "SEN" else "governor"
        return [f"will-the-{plural}-win-the-{STATE_SLUGS[state]}-{office}-race-in-2026"]
    return [f"will-the-{adj}-party-win-the-{state.lower()}-{seat}-house-seat",
            f"will-the-{adj}-party-win-the-{state.lower()}-al-house-seat"]   # at-large seats


def verify_polymarket(slugs: list[str]) -> set[str]:
    """Keeps only slugs Polymarket lists as open with prices (bulk lookup, 25 per call)."""
    live: set[str] = set()
    unique = sorted(set(slugs))
    for i in range(0, len(unique), 25):
        url = f"{POLYMARKET_API}/markets?" + urllib.parse.urlencode([("slug", s) for s in unique[i:i+25]] + [("limit", 50)])
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sig-cup-election-forecast/2.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                live |= {m["slug"] for m in json.load(response)
                         if m.get("active") and not m.get("closed") and m.get("outcomePrices")}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            print(f"  Polymarket lookup failed for batch {i//25+1}: {error}", flush=True)
        time.sleep(0.25)
    return live


def verify_kalshi(tickers: list[str]) -> set[str]:
    """Keeps only tickers Kalshi actually lists as active (bulk lookup, 50 per call)."""
    live: set[str] = set()
    unique = sorted(set(tickers))
    for i in range(0, len(unique), 50):
        url = f"{KALSHI_API}/markets?" + urllib.parse.urlencode({"tickers": ",".join(unique[i:i+50]), "limit": 100})
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sig-cup-election-forecast/2.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                live |= {m["ticker"] for m in json.load(response).get("markets", []) if m.get("status") in ("active", "open")}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            print(f"  Kalshi lookup failed for batch {i//50+1}: {error}", flush=True)
        time.sleep(0.25)
    return live


def kalshi_independents(keys: list[str], ind_names: dict[str,str]) -> dict[str,str]:
    """Independent candidates get ad-hoc Kalshi suffixes, so match the candidate's surname
    against each 2026 contract's yes_sub_title in that race's series."""
    found: dict[str,str] = {}
    for key in keys:
        name = (ind_names.get(key) or "").strip()
        state, seat = key.split("-")
        if not name or seat not in ("SEN", "GOV"): continue
        series = f"SENATE{state}" if seat == "SEN" else f"GOVPARTY{state}"
        url = f"{KALSHI_API}/markets?" + urllib.parse.urlencode({"series_ticker": series, "status": "open", "limit": 50})
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sig-cup-election-forecast/2.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                rows = json.load(response).get("markets", [])
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue
        surname = name.split()[-1].lower().strip(".,")
        match = [m["ticker"] for m in rows if "-26-" in m["ticker"] and surname in str(m.get("yes_sub_title", "")).lower()]
        if len(match) == 1: found[key] = match[0]
        time.sleep(0.25)
    return found


def fair_values(recommendations: list[dict], run_at: str, ind_names: dict[str,str] | None = None) -> dict[str,Any]:
    """sigbot input: one entry per SIG YES exchange, keyed by exchange id."""
    wanted = {r["exchange_id"]: kalshi_candidates(**{k: r["mapping"][k] for k in ("kind","key")}, party=r["mapping"]["yes_party"])
              for r in recommendations if r.get("mapping") and r.get("exchange_id")}
    print(f"Matching {len(wanted)} markets to Kalshi...", flush=True)
    live = verify_kalshi([t for ts in wanted.values() for t in ts])
    ind_keys = [r["mapping"]["key"] for r in recommendations if r.get("mapping", {}).get("yes_party") == "IND"]
    for key, ticker in kalshi_independents(ind_keys, ind_names or {}).items():
        live.add(ticker)
        for r in recommendations:
            if r.get("mapping", {}).get("key") == key and r["mapping"]["yes_party"] == "IND" and r.get("exchange_id") in wanted:
                wanted[r["exchange_id"]] = [ticker]
    # Races Kalshi doesn't list get a Polymarket check instead (CO-04: model 0.98 R, Polymarket 0.66).
    no_kalshi = {r["exchange_id"]: polymarket_candidates(r["mapping"]["kind"], r["mapping"]["key"], r["mapping"]["yes_party"])
                 for r in recommendations
                 if r.get("exchange_id") in wanted and not any(t in live for t in wanted[r["exchange_id"]])}
    print(f"Matching {len(no_kalshi)} markets without Kalshi to Polymarket...", flush=True)
    poly_live = verify_polymarket([s for ss in no_kalshi.values() for s in ss])
    markets: dict[str,Any] = {}
    for r in recommendations:
        xid = r.get("exchange_id")
        if xid not in wanted: continue
        entry: dict[str,Any] = {"label": r["market"], "group": f"{r['mapping']['kind']}:{r['mapping']['key']}"}
        ticker = next((t for t in wanted[xid] if t in live), None)
        if ticker: entry["kalshi"] = ticker
        slug = next((s for s in no_kalshi.get(xid, []) if s in poly_live), None)
        if slug: entry["polymarket"] = slug   # YES outcome is listed first, which sigbot reads by default
        if r.get("estimated_probability") is not None and not r.get("model_caveat"):
            entry["model"] = round(r["estimated_probability"], 4)
            entry["model_range"] = [round(x, 4) for x in r["uncertainty_range"]]
        if r.get("model_caveat"): entry["model_caveat"] = r["model_caveat"]
        if not ({"kalshi", "polymarket", "model"} & set(entry)): continue
        entry["weights"] = FAIR_WEIGHTS
        market_backed = bool(ticker or slug)
        entry["take_edge"] = TAKE_EDGE_WITH_KALSHI if market_backed else TAKE_EDGE_MODEL_ONLY
        if not market_backed:
            entry["size_mult"] = 0.5   # no market to catch a model blind spot (e.g. VT-GOV): bet smaller
        markets[xid] = entry
    kal = sum("kalshi" in e for e in markets.values()); poly = sum("polymarket" in e for e in markets.values())
    mod = sum("model" in e for e in markets.values())
    only = sum(not ({"kalshi", "polymarket"} & set(e)) for e in markets.values())
    print(f"fair_values: {len(markets)} markets ({kal} with Kalshi, {poly} with Polymarket, {mod} with model, "
          f"{only} model-only)", flush=True)
    return {"generated_at": run_at, "source": "election_forecast.py", "weights": FAIR_WEIGHTS, "markets": markets}


def render_text(report: dict[str,Any]) -> str:
    lines=["2026 SUSQUEHANNA PREDICTIONS CUP — VALIDATED RECOMMENDATIONS (READ ONLY)",
           f"As of {report['generated_at']} | simulations={report['simulation']['count']:,} seed={report['simulation']['seed']}",
           "",f"{'SIDE':10} {'PRICE':>7} {'PROB':>7} {'RANGE':>15} {'EDGE':>7} {'QTY':>7} MARKET","-"*120]
    for r in report["recommendations"]:
        def pct(v): return "--" if v is None else f"{v:.1%}"
        rng=r.get("uncertainty_range") or [None,None]
        lines.append(f"{r['recommendation'][:10]:10} {pct(r.get('price')):>7} {pct(r.get('estimated_probability')):>7} "
                     f"{(pct(rng[0])+'..'+pct(rng[1])):>15} {pct(r.get('estimated_edge')):>7} {r.get('quantity',0):7} {r['market']}")
        lines.append(f"  {r.get('reason','')}")
    v=report["validation"]["aggregate"]
    lines += ["","VALIDATION (limited historical probability benchmark)",
              f"Calibrated reference: Brier={v['calibrated_reference']['brier']:.4f}, log loss={v['calibrated_reference']['log_loss']:.4f}, n={v['calibrated_reference']['n']}",
              f"Raw source: Brier={v['raw_source_probability']['brier']:.4f}, log loss={v['raw_source_probability']['log_loss']:.4f}",
              f"50% baseline: Brier={v['baseline_50']['brier']:.4f}, log loss={v['baseline_50']['log_loss']:.4f}",
              "Limitation: "+report["validation"]["limitation"],"",
              f"Existing positions: {report['reconciliation']['positions_seen']} | open orders: {report['reconciliation']['open_orders_seen']}",
              "READ ONLY — no orders were submitted or modified."]
    return "\n".join(lines)+"\n"


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--offline-data",action="store_true",help="use cached public data only")
    parser.add_argument("--validate-only",action="store_true",help="run data/model/simulation validation without the competition API")
    parser.add_argument("--fair-out",default=str(ROOT/"fair_values.json"),help="sigbot fair-value file to write ('' to skip)")
    args=parser.parse_args(argv)
    run_at=utcnow(); as_of=run_at.date(); cache=PublicCache(max_age_hours=10**9 if args.offline_data else 6)
    print("Loading cached/public election inputs...",flush=True)
    datasets={name:rows_from_csv(cache.fetch(name,url)) for name,url in PUBLIC_SOURCES.items()}
    gcb,gcb_se,gcb_date,gcb_count=generic_ballot(datasets["generic"],as_of)
    race_inputs=build_race_inputs(datasets["races"],datasets["polls"],datasets["history"],gcb,gcb_se,as_of)
    print(f"Running {SIMULATIONS:,} joint simulations with seed {SEED}...",flush=True)
    forecasts,chambers=simulate(race_inputs,SIMULATIONS,SEED)
    print(f"Simulation complete: {len(forecasts)} races.",flush=True)
    validation=validation_report(datasets["validation"])
    if args.validate_only:
        result={"generated_at":run_at.isoformat(),"simulations":SIMULATIONS,"seed":SEED,"race_count":len(forecasts),
                "generic_ballot_margin":gcb,"generic_polls_used":gcb_count,"chambers":chambers,
                "validation":validation,"sources":cache.provenance}
        (ROOT/"validation-report.json").write_text(json.dumps(result,indent=2,allow_nan=False),encoding="utf-8")
        print(json.dumps({"simulations":SIMULATIONS,"seed":SEED,"race_count":len(forecasts),
                          "generic_polls_used":gcb_count,"validation":validation["aggregate"]},indent=2))
        return 0
    print("Reading competition market list...",flush=True)
    api=ReadOnlyAPI(KEY); markets,tid=fetch_markets(api)
    print(f"Competition markets loaded: {len(markets)}",flush=True)
    recommendations,reconciliation=make_recommendations(api,markets,tid,forecasts,chambers,run_at.isoformat())
    report={"generated_at":run_at.isoformat(),"read_only":True,"simulation":{"count":SIMULATIONS,"seed":SEED,
            "sampling_error_note":"Per-event Monte Carlo SE is reported separately; uncertainty ranges describe model/input uncertainty and do not shrink with more simulations."},
            "national_environment":{"generic_ballot_margin_dem_minus_rep":gcb,"sampling_se":gcb_se,"polls_used":gcb_count,"latest_poll":gcb_date},
            "model":{"description":"Poll/fundamental blend with incumbency and time-to-election; joint Gaussian national, regional, and race-specific error factors.",
            "uncertainty":{"national_systematic_sigma_points":2.5,"regional_sigma_points":"1.8–2.2","race_sigma_points":"data-dependent 2.8+"}},
            "validation":validation,"sources":cache.provenance,"upstream_references":UPSTREAM_REFERENCES,
            "reconciliation":reconciliation,"limits":{"max_order":MAX_ORDER,"max_related_market":MAX_MARKET,"max_total":MAX_TOTAL},
            "recommendations":recommendations}
    (ROOT/"forecast-report.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
    text=render_text(report); (ROOT/"forecast-report.txt").write_text(text,encoding="utf-8"); print(text)
    if args.fair_out:
        out=Path(args.fair_out); tmp=out.with_suffix(".tmp")
        ind_names={r.get("race_code",""):r.get("ind_name","") for r in datasets["races"] if r.get("ind_name")}
        tmp.write_text(json.dumps(fair_values(recommendations,run_at.isoformat(),ind_names),indent=2),encoding="utf-8"); tmp.replace(out)
        print(f"Wrote {out} for sigbot.",flush=True)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
