#!/usr/bin/env python3
"""Guarded executor: dry-run first; live needs the reviewed plan token."""
import argparse,hashlib,json,math,os,time,urllib.error,urllib.parse,urllib.request,uuid
from datetime import datetime,timezone
from pathlib import Path
R=Path(__file__).resolve().parent; BASE=os.getenv('SUPER_MARKET_BASE_URL','https://sig.thesuper.market/api/v1').rstrip('/'); KEY=os.getenv('SUPER_MARKET_API_KEY','')
MAX_ORDER=float(os.getenv('MAX_ORDER_SIZE','1000')); MAX_MARKET=float(os.getenv('MAX_EXPOSURE_PER_MARKET','2000')); MAX_TOTAL=float(os.getenv('MAX_TOTAL_EXPOSURE','10000')); MAX_AGE=int(os.getenv('MAX_PLAN_AGE_SECONDS','14400'))
FALLBACK_MIN_EDGE=float(os.getenv('FALLBACK_MIN_EDGE','0.01')); FALLBACK_MAX_COST=float(os.getenv('FALLBACK_MAX_ORDER_SUSQIES','100'))
STOP=R/'STOP'; AUDIT=R/'audit.jsonl'; STATE=R/'order-state.json'
ONE_TIME=R/'noon-authorization.json'; OPEN_UTC=datetime(2026,10,1,16,0,0,tzinfo=timezone.utc)
def log(event,payload):
    safe={k:v for k,v in payload.items() if k.lower() not in {'authorization','x-api-key','api_key','key'}}
    with AUDIT.open('a',encoding='utf-8') as f:f.write(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'event':event,'payload':safe},separators=(',',':'))+'\n')
def api(method,path,body=None):
    if not KEY:raise SystemExit('Missing SUPER_MARKET_API_KEY.')
    if method!='GET' and STOP.exists():raise SystemExit('STOP file present; no account changes sent.')
    h={'Authorization':f'Bearer {KEY}','Accept':'application/json','User-Agent':'sig-cup-guarded-trader/1.0'}; data=None
    if body is not None:h['Content-Type']='application/json';data=json.dumps(body).encode()
    log('request',{'method':method,'path':path,'body':body})
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE+path,headers=h,data=data,method=method),timeout=25) as r:
            x=json.load(r);log('response',{'method':method,'path':path,'status':r.status,'body':x});return x
    except urllib.error.HTTPError as e:
        raw=e.read().decode('utf-8','replace');log('error',{'method':method,'path':path,'status':e.code,'body':raw[:2000]})
        if e.code in (401,403):raise SystemExit(f'Authentication/authorization failure ({e.code}); trading stopped.')
        raise SystemExit(f'Unexpected API response {e.code}; trading stopped. See audit.jsonl.')
def rows(x):
    if isinstance(x,list):return x
    for k in ('data','orders','positions'):
        if isinstance(x.get(k),list):return x[k]
    return []
def fresh_report():
    p=R/'report.json'
    if not p.exists():raise SystemExit('Run .\\run.ps1 first.')
    x=json.loads(p.read_text(encoding='utf-8'));age=time.time()-datetime.fromisoformat(x['generated_at']).timestamp()
    if age>MAX_AGE:raise SystemExit('Recommendation report is stale; refresh it.')
    return x
def underlying(title):
    s=title.lower().strip().rstrip('?')
    s=s.replace('will the democratic party win the ','').replace('will the republican party win the ','')
    return s
def dry_run():
    if STOP.exists():raise SystemExit('STOP file present. Remove it only to resume planning.')
    account=api('GET','/account'); tour=api('GET','/tournaments/midterm-elections'); tid=tour.get('data',tour).get('id')
    pos=rows(api('GET','/tournaments/midterm-elections/portfolio/positions')); oo=rows(api('GET','/orders?'+urllib.parse.urlencode({'limit':200,'tournamentId':tid})))
    report=fresh_report(); titles={str(r['market_id']):r['market'] for r in report['recommendations']}
    exposure={}; total=0
    for p in pos:
        cost=abs(float(p.get('cost',p.get('exposure',p.get('value',0))) or 0)); mid=str(p.get('marketId',p.get('market_id'))); group=underlying(p.get('marketTitle') or titles.get(mid,mid));exposure[group]=exposure.get(group,0)+cost;total+=cost
    existing={(str(o.get('exchangeId',o.get('exchange_id'))),str(o.get('side','')).lower()) for o in oo if o.get('status','open') in ('open','resting','partial')}; proposed=[]
    for r in report['recommendations']:
        if r['side']=='NO TRADE' or r['confidence']=='low' or not r.get('resolution_criteria'):continue
        price=float(r['available_price']);xid=str(r['exchange_id']);side=r['side'].lower();group=underlying(r['market']);liq=r.get('yes_ask_quantity' if side=='yes' else 'no_ask_quantity')
        if (xid,side) in existing:continue
        if liq is None or liq <= 0:continue
        qty=max(0,int(min(math.floor(MAX_ORDER/price),math.floor(max(0,MAX_MARKET-exposure.get(group,0))/price),math.floor(max(0,MAX_TOTAL-total)/price),math.floor(liq))))
        if not qty:continue
        cost=qty*price;total+=cost;exposure[group]=exposure.get(group,0)+cost
        proposed.append({'market':r['market'],'marketId':r['market_id'],'exchangeId':r['exchange_id'],'side':side,'action':'buy','quantity':qty,'price':price,'tournamentId':tid,'estimatedEdge':r['edge'],'resolutionCriteria':r['resolution_criteria']})
    if not proposed:
        candidates=[]
        for r in report['recommendations']:
            side=(r.get('candidate_side') or '').lower();edge=r.get('edge');price=r.get('available_price');group=underlying(r['market'])
            if side not in ('yes','no') or edge is None or edge<FALLBACK_MIN_EDGE or r.get('confidence')=='low' or not r.get('resolution_criteria') or price is None:continue
            liq=r.get('yes_ask_quantity' if side=='yes' else 'no_ask_quantity')
            if liq is None or liq<=0:continue
            candidates.append((edge,r,side,float(price),group,float(liq)))
        if candidates:
            edge,r,side,price,group,liq=max(candidates,key=lambda x:x[0]);xid=str(r['exchange_id'])
            if (xid,side) not in existing:
                qty=max(0,int(min(math.floor(FALLBACK_MAX_COST/price),math.floor(max(0,MAX_MARKET-exposure.get(group,0))/price),math.floor(max(0,MAX_TOTAL-total)/price),math.floor(liq))))
                if qty:
                    cost=qty*price;total+=cost;exposure[group]=exposure.get(group,0)+cost
                    proposed.append({'market':r['market'],'marketId':r['market_id'],'exchangeId':r['exchange_id'],'side':side,'action':'buy','quantity':qty,'price':price,'tournamentId':tid,'estimatedEdge':edge,'resolutionCriteria':r['resolution_criteria'],'fallback':True})
    plan={'createdAt':datetime.now(timezone.utc).isoformat(),'accountVerified':bool(account),'limits':{'maxOrderSUSQies':MAX_ORDER,'maxUnderlyingExposureSUSQies':MAX_MARKET,'maxTotalExposureSUSQies':MAX_TOTAL},'fallback':{'minimumEdge':FALLBACK_MIN_EDGE,'maximumOrderSUSQies':FALLBACK_MAX_COST,'used':any(o.get('fallback') for o in proposed)},'plannedNewExposure':round(sum(o['quantity']*o['price'] for o in proposed),6),'maximumTotalExposureAfterPlan':round(total,6),'orders':proposed}
    token=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16];plan['approvalToken']=token
    (R/'dry-run-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8');log('dry_run',{'orderCount':len(proposed),'approvalToken':token,'limits':plan['limits']});print(json.dumps(plan,indent=2));print(f'\nDRY RUN ONLY — no orders submitted. Approval token: {token}');return plan
def live(token,authorized_once=False):
    p=R/'dry-run-plan.json'
    if not p.exists():raise SystemExit('No dry-run plan exists.')
    plan=json.loads(p.read_text(encoding='utf-8'))
    if time.time()-datetime.fromisoformat(plan['createdAt']).timestamp()>MAX_AGE:raise SystemExit('Dry-run plan expired.')
    if token!=plan.get('approvalToken'):raise SystemExit('Approval token does not match reviewed plan.')
    if not authorized_once and os.getenv('LIVE_TRADING')!='I_APPROVE_THIS_DRY_RUN':raise SystemExit('LIVE_TRADING approval variable is absent.')
    import recommender
    recommender.main()
    fresh=json.loads((R/'report.json').read_text(encoding='utf-8')); current={(str(x['exchange_id']),(x.get('candidate_side') or x['side']).lower()):x for x in fresh['recommendations'] if (x.get('candidate_side') or x['side'])!='NO TRADE'}
    state=json.loads(STATE.read_text()) if STATE.exists() else {'submitted':{}}; final=[]
    for o in plan['orders']:
        if STOP.exists():raise SystemExit('STOP file detected; halted.')
        now=current.get((str(o['exchangeId']),o['side']))
        required_edge=FALLBACK_MIN_EDGE if o.get('fallback') else fresh['minimum_edge']
        if not now or now.get('edge') is None or now['edge'] < required_edge:log('skip',{'market':o['market'],'reason':'edge_removed'});final.append({'market':o['market'],'status':'skipped','reason':'edge_removed'});continue
        if now.get('resolution_criteria') != o['resolutionCriteria']:log('skip',{'market':o['market'],'reason':'resolution_changed'});final.append({'market':o['market'],'status':'skipped','reason':'resolution_changed'});continue
        if float(now['available_price']) > float(o['price']):log('skip',{'market':o['market'],'reason':'price_worsened'});final.append({'market':o['market'],'status':'skipped','reason':'price_worsened'});continue
        qfield='yes_ask_quantity' if o['side']=='yes' else 'no_ask_quantity'
        if now.get(qfield) is None or float(now[qfield]) < int(o['quantity']):log('skip',{'market':o['market'],'reason':'liquidity_inadequate'});final.append({'market':o['market'],'status':'skipped','reason':'liquidity_inadequate'});continue
        d=f"{o['exchangeId']}:{o['side']}:{o['price']}:{o['quantity']}"
        if d in state['submitted']:final.append({'market':o['market'],'status':'skipped','reason':'duplicate'});continue
        body={k:o[k] for k in ('exchangeId','side','action','quantity','price','tournamentId')};body['idempotencyKey']=str(uuid.uuid4());resp=api('POST','/orders',body)
        if not any(k in resp for k in ('orderId','id','data')):raise SystemExit('Unexpected order response; halted.')
        payload=resp.get('data',resp) if isinstance(resp,dict) else {};oid=payload.get('orderId',payload.get('id'))
        if oid is None:raise SystemExit('API did not return an order identifier; halted.')
        confirmed=api('GET',f'/orders/{oid}');fills=api('GET',f'/orders/{oid}/fills')
        item={'market':o['market'],'status':'api_confirmed','orderId':oid,'order':confirmed,'fills':fills};final.append(item)
        state['submitted'][d]={'at':datetime.now(timezone.utc).isoformat(),'response':resp,'confirmation':confirmed,'fills':fills};STATE.write_text(json.dumps(state,indent=2),encoding='utf-8')
    result={'generatedAt':datetime.now(timezone.utc).isoformat(),'orders':final};(R/'execution-report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2));print('Approved plan processed. Only entries marked api_confirmed were accepted by the API.')
def authorized_once(run_id):
    if datetime.now(timezone.utc)<OPEN_UTC:raise SystemExit('Opening time has not arrived; no orders submitted.')
    if not ONE_TIME.exists():raise SystemExit('One-time authorization is absent.')
    auth=json.loads(ONE_TIME.read_text(encoding='utf-8-sig'))
    if auth.get('runId')!=run_id or auth.get('consumedAt'):raise SystemExit('One-time authorization is invalid or already consumed.')
    expected={'maxOrderSUSQies':1000.0,'maxUnderlyingExposureSUSQies':2000.0,'maxTotalExposureSUSQies':10000.0}
    if auth.get('limits')!=expected:raise SystemExit('One-time authorization limits do not match the bounded run.')
    if (MAX_ORDER,MAX_MARKET,MAX_TOTAL)!=(1000.0,2000.0,10000.0):raise SystemExit('Runtime risk limits do not match the authorized limits.')
    import recommender
    recommender.main();plan=dry_run()
    auth['consumedAt']=datetime.now(timezone.utc).isoformat();tmp=ONE_TIME.with_suffix('.tmp');tmp.write_text(json.dumps(auth,indent=2),encoding='utf-8');tmp.replace(ONE_TIME)
    log('one_time_authorization_consumed',{'runId':run_id,'orderCount':len(plan['orders']),'limits':expected})
    live(plan['approvalToken'],authorized_once=True)
def main():
    a=argparse.ArgumentParser();a.add_argument('mode',choices=['dry-run','live','authorized-once']);a.add_argument('--approval',default='');a.add_argument('--run-id',default='');x=a.parse_args()
    dry_run() if x.mode=='dry-run' else authorized_once(x.run_id) if x.mode=='authorized-once' else live(x.approval)
if __name__=='__main__':main()
