import os, json, re, math
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
import streamlit as st
import matplotlib.pyplot as plt

st.set_page_config(page_title='Book Finder V7.7', page_icon='📚', layout='wide')
st.title('📚 Private Used-Book Finder — V7.7')
with st.expander('🧪 Recovery / retest known ASINs', expanded=False):
    st.caption('Loads specific ASINs without running Product Finder discovery. Keepa may cache recently requested product data, but detail-token use is controlled by Keepa.')
    recovery_text=st.text_input('ASINs, comma-separated', value='')
    if st.button('Load these ASINs'):
        raw_vals=[x.strip().upper() for x in recovery_text.replace('\n',',').split(',') if x.strip()]
        raw_vals=list(dict.fromkeys(raw_vals))[:20]
        vals=[a for a in raw_vals if re.fullmatch(r'[A-Z0-9]{10}',a)]
        invalid=[a for a in raw_vals if a not in vals]
        if invalid:
            st.warning('Skipped invalid ASIN format: ' + ', '.join(invalid) + '. ASINs must be exactly 10 letters/numbers.')
        if vals:
            st.session_state['asins_v5']=vals
            st.session_state['deal_meta_v5']={a:{'source':'Recovery/Test'} for a in vals}
            st.success(f'Loaded {len(vals)} valid ASIN(s) without a discovery call. Scroll to the analysis button below.')
        else:
            st.warning('Enter at least one valid 10-character ASIN.')

st.caption('A2A sourcing: book-only discovery + sustained-rank demand + demand-supported value + supply momentum + Amazon/FBA economics + seasonality + review history.')

KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)

# Keepa Product.csv / stats.current indexes used here.
AMAZON, NEW, USED, SALES, COUNT_USED = 0, 1, 2, 3, 12
# Keepa price-type indexes: shipping-inclusive Used conditions + Used Buy Box.
USED_LN_SHIP, USED_VG_SHIP, USED_GOOD_SHIP, USED_ACC_SHIP, USED_BB_SHIP = 19, 20, 21, 22, 32

def secret(name):
    try: return st.secrets.get(name, os.getenv(name, ''))
    except Exception: return os.getenv(name, '')

key = secret('KEEPA_API_KEY')
supa_url = secret('SUPABASE_URL').rstrip('/')
supa_key = secret('SUPABASE_KEY')

if not key:
    st.warning('Add KEEPA_API_KEY to Streamlit Secrets. Never put your API key in GitHub.')

# ---------- Optional persistent ASIN history (Supabase REST) ----------
def db_headers():
    return {'apikey': supa_key, 'Authorization': f'Bearer {supa_key}', 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}

def db_enabled(): return bool(supa_url and supa_key)

@st.cache_data(ttl=30, show_spinner=False)
def load_statuses_remote(url, api_key):
    r=requests.get(f'{url}/rest/v1/book_status?select=*', headers={'apikey':api_key,'Authorization':f'Bearer {api_key}'}, timeout=20)
    r.raise_for_status(); return r.json()

def load_statuses():
    if db_enabled():
        try: return {x['asin']:x for x in load_statuses_remote(supa_url,supa_key)}
        except Exception as e:
            st.sidebar.warning(f'History database unavailable: {e}')
    return st.session_state.setdefault('local_statuses_v5', {})

def save_status(asin, status, title='', reason=''):
    row={'asin':asin,'status':status,'title':title[:500],'reason':reason[:500],'updated_at':datetime.now(timezone.utc).isoformat()}
    if db_enabled():
        r=requests.post(f'{supa_url}/rest/v1/book_status?on_conflict=asin', headers={**db_headers(),'Prefer':'resolution=merge-duplicates,return=minimal'}, data=json.dumps(row), timeout=20)
        r.raise_for_status(); load_statuses_remote.clear()
    else:
        st.session_state.setdefault('local_statuses_v5', {})[asin]=row

def persist_analyzed(rows):
    # Automatic marker is deliberately distinct from the user's Buy/Watch/Pass/Reviewed choices.
    # A later manual choice simply replaces status='Analyzed' for that ASIN.
    for row in rows:
        asin=row.get('ASIN')
        if not asin: continue
        existing=load_statuses().get(asin,{})
        if existing.get('status') in ('Buy','Watch','Pass','Reviewed'):
            continue
        save_status(asin,'Analyzed',row.get('Title',''),'Automatically analyzed by scanner; no manual decision yet.')

def persist_raw_products(products):
    current=load_statuses()
    for p in products or []:
        asin=p.get('asin')
        if not asin: continue
        if current.get(asin,{}).get('status') in ('Buy','Watch','Pass','Reviewed'):
            continue
        save_status(asin,'Analyzed',p.get('title',''),'Keepa product data retrieved; automatic analyzed marker.')

statuses=load_statuses()

# V7.4.3 migration: these books were detail-analyzed before automatic persistent markers existed.
LEGACY_ANALYZED_ASINS = {'1538782227','B0G5ZDLMRN','059395601X','B0F74CH248','0063462540','0593798635','0310169518','0316404098','0063376695','B0091A0C4O'}
# Known non-book leak observed during testing. Exclude before paid detail analysis.
KNOWN_NONBOOK_ASINS = {'B0091A0C4O'}

def migrate_legacy_analyzed():
    existing=load_statuses(); saved=0
    for asin in sorted(LEGACY_ANALYZED_ASINS):
        if asin in existing: continue
        try:
            save_status(asin,'Analyzed','','Legacy analyzed ASIN migrated by V7.4.3.')
            saved+=1
        except Exception:
            pass
    if saved:
        try: load_statuses_remote.clear()
        except Exception: pass
    return saved

legacy_migrated=migrate_legacy_analyzed()
statuses=load_statuses()

def session_seen_asins():
    return set(st.session_state.get('analyzed_asins_v64', []))

def remember_analyzed(asins):
    seen=session_seen_asins()
    seen.update(str(a) for a in asins if a)
    st.session_state['analyzed_asins_v64']=sorted(seen)


with st.sidebar:
    st.header('Buying rules')
    max_cost = st.number_input('Maximum shipped buy cost ($)', 1., 500., 20.)
    min_intrinsic = st.number_input('Minimum intrinsic Used value ($)', 1., 500., 20.)
    min_profit = st.number_input('Minimum estimated profit ($)', 0., 100., 5.)
    min_roi = st.number_input('Minimum ROI (%)', 0, 1000, 50)
    inbound_per_lb = st.number_input('Inbound placement + ship to FBA ($/lb)', 0.0, 5.0, 0.60, 0.05)
    prep_cost = st.number_input('Prep cost per book ($)', 0.0, 20.0, 0.00, 0.10)
    st.caption('Amazon fees are calculated per book: 15% referral + $1.80 media closing + 2026 FBA fulfillment. Inbound placement + shipping to FBA is modeled separately from weight at the $/lb rate above.')
    max_rank = st.number_input('Maximum current sales rank', 1000, 10000000, 2000000, step=100000)
    st.divider(); st.header('Discovery quality')
    prefilter_value = st.number_input('Minimum 90-day Used average before detail analysis ($)', 1.0, 500.0, max(25.0, float(min_intrinsic)), 1.0)
    min_value_spread = st.number_input('Target historical value spread over buy cost ($)', 0.0, 100.0, 12.0, 1.0)
    st.caption('V6.5 prioritizes higher-value candidates before spending product-history tokens. Analyzed ASINs are also persisted so app restarts do not make the scanner spend tokens on them again. The detailed demand-supported model still makes the final BUY/ANALYZE/PASS decision.')
    if st.button('Reset temporary session memory'):
        st.session_state['analyzed_asins_v64']=[]
        st.success('Temporary session memory cleared. Persistent analyzed history was not deleted.')
    st.divider(); st.header('Mining mode')
    mining_mode = st.radio('Discovery engine', ['🚀 Opportunity Mine','🔎 Value Mine','⚡ Recent Used Drops','🎓 Seasonal Mine'], index=0)
    deal_window = st.selectbox('Recent-drop window', [('24 hours',0),('7 days',1),('31 days',2),('90 days',3)], index=1, format_func=lambda x:x[0])
    min_drop_pct = st.slider('Minimum recent price drop (%)', 10, 80, 30, 5)
    st.caption('Recent Drops uses Keepa Deals first, then spends detail-history calls only on the candidates you choose to analyze.')
    st.divider(); st.header('Value model')
    floor_days = st.selectbox('Intrinsic-value lookback', [90,180,365,730], index=2)
    low_band = st.slider('Low-price band used for intrinsic value',10,50,25,5)
    chart_days = st.selectbox('Mini graph period',[90,365,730],index=1,format_func=lambda x:'90 days' if x==90 else ('1 year' if x==365 else '2 years'))
    st.divider(); st.header('History')
    view_filter=st.selectbox('Show books marked as',['New only','Watch','Buy','Reviewed','Pass','All'],index=0)
    if db_enabled():
        persistent_count=len(load_statuses())
        st.success(f'Persistent analyzed-ASIN history: ON — {persistent_count} ASIN(s) stored. Analyzed books stay skipped across restarts/deploys.')
    else: st.warning('Analyzed-ASIN skip is session-only. Without Supabase, a Streamlit restart/deploy can forget analyzed books.')

@st.cache_data(ttl=3600, show_spinner=False)
def discover(api_key,max_rank,max_cost,min_hist_value,min_spread=12.0):
    # Cheap current Used price + materially higher 90-day Used average.
    # Keepa Product Finder cannot know our later demand-supported value, so this
    # is only a token-saving prefilter; detailed history remains authoritative.
    hist_floor=max(float(min_hist_value), float(min_spread))
    selection={'rootCategory':[283155],'productType':0,
               'current_SALES_lte':int(max_rank),
               'current_USED_lte':int(max_cost*100),
               'avg90_USED_gte':int(hist_floor*100),
               'perPage':50,'page':0,
               'sort':[['avg90_USED','desc']]}
    r=requests.get('https://api.keepa.com/query',params={'key':api_key,'domain':1,'selection':json.dumps(selection,separators=(',',':'))},timeout=45)
    r.raise_for_status(); data=r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

@st.cache_data(ttl=3600, show_spinner=False)
def discover_ranked(api_key,max_rank,max_cost,min_hist_value,min_spread=12.0):
    """V7.7 Immediate-Profit Opportunity Mine.

    Product Finder cannot calculate our exact FBA max-buy formula, but it can cheaply
    search current Used/New/rank fields.  We therefore build several margin bands so
    books with meaningful current New-vs-Used room rise ahead of books whose Used price
    is already sitting near New.  Detailed history remains authoritative.
    """
    hist_floor=max(float(min_hist_value), float(min_spread))
    common={'rootCategory':[283155],'productType':0,
            'current_SALES_lte':int(max_rank),
            'avg90_USED_gte':int(hist_floor*100),
            'perPage':50,'page':0}

    def run(selection, sort_field='current_SALES', direction='asc'):
        q=dict(common); q.update(selection); q['sort']=[[sort_field,direction]]
        r=requests.get('https://api.keepa.com/query',params={'key':api_key,'domain':1,'selection':json.dumps(q,separators=(',',':'))},timeout=45)
        r.raise_for_status(); data=r.json()
        if data.get('error'): raise ValueError(str(data['error']))
        return data

    # V7.7: approximate the later Max-Buy gate before spending detail tokens.
    # Product Finder cannot calculate exact FBA fees/weight, so use deliberately
    # conservative acquisition bands. Each band requires both a large 90-day Used
    # recovery value and New-price headroom. Detailed analysis remains authoritative.
    bands=[
        ('Immediate A2A — Best', min(float(max_cost),6.0), 24.0, 24.0, 5.00),
        ('Immediate A2A — Strong', min(float(max_cost),8.0), 28.0, 28.0, 4.20),
        ('Immediate A2A — Strong', min(float(max_cost),10.0), 34.0, 34.0, 3.40),
        ('Immediate A2A — Possible', min(float(max_cost),12.0), 40.0, 40.0, 2.60),
        ('Immediate A2A — Possible', min(float(max_cost),15.0), 48.0, 48.0, 1.80),
    ]
    responses=[]; passes=[]
    for lane,used_cap,hist_floor_band,new_floor,weight in bands:
        if used_cap < 1: continue
        data=run({'current_USED_lte':int(used_cap*100),'avg90_USED_gte':int(hist_floor_band*100),'current_NEW_gte':int(new_floor*100)})
        arr=[a for a in data.get('asinList',[]) if isinstance(a,str)]
        responses.append(data); passes.append((lane,arr,weight))

    # A demand fallback keeps excellent cheap books from disappearing merely because
    # Keepa has no current New price.  It is intentionally much lower weight.
    demand=run({'current_USED_lte':int(float(max_cost)*100)},'current_SALES','asc')
    cheap=run({'current_USED_lte':int(min(float(max_cost),10.0)*100)},'current_USED','asc')
    responses.extend([demand,cheap])
    passes.extend([
        ('Demand Fallback',[a for a in demand.get('asinList',[]) if isinstance(a,str)],0.65),
        ('Cheap Entry',[a for a in cheap.get('asinList',[]) if isinstance(a,str)],0.80),
    ])

    scores={}; lanes={}
    for lane,arr,weight in passes:
        for i,asin in enumerate(arr):
            scores[asin]=scores.get(asin,0.0)+weight/(10.0+i)
            lanes.setdefault(asin,set()).add(lane)

    # Multiple profit bands + demand confirmation are much more useful than a single
    # historical-price spike.  Pure fallback candidates are deliberately demoted.
    for asin in list(scores):
        ls=lanes.get(asin,set())
        profit_lanes={x for x in ls if x.startswith('Immediate A2A')}
        if profit_lanes and 'Demand Fallback' in ls: scores[asin]+=0.040
        if len(profit_lanes)>=2: scores[asin]+=0.025
        if not profit_lanes: scores[asin]*=0.08

    ranked=sorted(scores,key=lambda a:scores[a],reverse=True)
    meta={}
    for asin in ranked:
        ls=lanes.get(asin,set())
        if 'Immediate A2A — Best' in ls: lane='Immediate A2A — Best'
        elif 'Immediate A2A — Strong' in ls: lane='Immediate A2A — Strong'
        elif 'Immediate A2A — Possible' in ls: lane='Immediate A2A — Possible'
        elif 'Cheap Entry' in ls: lane='Cheap Entry Fallback'
        else: lane='Demand Fallback'
        meta[asin]={'source':'Opportunity Mine','lane':lane,'discovery_score':round(scores[asin]*100,2)}
    tokens=[x.get('tokensLeft') for x in responses if isinstance(x.get('tokensLeft'),(int,float))]
    return {'asinList':ranked,'meta':meta,'tokensLeft':min(tokens) if tokens else 'unknown'}

@st.cache_data(ttl=1800, show_spinner=False)
def discover_deals(api_key, max_rank, max_cost, drop_pct, date_range):
    # Keepa Deals: USED marketplace price drops. 5 tokens/request, up to 150 deals.
    selection={
        'page':0,'domainId':1,'priceTypes':[2],'dateRange':int(date_range),
        'isRangeEnabled':True,'isFilterEnabled':True,
        'includeCategories':[283155],
        'salesRankRange':[0,int(max_rank)],
        'currentRange':[0,int(max_cost*100)],
        'deltaPercentRange':[int(drop_pct),100],
        'filterErotic':True,'singleVariation':True,'sortType':4
    }
    r=requests.post('https://api.keepa.com/deal',params={'key':api_key},json=selection,timeout=60)
    r.raise_for_status(); data=r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

@st.cache_data(ttl=86400,show_spinner=False)
def details(api_key,asins):
    r=requests.get('https://api.keepa.com/product',params={'key':api_key,'domain':1,'asin':','.join(asins),'stats':365,'history':1},timeout=90)
    r.raise_for_status(); data=r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

def cents(v): return round(v/100,2) if isinstance(v,(int,float)) and v>=0 else None
def at_raw(arr,idx): return arr[idx] if isinstance(arr,list) and len(arr)>idx and isinstance(arr[idx],(int,float)) and arr[idx]>=0 else None
def at_price(arr,idx): return cents(at_raw(arr,idx))

def keepa_series(csv_arr,divide=100.0):
    if not isinstance(csv_arr,list) or len(csv_arr)<2:return pd.Series(dtype=float)
    pts=[]
    for i in range(0,len(csv_arr)-1,2):
        t,v=csv_arr[i],csv_arr[i+1]
        if not isinstance(t,(int,float)) or not isinstance(v,(int,float)) or v<0:continue
        dt=KEEPA_EPOCH+timedelta(minutes=int(t)); pts.append((pd.Timestamp(dt).tz_convert(None).normalize(),v/divide))
    if not pts:return pd.Series(dtype=float)
    s=pd.Series({d:v for d,v in pts}).sort_index(); end=pd.Timestamp.utcnow().tz_localize(None).normalize()
    return s.reindex(pd.date_range(s.index.min(),end,freq='D')).ffill().dropna()

def intrinsic_floor(s,days=365,band_pct=25):
    if s.empty:return None
    x=s[s.index>=s.index.max()-pd.Timedelta(days=days)].dropna()
    if len(x)<7:return None
    trim=x[x>=x.quantile(.05)]; cutoff=trim.quantile(band_pct/100); low=trim[trim<=cutoff]
    return round(float(low.mean()),2) if len(low) else None

def rank_drop_dates(rank_s, days=730):
    """Dates where sales rank improves sharply. This is a demand proxy, not confirmed unit sales."""
    if rank_s.empty:
        return pd.DatetimeIndex([])
    x=rank_s[rank_s.index>=rank_s.index.max()-pd.Timedelta(days=days)].dropna()
    if len(x)<2:
        return pd.DatetimeIndex([])
    prev=x.shift(1)
    drops=(prev-x)>np.maximum(prev*0.08,5000)
    return pd.DatetimeIndex(x.index[drops.fillna(False)])

def demand_supported_value(price_s, rank_s, days=730):
    """Conservative Used value supported by price levels near rank-drop events.

    Rank drops are only a demand proxy. We sample the Used price on/around those
    dates and use a conservative lower quartile. This prevents long periods of
    high asking prices with little rank activity from inflating value.
    """
    if price_s.empty or rank_s.empty:
        return None
    end=min(price_s.index.max(), rank_s.index.max())
    start=end-pd.Timedelta(days=days)
    p=price_s[(price_s.index>=start)&(price_s.index<=end)].dropna()
    r=rank_s[(rank_s.index>=start)&(rank_s.index<=end)].dropna()
    if len(p)<30 or len(r)<30:
        return None
    dates=rank_drop_dates(r,days)
    samples=[]
    for d in dates:
        # Use a short window because Keepa series are step-filled daily.
        w=p[(p.index>=d-pd.Timedelta(days=2))&(p.index<=d+pd.Timedelta(days=2))]
        if not w.empty:
            samples.append(float(w.median()))
    if len(samples)<3:
        joined=pd.concat([p.rename('price'),r.rename('rank')],axis=1).dropna()
        recent=joined[joined.index>=joined.index.max()-pd.Timedelta(days=90)] if not joined.empty else joined
        low_rank=recent[recent['rank']<=100000] if not recent.empty else recent
        if len(low_rank)>=14:
            weekly=low_rank['price'].resample('7D').median().dropna()
            samples=[float(v) for v in weekly.values if np.isfinite(v) and v>0]
            evidence_type='continuous-low-rank'
        else:
            evidence_type='rank-drop'
    else:
        evidence_type='rank-drop'
    if len(samples)<3:
        return None
    a=np.asarray(samples,dtype=float)
    a=a[np.isfinite(a)&(a>0)]
    if len(a)<3:
        return None
    # Remove extreme tails, then use the 25th percentile as a conservative
    # price-at-demand estimate. Never let a few high-price events dominate.
    if len(a)>=8:
        lo,hi=np.quantile(a,[.05,.95]); a=a[(a>=lo)&(a<=hi)]
    if len(a)<3:
        return None
    return {'value':round(float(np.quantile(a,.25)),2),
            'median':round(float(np.median(a)),2),
            'events':int(len(a)),
            'low':round(float(np.quantile(a,.10)),2),
            'high':round(float(np.quantile(a,.75)),2),
            'evidence_type':evidence_type}

def supported_value_reason(ds, floor, med):
    if not ds:
        return 'Insufficient rank-drop history to establish demand-supported value.'
    v=ds['value']; events=ds['events']
    if med and med>0 and v<med*.55:
        return f'Historical asking prices look inflated versus demand: supported value ${v:.2f} from {events} rank-drop events.'
    if floor and floor>0 and v<floor*.70:
        return f'Demand occurred at materially lower prices than the old intrinsic model: ${v:.2f} from {events} events.'
    if ds.get('evidence_type')=='continuous-low-rank':
        return f'${v:.2f} is supported by {events} weekly price samples during sustained low-rank demand.'
    return f'${v:.2f} is supported by {events} historical rank-drop events (demand proxy).'

def seasonal_model(s):
    if s.empty or len(s)<365*2:return None
    df=s.rename('price').to_frame(); df['year']=df.index.year; df['month']=df.index.month
    hist=df[df.year<pd.Timestamp.utcnow().year]; monthly=hist.groupby(['year','month']).price.median().reset_index()
    valid=monthly.groupby('month').year.nunique(); valid=valid[valid>=2].index
    if not len(valid):return None
    stats=monthly[monthly.month.isin(valid)].groupby('month').price.agg(['median','count']); baseline=float(hist.price.median())
    if baseline<=0:return None
    stats['premium']=stats['median']/baseline; m=int(stats.premium.idxmax()); premium=float(stats.loc[m,'premium'])
    if premium<1.20:return None
    vals=monthly[monthly.month==m].price; value=float(min(vals.median(),vals.quantile(.25)))
    return {'month':m,'value':round(value,2),'premium':round((premium-1)*100,1),'years':int(len(vals)),'confidence':'High' if len(vals)>=3 else 'Medium'}

def rank_drop_count(rank_s,days=90):
    if rank_s.empty:return 0
    x=rank_s[rank_s.index>=rank_s.index.max()-pd.Timedelta(days=days)].dropna(); prev=x.shift(1)
    return int(((prev-x)>np.maximum(prev*0.08,5000)).sum())

def demand_quality(rank_s, book_type, drops90):
    if rank_s.empty:return 'WEAK',None
    x=rank_s[rank_s.index>=rank_s.index.max()-pd.Timedelta(days=90)].dropna(); prev=x.shift(1)
    events=x[(prev-x)>np.maximum(prev*0.08,5000)]
    days_since=max(0,int((rank_s.index.max()-events.index.max()).days)) if not events.empty else None
    seasonal=any(k in str(book_type) for k in ('College','High School','Holiday','Seasonal','Test Prep'))
    current_rank=float(x.iloc[-1]) if not x.empty else None
    median_rank=float(x.median()) if not x.empty else None
    days_under_100k=int((x<=100000).sum()) if not x.empty else 0
    days_under_300k=int((x<=300000).sum()) if not x.empty else 0
    if days_under_100k>=14 or (current_rank is not None and current_rank<=50000 and median_rank<=100000): return 'STRONG',days_since
    if days_under_300k>=14 or (current_rank is not None and current_rank<=150000 and median_rank<=300000): return 'MODERATE',days_since
    if seasonal and drops90<3:return 'SEASONAL',days_since
    if drops90>=5 and days_since is not None and days_since<=45:return 'STRONG',days_since
    if drops90>=3:return 'MODERATE',days_since
    return 'WEAK',days_since



def history_confidence(price_s, rank_s):
    starts=[x.index.min() for x in (price_s,rank_s) if isinstance(x,pd.Series) and not x.empty]
    ends=[x.index.max() for x in (price_s,rank_s) if isinstance(x,pd.Series) and not x.empty]
    if not starts or not ends:return 'NONE',0
    days=max(0,int((max(ends)-min(starts)).days))
    if days>=730:return 'HIGH',days
    if days>=365:return 'MEDIUM',days
    return 'LIMITED',days

def amazon_price_trend(amz_s):
    if amz_s.empty:return 'NO DATA',None
    x=amz_s[amz_s.index>=amz_s.index.max()-pd.Timedelta(days=90)].dropna()
    if len(x)<14:return 'NEW DATA',None
    early=float(x.iloc[:max(3,len(x)//3)].median()); late=float(x.iloc[-max(3,len(x)//3):].median())
    if early<=0:return 'STABLE',None
    pct=(late/early-1)*100
    if pct<=-10:return 'FALLING',round(pct,1)
    if pct>=10:return 'RISING',round(pct,1)
    return 'STABLE',round(pct,1)

def seasonal_mine_v2(used_s, rank_s, count_used_s, book_type):
    """Detect recurring seasonal windows using price, demand, and Used-seller supply."""
    out = {
        'Seasonal V2': False, 'Seasonal Confidence': 'LOW',
        'Seasonal Peak Month': None, 'Seasonal Cycles': 0,
        'Seasonal Value V2': None, 'Seasonal Evidence': 'Insufficient recurring seasonal evidence.'
    }
    if used_s.empty or rank_s.empty:
        return out

    u = used_s.dropna()
    r = rank_s.dropna()
    if len(u) < 30 or len(r) < 30:
        return out

    # Daily series, forward-filled only inside observed history.
    start = max(u.index.min(), r.index.min())
    end = min(u.index.max(), r.index.max())
    if start >= end:
        return out
    idx = pd.date_range(start.normalize(), end.normalize(), freq='D')
    ud = u.reindex(idx).ffill()
    rd = r.reindex(idx).ffill()
    cd = count_used_s.dropna().reindex(idx).ffill() if count_used_s is not None and not count_used_s.empty else pd.Series(index=idx, dtype=float)

    frame = pd.DataFrame({'price': ud, 'rank': rd, 'offers': cd})
    frame['year'] = frame.index.year
    frame['month'] = frame.index.month

    monthly = frame.groupby(['year','month']).agg(
        price=('price','median'),
        rank=('rank','median'),
        offers=('offers','median')
    ).reset_index()

    # Need at least two prior annual observations for a month.
    current_year = int(end.year)
    prior = monthly[monthly['year'] < current_year].copy()
    if prior.empty:
        return out

    overall_price = float(prior['price'].median()) if prior['price'].notna().any() else None
    overall_rank = float(prior['rank'].median()) if prior['rank'].notna().any() else None
    overall_offers = float(prior['offers'].median()) if prior['offers'].notna().any() else None
    candidates = []

    for month in range(1,13):
        mm = prior[prior['month'] == month].dropna(subset=['price','rank'])
        years = mm['year'].nunique()
        if years < 2:
            continue
        p = float(mm['price'].median())
        rk = float(mm['rank'].median())
        off = float(mm['offers'].median()) if mm['offers'].notna().any() else None

        price_lift = (p / overall_price - 1.0) if overall_price and overall_price > 0 else 0.0
        rank_improve = (1.0 - rk / overall_rank) if overall_rank and overall_rank > 0 else 0.0
        supply_drop = (1.0 - off / overall_offers) if off is not None and overall_offers and overall_offers > 0 else 0.0

        # Price and demand must agree; supply decline strengthens confidence.
        score = max(price_lift,0)*0.45 + max(rank_improve,0)*0.40 + max(supply_drop,0)*0.15
        if price_lift >= 0.08 and rank_improve >= 0.08:
            candidates.append((score, month, years, p, price_lift, rank_improve, supply_drop))

    if not candidates:
        return out

    candidates.sort(reverse=True)
    score, peak_month, years, seasonal_value, price_lift, rank_improve, supply_drop = candidates[0]

    # Count recurring cycles. Education/textbook types may legitimately show 2–4 annual windows.
    qualified_months = [x for x in candidates if x[0] >= max(0.08, score*0.55)]
    cycle_months = []
    for x in sorted(qualified_months, key=lambda z:z[1]):
        if not cycle_months or min((x[1]-m)%12 for m in cycle_months) >= 2:
            cycle_months.append(x[1])
    cycles = len(cycle_months)
    edu = any(k in str(book_type) for k in ('College','High School','Education','Test Prep'))
    if edu:
        cycles = min(max(cycles,1),4)
    else:
        cycles = min(max(cycles,1),2)

    confidence = 'HIGH' if years >= 3 and score >= 0.18 and (supply_drop >= 0.05 or price_lift >= 0.20) else ('MEDIUM' if years >= 2 and score >= 0.10 else 'LOW')
    out.update({
        'Seasonal V2': confidence in ('HIGH','MEDIUM'),
        'Seasonal Confidence': confidence,
        'Seasonal Peak Month': int(peak_month),
        'Seasonal Cycles': int(cycles),
        'Seasonal Value V2': round(float(seasonal_value),2),
        'Seasonal Evidence': f"{years} prior years; price +{price_lift*100:.0f}%, rank improves {rank_improve*100:.0f}%"
                             + (f", Used supply -{supply_drop*100:.0f}%" if overall_offers and supply_drop > 0 else "")
    })
    return out

def supply_meter(offers,drops90):
    if offers is None:return '⚪ UNKNOWN'
    # Demand-adjusted heuristic: compare current Used offer count with 90-day rank-drop activity.
    monthly=max(drops90/3.0,0.5); ratio=offers/monthly
    if offers<=5 or ratio<=0.8:return '🟢 LOW'
    if ratio<=2.0 and offers<=20:return '🟡 BALANCED'
    return '🔴 HIGH'

def shipped_used_reference(current):
    vals=[]
    for idx in (USED_LN_SHIP,USED_VG_SHIP,USED_GOOD_SHIP,USED_ACC_SHIP,USED_BB_SHIP):
        v=at_price(current,idx)
        if v is not None and v>0: vals.append(v)
    return min(vals) if vals else None

def supply_trend(offer_s):
    if offer_s.empty:return ('UNKNOWN',None,None)
    end=offer_s.index.max()
    now=float(offer_s.iloc[-1])
    old=offer_s[offer_s.index<=end-pd.Timedelta(days=90)]
    if old.empty:return ('NEW DATA',None,None)
    past=float(old.iloc[-1])
    if past<=0:return ('UNKNOWN',past,now)
    pct=(now-past)/past*100
    if pct<=-30:return ('⬇️ SQUEEZE',past,now)
    if pct>=30:return ('⬆️ RISING',past,now)
    return ('➡️ STABLE',past,now)

def supply_momentum(offer_s):
    """Recent Used-seller direction. Rising supply is a competition risk, not weak demand."""
    if offer_s is None or offer_s.empty:
        return ('UNKNOWN',None,None,None)
    x=offer_s.dropna().sort_index()
    if x.empty:
        return ('UNKNOWN',None,None,None)
    end=x.index.max(); now=float(x.iloc[-1])
    def prior(days):
        old=x[x.index<=end-pd.Timedelta(days=days)]
        return float(old.iloc[-1]) if not old.empty else None
    p30,p90=prior(30),prior(90)
    def pct(old):
        return None if old is None or old<=0 else (now-old)/old*100.0
    q30,q90=pct(p30),pct(p90)
    signals=[v for v in (q30,q90) if v is not None]
    if not signals:
        label='NEW DATA'
    elif (q30 is not None and q30>=40) or (q90 is not None and q90>=60):
        label='⬆️ RISING FAST'
    elif (q30 is not None and q30>=20) or (q90 is not None and q90>=30):
        label='⬆️ RISING'
    elif (q30 is not None and q30<=-25) or (q90 is not None and q90<=-35):
        label='⬇️ FALLING'
    else:
        label='➡️ STABLE'
    return (label, round(q30,1) if q30 is not None else None, round(q90,1) if q90 is not None else None, now)

def is_book_product(product):
    """Reject obvious non-book media that occasionally leaks into discovery/category data."""
    if not isinstance(product,dict): return False
    title=str(product.get('title') or '').lower()
    group=' '.join(str(product.get(k) or '') for k in ('productGroup','productTypeName','binding','websiteDisplayGroupName')).lower()
    cats=product.get('categoryTree') or []
    cat_text=' '.join(str(c.get('name','')) for c in cats if isinstance(c,dict)).lower()
    text=' '.join((title,group,cat_text))
    media_terms=('blu-ray','blu ray',' dvd','dvd ','[dvd]','4k uhd','ultra hd','audio cd','music cd','vinyl','video game','vhs')
    if any(t in text for t in media_terms): return False
    # Keepa/Amazon US Books root category. Product Finder already requests this root;
    # this is a second safety check when detail metadata exposes a conflicting root.
    root=product.get('rootCategory')
    if root not in (None,0,283155,9223372036854775807):
        return False
    if any(k in group or k in cat_text for k in ('book','books','kindle','textbook')):
        return True
    # ISBN-bearing physical listings are overwhelmingly books; allow when metadata is sparse.
    if product.get('isbn') or product.get('eanList'):
        return True
    # Product Finder was already restricted to Books; accept metadata-sparse results unless
    # an explicit non-book signal above rejected them.
    return root==283155

def recovery_cycles(s,days=730):
    if s.empty:return 0
    x=s[s.index>=s.index.max()-pd.Timedelta(days=days)].dropna()
    if len(x)<60:return 0
    med=float(x.median())
    if med<=0:return 0
    low=x<med*.60
    recovered=x>med*.85
    cycles=0; in_low=False
    for lo,rec in zip(low.values,recovered.values):
        if lo: in_low=True
        elif in_low and rec:
            cycles+=1; in_low=False
    return cycles

def price_crash_signal(current_used, med365, floor):
    if current_used is None or med365 is None or med365<=0:return ''
    pct=(1-current_used/med365)*100
    if pct>=50 and floor is not None and floor>=current_used*1.5:return f'🔥 CRASH {pct:.0f}% below median'
    if pct>=30:return f'📉 {pct:.0f}% below median'
    return ''


# ---------- V6.2 fee engine ----------
# 2026 US FBA fulfillment rate card (excluding apparel/dangerous goods).
# Amazon applies a 3.5% fuel/logistics surcharge to these base fulfillment fees.
FUEL_SURCHARGE = 0.035

def product_weight_lb(p):
    """Keepa packageWeight/itemWeight are grams. Prefer package weight for FBA economics."""
    vals=[]
    for k in ('packageWeight','itemWeight'):
        v=p.get(k)
        if isinstance(v,(int,float)) and v>0:
            vals.append(float(v)/453.59237)
    return vals[0] if vals else None

def product_dimensions_in(p):
    """Keepa package dimensions are millimeters. Returns L,W,H inches when available."""
    vals=[]
    for k in ('packageLength','packageWidth','packageHeight'):
        v=p.get(k)
        if not isinstance(v,(int,float)) or v<=0:
            return None
        vals.append(float(v)/25.4)
    return tuple(vals)

def dimensional_weight_lb(dims):
    if not dims:return None
    l,w,h=dims
    return (l*w*h)/139.0

def fba_size_tier(weight_lb,dims):
    """Practical standard-size classifier for books; nonstandard items are flagged for manual review."""
    if weight_lb is None:
        return 'UNKNOWN', None
    if dims:
        l,w,h=sorted(dims,reverse=True)
        # Small standard: <=15 x 12 x 0.75 and <=16 oz
        if l<=15 and w<=12 and h<=0.75 and weight_lb<=1:
            return 'SMALL_STANDARD', weight_lb
        # Large standard: <=18 x 14 x 8 and <=20 lb; dimensional weight can apply.
        if l<=18 and w<=14 and h<=8 and weight_lb<=20:
            dw=dimensional_weight_lb(dims)
            return 'LARGE_STANDARD', max(weight_lb,dw or 0)
        return 'NONSTANDARD', max(weight_lb,dimensional_weight_lb(dims) or 0)
    # Most books without dimensions are safer treated as large-standard.
    return 'LARGE_STANDARD', weight_lb

def _price_band(sell_price):
    if sell_price < 10:return 0
    if sell_price <= 50:return 1
    return 2

def _lookup_standard_fee(tier, shipping_lb, sell_price, peak):
    """Base 2026 fee before 3.5% fuel surcharge. Returns None for nonstandard."""
    band=_price_band(sell_price)
    if tier=='SMALL_STANDARD':
        oz=shipping_lb*16
        # thresholds and [<$10, $10-$50, >$50]
        rows=[
            (2,[2.62,3.51,3.77]),(4,[2.68,3.61,3.87]),(6,[2.76,3.65,3.91]),
            (8,[2.86,3.74,4.00]),(10,[2.98,3.89,4.15]),(12,[3.03,3.99,4.25]),
            (14,[3.14,4.13,4.39]),(16,[3.17,4.18,4.44])]
        # Peak table above. For non-peak, use Amazon 2026 peak deltas conservatively by
        # subtracting $0.19 for low-price and $0.39 for regular standard-size bands.
        for lim,fees in rows:
            if oz<=lim:
                base=fees[band]
                if not peak: base-=0.19 if band==0 else 0.39
                return base
    if tier=='LARGE_STANDARD':
        oz=shipping_lb*16
        peak_rows=[
            (4,[3.15,3.97,4.23]),(8,[3.39,4.21,4.47]),(12,[3.66,4.48,4.74]),
            (16,[4.07,4.89,5.15]),(20,[4.52,5.34,5.60]),(24,[4.91,5.73,5.99]),
            (28,[5.07,5.89,6.15]),(32,[5.33,6.15,6.41]),(36,[5.47,6.29,6.55]),
            (40,[5.67,6.49,6.75]),(44,[5.84,6.66,6.92]),(48,[6.07,6.89,7.15])]
        for lim,fees in peak_rows:
            if oz<=lim:
                base=fees[band]
                if not peak: base-=0.39 if band==0 else 0.54
                return base
        # Above 3 lb through 20 lb: Amazon rate is base + $0.08 per additional 4 oz interval.
        if shipping_lb<=20:
            base=[6.15,6.97,7.23][band] if not peak else [6.69,7.51,7.77][band]
            shipping_lb=float(shipping_lb)
            intervals=int(math.ceil(float(max(0.0, shipping_lb-3.0)*4.0)))
            return base + 0.08*intervals
    return None

def is_peak_fba_date(d=None):
    d=d or datetime.now().date()
    # 2026 holiday peak: Oct 15, 2026 through Jan 14, 2027.
    return d>=datetime(2026,10,15).date() and d<=datetime(2027,1,14).date()

def fba_fulfillment_fee(p,sell_price):
    wt=product_weight_lb(p); dims=product_dimensions_in(p)
    tier,ship_wt=fba_size_tier(wt,dims)
    if sell_price is not None:
        sell_price=float(sell_price)
    if ship_wt is not None:
        ship_wt=float(ship_wt)
    if sell_price is None or ship_wt is None:
        return {'fee':None,'weight_lb':wt,'shipping_weight_lb':ship_wt,'tier':tier,'peak':is_peak_fba_date(),'needs_review':True}
    base=_lookup_standard_fee(tier,ship_wt,float(sell_price),is_peak_fba_date())
    fee=round(base*(1+FUEL_SURCHARGE),2) if base is not None else None
    return {'fee':fee,'weight_lb':wt,'shipping_weight_lb':ship_wt,'tier':tier,'peak':is_peak_fba_date(),'needs_review':fee is None}

def fee_breakdown(p,sell_price,inbound_per_lb,prep_cost):
    f=fba_fulfillment_fee(p,sell_price)
    referral=round(float(sell_price)*0.15,2) if sell_price is not None else None
    closing=1.80 if sell_price is not None else None
    inbound=round((f['weight_lb'] or 0)*inbound_per_lb,2) if f['weight_lb'] is not None else None
    amazon=round(referral+closing+f['fee'],2) if referral is not None and f['fee'] is not None else None
    all_cost=round(amazon+inbound+prep_cost,2) if amazon is not None and inbound is not None else None
    return {**f,'referral':referral,'closing':closing,'inbound':inbound,'prep':prep_cost,
            'amazon_fees':amazon,'fees_before_buy':all_cost}


def classify_book(p):
    """Heuristic book-type classifier for sourcing/seasonality context."""
    if isinstance(p, dict):
        title = str(p.get("title") or "").lower()
        brand = str(p.get("brand") or "").lower()
        manufacturer = str(p.get("manufacturer") or "").lower()
        publisher = str(p.get("publisher") or "").lower()
        binding = str(p.get("binding") or "").lower()
    else:
        # Existing analysis pipeline passes the title directly.
        title = str(p or "").lower()
        brand = manufacturer = publisher = binding = ""
    text = " ".join([title, brand, manufacturer, publisher, binding])

    college_terms = [
        "anthology", "textbook", "college", "university", "calculus",
        "organic chemistry", "microeconomics", "macroeconomics",
        "anatomy and physiology", "statistics", "accounting",
        "college algebra", "nursing", "edition", "volume i", "volume ii"
    ]
    school_terms = [
        "my story", "student edition", "teacher edition", "teacher's edition",
        "workbook", "curriculum", "homeschool", "high school", "middle school",
        "grade 6", "grade 7", "grade 8", "grade 9", "grade 10", "grade 11",
        "grade 12", "language arts", "school"
    ]
    test_terms = [
        "sat", "act prep", "gre", "gmat", "lsat", "mcat", "ap exam",
        "test prep", "practice tests", "study guide"
    ]
    professional_terms = [
        "certification", "professional", "clinical", "engineering",
        "medical", "law handbook", "technical manual"
    ]
    holiday_terms = [
        "christmas", "advent", "hanukkah", "halloween", "thanksgiving",
        "easter", "valentine"
    ]

    if any(x in text for x in test_terms):
        return "Test Prep"
    if any(x in text for x in holiday_terms):
        return "Holiday/Seasonal"
    if any(x in text for x in school_terms):
        return "High School/Education"
    if any(x in text for x in college_terms):
        return "College/Textbook"
    if any(x in text for x in professional_terms):
        return "Professional/Technical"
    return "General/All-Year"

def month_name(m):return datetime(2000,m,1).strftime('%b') if m else ''

def mini_chart(price_s,amazon_s,rank_s,days,title):
    # V7.5: Keepa can return an empty series with a RangeIndex for one
    # history (commonly Amazon). Never compare that integer index to dates.
    def dated(s):
        if s is None or not isinstance(s,pd.Series) or s.empty:
            return pd.Series(dtype=float)
        x=s.copy()
        if not isinstance(x.index,pd.DatetimeIndex):
            try:
                idx=pd.to_datetime(x.index,errors='coerce')
                good=~pd.isna(idx)
                x=x.iloc[list(good)]
                x.index=idx[good]
            except Exception:
                return pd.Series(dtype=float)
        if getattr(x.index,'tz',None) is not None:
            x.index=x.index.tz_localize(None)
        return x.sort_index()
    price_s,amazon_s,rank_s=map(dated,(price_s,amazon_s,rank_s))
    ends=[x.index.max() for x in (price_s,amazon_s,rank_s) if not x.empty]
    if not ends:return None
    end=max(ends); start=end-pd.Timedelta(days=days)
    p=price_s.loc[price_s.index>=start] if not price_s.empty else price_s
    a=amazon_s.loc[amazon_s.index>=start] if not amazon_s.empty else amazon_s
    r=rank_s.loc[rank_s.index>=start] if not rank_s.empty else rank_s
    fig,ax1=plt.subplots(figsize=(5.2,2.0))
    if not p.empty:ax1.plot(p.index,p.values,color='black',linewidth=1.35,label='Used')
    if not a.empty:ax1.plot(a.index,a.values,color='#f5a000',linewidth=1.35,label='Amazon')
    if not p.empty or not a.empty:ax1.legend(loc='upper left',fontsize=6,frameon=False,ncol=2)
    ax1.set_ylabel('Price $',fontsize=7); ax1.grid(alpha=.18); ax1.tick_params(axis='both',labelsize=7)
    ax2=ax1.twinx()
    if not r.empty:ax2.plot(r.index,r.values,color='green',linewidth=.8,alpha=.8)
    # Keepa-style orientation: larger/worse rank is higher; #1/better rank is lower.
    # Matplotlib's normal numeric y-axis already has this orientation, so do NOT invert it.
    ax2.set_ylabel('Rank',fontsize=7); ax2.tick_params(axis='y',labelsize=7); ax1.set_title(title,fontsize=8,loc='left'); fig.tight_layout()
    return fig

def keepa_fba_fulfillment_fee(product):
    """Best available FBA fulfillment reference from Keepa product metadata.

    Keepa commonly exposes fbaFees.pickAndPackFee in cents. This is a
    screening estimate; Amazon's Revenue Calculator / Product Fees API is the
    authority for the exact fee at a given price and date.
    """
    f=product.get('fbaFees') or {}
    for k in ('pickAndPackFee','fulfillmentFee','fee'):
        v=f.get(k) if isinstance(f,dict) else None
        if isinstance(v,(int,float)) and v>=0:
            return round(v/100.0,2)
    return None

def amazon_book_fees(sell_price, fba_fee):
    """Amazon Books screening fee model for a Professional FBA seller.

    Books: 15% referral fee + $1.80 media closing fee + FBA fulfillment.
    The FBA component is editable because exact size/weight, peak rates,
    surcharges and account-specific economics can change.
    """
    if sell_price is None or sell_price<=0 or fba_fee is None:
        return None
    referral=round(float(sell_price)*0.15,2)
    closing=1.80
    fulfillment=round(float(fba_fee),2)
    total=round(referral+closing+fulfillment,2)
    return {'referral':referral,'closing':closing,'fulfillment':fulfillment,'total':total}

def max_buy_with_dynamic_fees(sell_price, fba_fee, extra_cost, min_profit, min_roi, max_cost):
    fees=amazon_book_fees(sell_price,fba_fee)
    if not fees:return None
    net_before_buy= sell_price-fees['total']-extra_cost
    by_profit=net_before_buy-min_profit
    # ROI = profit / (buy + inbound/prep). Solve for buy.
    r=min_roi/100.0
    by_roi=(net_before_buy-r*extra_cost)/(1+r)
    return round(max(0,min(max_cost,by_profit,by_roi)),2)

def decision_reason(action,buy,floor,maxbuy,drops,offers):
    if action=='🟢 BUY':return 'Passes conservative value, profit and ROI rules.'
    if action=='🟠 BUY & HOLD':return 'Recurring seasonal value passes your rules.'
    if floor is None:return 'Not enough usable Used-price history.'
    if buy is not None and maxbuy is not None and buy>maxbuy:return f'Buy cost is ${buy-maxbuy:.2f} above max buy.'
    if drops<=1:return 'Demand signal is weak; inspect Keepa before deciding.'
    if offers is not None and supply_meter(offers,drops).endswith('HIGH'):return 'Supply is high relative to recent demand.'
    return 'Needs manual review.'

st.subheader('1 · Mine new Used-book opportunities')
st.write('Choose **Opportunity Mine**, **Value Mine**, **Recent Used Drops**, or **Seasonal Mine**. V7.7 uses immediate-profit discovery before detailed analysis, and previously marked ASINs are removed.')
st.caption('V6 keeps generic USED history visible, but BUY economics use demand-supported value when enough rank-drop evidence exists. When Keepa exposes a shipping-inclusive Used condition/Used Buy Box reference, V7.7 uses it as the starting shipped-cost estimate; always verify checkout.')

button_label = ('🚀 Find best opportunities' if mining_mode.startswith('🚀') else ('⚡ Find recent Used price drops' if mining_mode.startswith('⚡') else ('🎓 Find seasonal candidates' if mining_mode.startswith('🎓') else '🔎 Discover value opportunities')))
if st.button(button_label,disabled=not bool(key),type='primary'):
    try:
        if mining_mode.startswith('⚡'):
            with st.spinner('Searching Keepa Deals for recent Used drops…'):
                response=discover_deals(key,max_rank,max_cost,min_drop_pct,deal_window[1])
            deals=response.get('deals',{}).get('dr',[])
            raw=[d.get('asin') for d in deals if d.get('asin')]
            deal_meta={}
            for d in deals:
                asin=d.get('asin')
                dp=d.get('deltaPercent',[])
                pct=None
                try:
                    pct=dp[deal_window[1]][USED]
                except Exception: pass
                deal_meta[asin]={'drop_pct':pct,'source':'Recent Used Drop'}
            st.session_state['deal_meta_v5']=deal_meta
        elif mining_mode.startswith('🚀'):
            with st.spinner('Building and ranking a larger token-light opportunity pool…'):
                response=discover_ranked(key,max_rank,max_cost,prefilter_value,min_value_spread)
            raw=response.get('asinList',[])
            st.session_state['deal_meta_v5']=response.get('meta',{})
        else:
            with st.spinner('Searching Keepa…'):
                response=discover(key,max_rank,max_cost,prefilter_value,min_value_spread)
            raw=response.get('asinList',[])
            # Product Finder already enforces current Used <= max buy and avg90 Used >=
            # our quality floor server-side. Keep this stage intentionally cheap:
            # no extra product-history call is made here.
            st.session_state['deal_meta_v5']={a:{'source':'Seasonal Mine' if mining_mode.startswith('🎓') else 'Value Mine'} for a in raw}
        # V7.5: build the skip set FIRST, from fresh persistent history plus every
        # in-session source that can prove an ASIN has already consumed detail tokens.
        norm=lambda a: str(a).strip().upper() if a else ''
        fresh_statuses=load_statuses()
        known={norm(a) for a in fresh_statuses.keys() if norm(a)}
        seen={norm(a) for a in session_seen_asins() if norm(a)}
        board_seen={norm(x.get('ASIN')) for x in st.session_state.get('books_v5',[]) if norm(x.get('ASIN'))}
        prior_selected={norm(a) for a in st.session_state.get('last_detail_asins_v741',[]) if norm(a)}
        legacy={norm(a) for a in LEGACY_ANALYZED_ASINS if norm(a)}
        skip=known | seen | board_seen | prior_selected | legacy
        raw=[norm(a) for a in raw if norm(a)]
        new=[a for a in raw if a not in skip and a not in KNOWN_NONBOOK_ASINS]
        st.session_state['asins_v5_raw']=raw; st.session_state['asins_v5']=new
        marked_n=len(set(raw)&known)
        analyzed_n=len(set(raw)&((seen|board_seen|prior_selected|legacy)-known))
        st.session_state['discovery_summary_v74']={'returned':len(raw),'marked':marked_n,'analyzed':analyzed_n,'eligible':len(new)}
        st.success(f'Keepa returned {len(raw)} candidates → skipped {marked_n} marked + {analyzed_n} already analyzed → {len(new)} NEW candidates eligible. Tokens left: {response.get("tokensLeft","unknown")}')
    except Exception as e:st.error(f'Keepa discovery failed: {e}')

asins=st.session_state.get('asins_v5',[])
if asins:
    st.markdown('#### Free candidate preview')
    st.caption('Keepa Product Finder returns ASINs at this stage without full product-history detail. Showing the ASINs costs no additional detail-history tokens; titles and demand-supported values appear only after analysis.')
    preview_n=min(30,len(asins))
    if mining_mode.startswith('🚀'):
        dm=st.session_state.get('deal_meta_v5',{})
        preview_rows=[]
        for a in asins[:preview_n]:
            m=dm.get(a,{})
            preview_rows.append({'ASIN':a,'Opportunity lane':m.get('lane','Opportunity'),'Discovery score':m.get('discovery_score')})
        st.dataframe(pd.DataFrame(preview_rows),hide_index=True,use_container_width=True)
        st.caption('Discovery score is a token-light ranking signal only. Final BUY/ANALYZE/PASS still comes from detailed demand-supported analysis.')
    else:
        st.dataframe(pd.DataFrame({'ASIN':asins[:preview_n]}),hide_index=True,use_container_width=True)
    n=st.slider('New books to analyze now',1,min(20,len(asins)),min(5,len(asins)))
    st.caption('V7.7 ranks Opportunity Mine for immediate A2A economics first: cheap current Used cost must have enough historical/New resale headroom to plausibly clear FBA fees, $5 profit and 50% ROI. Detailed history still makes the final decision; attractive recurring seasonal books are kept in a separate SEASONAL WATCH lane.')
    if st.button(f'Analyze first {n} new books'):
        try:
            selected_detail_asins=[str(a).strip().upper() for a in asins[:n]]
            # Record selection before the paid detail call so a rerun cannot immediately
            # select the same ASINs again even if the remote history write is unavailable.
            st.session_state['last_detail_asins_v741']=sorted(set(st.session_state.get('last_detail_asins_v741',[])) | set(selected_detail_asins))
            remember_analyzed(selected_detail_asins)
            with st.spinner('Analyzing Used history, rank drops and supply…'):response=details(key,selected_detail_asins)
            raw_products=response.get('products',[])
            persist_raw_products(raw_products)
            remember_analyzed([p.get('asin') for p in raw_products])
            statuses=load_statuses()
            rows=[]; histories={}
            nonbooks=[]
            for p in response.get('products',[]):
                asin=p.get('asin'); title=p.get('title','')
                if not is_book_product(p):
                    nonbooks.append((asin,title))
                    continue
                current=p.get('stats',{}).get('current',[]); csv=p.get('csv',[])
                used=at_price(current,USED); amazon_price=at_price(current,AMAZON); new_price=at_price(current,NEW); rank=at_raw(current,SALES); offers=int(at_raw(current,COUNT_USED)) if at_raw(current,COUNT_USED) is not None else None
                used_hist=keepa_series(csv[USED],100.0) if isinstance(csv,list) and len(csv)>USED else pd.Series(dtype=float)
                rank_hist=keepa_series(csv[SALES],1.0) if isinstance(csv,list) and len(csv)>SALES else pd.Series(dtype=float)
                amazon_hist=keepa_series(csv[AMAZON],100.0) if isinstance(csv,list) and len(csv)>AMAZON else pd.Series(dtype=float)
                offer_hist=keepa_series(csv[COUNT_USED],1.0) if isinstance(csv,list) and len(csv)>COUNT_USED else pd.Series(dtype=float)
                floor=intrinsic_floor(used_hist,floor_days,low_band); ds=demand_supported_value(used_hist,rank_hist,max(730,floor_days)); season=seasonal_model(used_hist); drops90=rank_drop_count(rank_hist,90)
                shipped_ref=shipped_used_reference(current); strend,offers90,offers_now=supply_trend(offer_hist); smomentum,supply30,supply90,_=supply_momentum(offer_hist); recoveries=recovery_cycles(used_hist)
                x365=used_hist[used_hist.index>=used_hist.index.max()-pd.Timedelta(days=365)] if not used_hist.empty else pd.Series(dtype=float)
                med=round(float(x365.median()),2) if not x365.empty else None
                histories[asin]=(used_hist,amazon_hist,rank_hist)
                dealmeta=st.session_state.get('deal_meta_v5',{}).get(asin,{})
                source=dealmeta.get('source','Value Mine')
                drop_pct=dealmeta.get('drop_pct')
                crash=price_crash_signal(used,med,floor)
                book_type=classify_book(title); dq,last_drop_days=demand_quality(rank_hist,book_type,drops90)
                hist_conf,hist_days=history_confidence(used_hist,rank_hist); amz_trend,amz_trend_pct=amazon_price_trend(amazon_hist)
                seasonal_v2=seasonal_mine_v2(used_hist,rank_hist,offer_hist,book_type)
                rows.append({'ASIN':asin,'Title':title,'Source':source,'Recent Drop %':drop_pct,'Book Type':book_type,
                             'Seasonal V2':seasonal_v2['Seasonal V2'],
                             'Seasonal Confidence':seasonal_v2['Seasonal Confidence'],
                             'Seasonal Peak Month':seasonal_v2['Seasonal Peak Month'],
                             'Seasonal Cycles':seasonal_v2['Seasonal Cycles'],
                             'Seasonal Value V2':seasonal_v2['Seasonal Value V2'],
                             'Seasonal Evidence':seasonal_v2['Seasonal Evidence'],
                             'Rank':rank,
                    'Current Used ref ($)':used,'Amazon Price ($)':amazon_price,'New Price ($)':new_price,'Buy Cost SHIPPED ($)':shipped_ref if shipped_ref is not None else used,
                    'Shipped Ref Available':shipped_ref is not None,'FBA Fulfillment Fee ($)':keepa_fba_fulfillment_fee(p),'Intrinsic ($)':floor,'Demand-Supported Value ($)':ds['value'] if ds else None,'Demand Events':ds['events'] if ds else 0,'Demand-Supported Median ($)':ds['median'] if ds else None,'Demand Value Reason':supported_value_reason(ds,floor,med),'365d Median ($)':med,
                    'Rank Drops 90d':drops90,'Demand Quality':dq,'Days Since Last Drop':last_drop_days,'Used Offers':offers,'Supply Meter':supply_meter(offers,drops90),
                    'Supply Trend':strend,'Supply Momentum':smomentum,'Supply Δ30d %':supply30,'Supply Δ90d %':supply90,'Offers ~90d Ago':offers90,'Recovery Cycles':recoveries,'Price Signal':crash,'History Confidence':hist_conf,'History Days':hist_days,'Amazon Price Trend':amz_trend,'Amazon Trend %':amz_trend_pct,
                    'Seasonal Value ($)':season['value'] if season else None,'Peak':month_name(season['month']) if season else '',
                    'Seasonal Years':season['years'] if season else 0,'Seasonal Confidence':season['confidence'] if season else 'None',
                    'Amazon':f'https://www.amazon.com/dp/{asin}','Keepa':f'https://keepa.com/#!product/1-{asin}','_product':p})
            if mining_mode.startswith('🎓'):
                rows=sorted(rows,key=lambda x:(0 if x['Seasonal Value ($)'] is not None else 1,-x['Seasonal Years']))
            st.session_state['books_v5']=rows; st.session_state['histories_v5']=histories
            remember_analyzed([r.get('ASIN') for r in rows])
            statuses=load_statuses()
            st.success(f'Analyzed {len(rows)} books. Tokens remaining: {response.get("tokensLeft","unknown")}')
            if nonbooks:
                st.warning('Filtered non-book media from the board: ' + ', '.join(f'{a} {t[:45]}' for a,t in nonbooks))
        except Exception as e:st.error(f'Keepa details failed: {e}')

books=st.session_state.get('books_v5',[])
if books:
    st.subheader('2 · Verify shipped buy cost')
    df=pd.DataFrame(books)
    st.caption("V7.7 pre-fills **Buy Cost SHIPPED** from Keepa when available. FBA fulfillment is calculated from the book's weight/dimensions and the 2026 rate model; verify borderline buys against RevSeller/Amazon Revenue Calculator.")
    edited=st.data_editor(df[['ASIN','Title','Source','Buy Cost SHIPPED ($)','Current Used ref ($)','Amazon Price ($)','New Price ($)','Intrinsic ($)','Demand-Supported Value ($)','Demand Events','365d Median ($)','Rank Drops 90d','Demand Quality','Days Since Last Drop','Used Offers','Supply Meter','Supply Trend','Supply Momentum','Supply Δ30d %','Supply Δ90d %','Price Signal']],hide_index=True,use_container_width=True,disabled=[c for c in ['ASIN','Title','Source','Current Used ref ($)','Amazon Price ($)','New Price ($)','Intrinsic ($)','Demand-Supported Value ($)','Demand Events','365d Median ($)','Rank Drops 90d','Demand Quality','Days Since Last Drop','Used Offers','Supply Meter','Supply Trend','Supply Momentum','Supply Δ30d %','Supply Δ90d %','Price Signal'] if c in df.columns])
    buy_map=dict(zip(edited['ASIN'],edited['Buy Cost SHIPPED ($)']))

    results=[]
    for b in books:
        buy=buy_map.get(b['ASIN']); floor=b['Intrinsic ($)']; supported=b.get('Demand-Supported Value ($)'); seasonal=b['Seasonal Value ($)']
        valuation=supported if supported is not None else floor
        buy=float(buy) if pd.notna(buy) else None
        new_candidates=[v for v in (b.get('Amazon Price ($)'),b.get('New Price ($)')) if isinstance(v,(int,float)) and v>0]
        new_market_price=min(new_candidates) if new_candidates else None
        used_value_ceiling=round(new_market_price*0.90,2) if new_market_price is not None else None
        raw_valuation=valuation
        if valuation is not None and used_value_ceiling is not None:
            valuation=min(valuation,used_value_ceiling)
        fb=fee_breakdown(b.get('_product',{}),valuation,inbound_per_lb,prep_cost) if valuation is not None else None

        amazon_fees=fb['amazon_fees'] if fb else None
        inbound_cost=fb['inbound'] if fb else None
        total_before_buy=fb['fees_before_buy'] if fb else None

        profit=round(valuation-buy-total_before_buy,2) if buy is not None and valuation is not None and total_before_buy is not None else None
        roi=round(100*profit/(buy+inbound_cost+prep_cost),1) if profit is not None and (buy+(inbound_cost or 0)+prep_cost)>0 else None

        # V7.5 seasonal economics: use the recurring Seasonal V2 value when established.
        seasonal_v2=b.get('Seasonal Value V2') if b.get('Seasonal V2') else None
        seasonal=seasonal_v2 if seasonal_v2 is not None else seasonal
        peak_month=b.get('Seasonal Peak Month')
        now_month=datetime.now(timezone.utc).month
        months_to_peak=((int(peak_month)-now_month) % 12) if peak_month else None
        # Small capital/time haircut: 1% per month until the next modeled peak, capped at 12%.
        hold_haircut=min(0.12,0.01*(months_to_peak or 0)) if seasonal is not None else 0
        seasonal_net_value=round(seasonal*(1-hold_haircut),2) if seasonal is not None else None
        sfb=fee_breakdown(b.get('_product',{}),seasonal_net_value,inbound_per_lb,prep_cost) if seasonal_net_value is not None else None
        sprofit=round(seasonal_net_value-buy-sfb['fees_before_buy'],2) if buy is not None and seasonal_net_value is not None and sfb and sfb['fees_before_buy'] is not None else None
        sroi=round(100*sprofit/(buy+(sfb['inbound'] or 0)+prep_cost),1) if sprofit is not None and (buy+(sfb['inbound'] or 0)+prep_cost)>0 else None
        if seasonal_net_value is not None and sfb and sfb['fees_before_buy'] is not None:
            snet_before_buy=seasonal_net_value-sfb['fees_before_buy']
            smax_profit_buy=snet_before_buy-min_profit
            smax_roi_buy=(snet_before_buy-(min_roi/100)*((sfb['inbound'] or 0)+prep_cost))/(1+min_roi/100)
            seasonal_maxbuy=round(max(0,min(max_cost,smax_profit_buy,smax_roi_buy)),2)
        else:
            seasonal_maxbuy=None

        # Max Buy: satisfy both minimum dollar profit and ROI after all non-acquisition costs.
        if valuation is not None and total_before_buy is not None:
            net_before_buy=valuation-total_before_buy
            max_profit_buy=net_before_buy-min_profit
            # ROI denominator includes acquisition + inbound + prep.
            max_roi_buy=(net_before_buy-(min_roi/100)*((inbound_cost or 0)+prep_cost))/(1+min_roi/100)
            maxbuy=round(max(0,min(max_cost,max_profit_buy,max_roi_buy)),2)
        else:
            maxbuy=None

        fee_known=fb is not None and fb['fee'] is not None and fb['weight_lb'] is not None
        seasonal_type=(b.get('Demand Quality')=='SEASONAL' and b.get('Seasonal V2') is True and b.get('Seasonal Confidence') in ('MEDIUM','HIGH'))
        recent_demand_ok=(b.get('Demand Quality') in ('STRONG','MODERATE') or (b['Rank Drops 90d']>=3 and b.get('Days Since Last Drop') is not None and b.get('Days Since Last Drop')<=90))
        normal_ok=fee_known and valuation is not None and valuation>=min_intrinsic and buy is not None and buy<=maxbuy and profit>=min_profit and roi>=min_roi and recent_demand_ok and not seasonal_type and (supported is not None or b['Rank Drops 90d']>=5)
        seasonal_ok=sfb is not None and sfb['fee'] is not None and seasonal_net_value is not None and buy is not None and b.get('Seasonal Years',0)>=2 and b.get('Seasonal Confidence') in ('MEDIUM','HIGH') and seasonal_maxbuy is not None and buy<=seasonal_maxbuy and sprofit>=min_profit and sroi>=min_roi
        seasonal_watch=sfb is not None and seasonal_net_value is not None and b.get('Seasonal Years',0)>=2 and b.get('Seasonal Confidence') in ('MEDIUM','HIGH') and seasonal_maxbuy is not None and buy>seasonal_maxbuy
        action='🟢 BUY' if normal_ok else ('🟠 SEASONAL WATCH' if seasonal_ok else ('🟠 SEASONAL WATCH' if seasonal_watch else ('🔴 PASS' if valuation is not None and (valuation<min_intrinsic or (maxbuy is not None and buy is not None and buy>maxbuy*1.5)) else '🟡 ANALYZE')))
        reason=decision_reason(action,buy,valuation,maxbuy,b['Rank Drops 90d'],b['Used Offers'])
        if action=='🟠 SEASONAL WATCH':
            if seasonal_ok:
                reason=f"Immediate flip does not pass, but seasonal economics do: buy ${buy:.2f} ≤ seasonal max ${seasonal_maxbuy:.2f}; projected seasonal profit ${sprofit:.2f}, ROI {sroi:.0f}%."
            else:
                reason=f"Recurring seasonal pattern detected; current buy ${buy:.2f} is above seasonal max ${seasonal_maxbuy:.2f}. Watch for a lower entry price."
        if not seasonal_type and b['Rank Drops 90d']==0 and b.get('Demand Quality')=='WEAK':
            action='🔴 PASS'; reason='No meaningful sales-rank drops or sustained low-rank demand in the last 90 days.'
        elif not seasonal_type and b['Rank Drops 90d']<3 and b.get('Demand Quality') not in ('STRONG','MODERATE') and action!='🔴 PASS':
            action='🟡 ANALYZE'; reason=f"Only {b['Rank Drops 90d']} meaningful rank drop(s) in 90 days; minimum 3 required for BUY."
        if action!='🟠 SEASONAL WATCH' and new_market_price is not None and b.get('Current Used ref ($)') is not None and b.get('Current Used ref ($)') >= new_market_price:
            action='🔴 PASS'; reason=f"New-price ceiling: current Used ${b.get('Current Used ref ($)'):.2f} is at/above New/Amazon ${new_market_price:.2f}."
        # Rising seller count is competition risk, not evidence of weak demand.
        # Do not turn a profitable candidate into PASS solely for this; require manual review.
        if action=='🟢 BUY' and b.get('Supply Momentum')=='⬆️ RISING FAST' and (b.get('Used Offers') or 0)>=8:
            action='🟡 ANALYZE'; reason='Economics pass, but Used seller supply is rising quickly; verify competition before buying.'
        if not fee_known:
            action='🟡 ANALYZE' if action!='🔴 PASS' else action
            reason='FBA fee/weight could not be calculated reliably; verify in RevSeller/Amazon calculator.'
        elif supported is None and action=='🟢 BUY':
            action='🟡 ANALYZE'; reason='Old intrinsic model passes, but demand-supported value is not established yet.'
        elif supported is not None and action not in ('🟢 BUY','🟠 SEASONAL WATCH') and b.get('Demand Value Reason'):
            reason=b['Demand Value Reason'] if supported < (floor or supported)*.70 else reason

        clean={k:v for k,v in b.items() if k!='_product'}
        results.append({**clean,'Buy Cost SHIPPED ($)':buy,'Weight (lb)':round(fb['weight_lb'],2) if fb and fb['weight_lb'] is not None else None,
            'FBA Size Tier':fb['tier'] if fb else 'UNKNOWN','FBA Peak?':fb['peak'] if fb else is_peak_fba_date(),
            'Referral 15% ($)':fb['referral'] if fb else None,'Closing Fee ($)':fb['closing'] if fb else None,
            'FBA Fulfillment ($)':fb['fee'] if fb else None,'Inbound+Ship to FBA ($)':inbound_cost,'Prep ($)':prep_cost,
            'Amazon Fees ($)':amazon_fees,'Total Fees Before Buy ($)':total_before_buy,
            'New Market Price ($)':new_market_price,'Used Value Ceiling ($)':used_value_ceiling,'Effective Valuation ($)':valuation,'Max Buy ($)':maxbuy,'Profit @ Supported Value ($)':profit,'ROI (%)':roi,
            'Seasonal Max Buy ($)':seasonal_maxbuy,'Seasonal Profit ($)':sprofit,'Seasonal ROI (%)':sroi,
            'Seasonal Net Value ($)':seasonal_net_value,'Months to Peak':months_to_peak,
            'Decision':action,'Reason':reason})

    st.subheader('3 · Quick-glance sourcing board')
    st.caption('⚫ = generic Used price · 🟠 = Amazon price · 🟢 = sales rank. Supply Meter is a heuristic. Supply Momentum tracks seller-count direction; rising supply means more competition, not weaker demand. Sales rank is displayed in Keepa orientation: lower/better rank is lower on the chart.')
    histories=st.session_state.get('histories_v5',{})
    order={'🟢 BUY':0,'🟠 SEASONAL WATCH':1,'🟡 ANALYZE':2,'🔴 PASS':3}
    results=sorted(results,key=lambda x:(order.get(x['Decision'],9),-(x['Profit @ Supported Value ($)'] or -999)))
    for r in results:
        stored_status=statuses.get(r['ASIN'],{}).get('status','NEW')
        current_status='NEW' if stored_status=='Analyzed' else stored_status
        if view_filter!='All' and view_filter!='New only' and current_status.upper()!=view_filter.upper():continue
        if view_filter=='New only' and current_status!='NEW':continue
        with st.container(border=True):
            left,graph,metrics,decision=st.columns([2.2,3.1,3.0,1.8])
            with left:
                st.markdown(f"**{r['Title'][:110]}**")
                st.caption(f"ASIN: `{r['ASIN']}` · {r['Book Type']} · {r['Source']}")
                if r.get('Price Signal'): st.caption(r['Price Signal'])
                if r.get('Seasonal Value ($)') is not None: st.caption(f"🎓 Seasonal: ${r['Seasonal Value ($)']:.2f} · peak {r['Peak']} · {r['Seasonal Years']} prior years")
                st.markdown(f"[Keepa]({r['Keepa']}) · [Amazon]({r['Amazon']})")
            with graph:
                p,amz,rk=histories.get(r['ASIN'],(pd.Series(dtype=float),pd.Series(dtype=float),pd.Series(dtype=float)))
                fig=mini_chart(p,amz,rk,chart_days,f'{chart_days}d Used + Amazon price + sales rank')
                if fig is not None:st.pyplot(fig,use_container_width=True); plt.close(fig)
            with metrics:
                a,b,c=st.columns(3)
                a.metric('Buy shipped',f"${r['Buy Cost SHIPPED ($)']:.2f}" if r['Buy Cost SHIPPED ($)'] is not None else '—')
                b.metric('Max buy',f"${r['Max Buy ($)']:.2f}" if r['Max Buy ($)'] is not None else '—')
                c.metric('Effective Used value',f"${r['Effective Valuation ($)']:.2f}" if r.get('Effective Valuation ($)') is not None else '—')
                d,e,f=st.columns(3)
                d.metric('Historical demand',f"${r['Demand-Supported Value ($)']:.2f}" if r.get('Demand-Supported Value ($)') is not None else '—')
                e.metric('New/Amazon',f"${r['New Market Price ($)']:.2f}" if r.get('New Market Price ($)') is not None else '—')
                f.metric('Used ceiling',f"${r['Used Value Ceiling ($)']:.2f}" if r.get('Used Value Ceiling ($)') is not None else '—')
                st.caption(r.get('Demand Value Reason',''))
                d,e,f=st.columns(3)
                d.metric('365 median',f"${r['365d Median ($)']:.2f}" if r['365d Median ($)'] is not None else '—')
                e.metric('Drops 90d',r['Rank Drops 90d'])
                f.metric('Used offers',r['Used Offers'] if r['Used Offers'] is not None else '—')
                last_txt='—' if r.get('Days Since Last Drop') is None else f"{r['Days Since Last Drop']}d"
                st.caption(f"Demand: **{r.get('Demand Quality','—')}** · Supply momentum: **{r.get('Supply Momentum','—')}** · History confidence: **{r.get('History Confidence','—')}** ({r.get('History Days',0)}d) · Last meaningful rank drop: **{last_txt} ago**")
                trend_pct='' if r.get('Amazon Trend %') is None else f" ({r.get('Amazon Trend %'):+.0f}% / 90d)"
                st.caption(f"Amazon/New price trend: **{r.get('Amazon Price Trend','NO DATA')}**{trend_pct}")
                if r.get('Seasonal V2'):
                    month_name=pd.Timestamp(2000,int(r['Seasonal Peak Month']),1).strftime('%b') if r.get('Seasonal Peak Month') else '—'
                    sval='—' if r.get('Seasonal Value V2') is None else f"${r['Seasonal Value V2']:.2f}"
                    st.caption(f"🎓 Seasonal V2: **{r.get('Seasonal Confidence','—')}** · peak **{month_name}** · {r.get('Seasonal Cycles',0)} cycle(s)/yr · seasonal value **{sval}**")
                    smx='—' if r.get('Seasonal Max Buy ($)') is None else f"${r['Seasonal Max Buy ($)']:.2f}"
                    spr='—' if r.get('Seasonal Profit ($)') is None else f"${r['Seasonal Profit ($)']:.2f}"
                    sroi_txt='—' if r.get('Seasonal ROI (%)') is None else f"{r['Seasonal ROI (%)']:.0f}%"
                    wait='—' if r.get('Months to Peak') is None else ('now' if r['Months to Peak']==0 else f"{r['Months to Peak']} mo")
                    st.caption(f"**Seasonal Max Buy: {smx}** · seasonal profit {spr} · ROI {sroi_txt} · next peak {wait}")
                    st.caption(r.get('Seasonal Evidence',''))
                st.write(f"**Supply:** {r['Supply Meter']} · **Profit:** ${r['Profit @ Supported Value ($)']:.2f}" if r['Profit @ Supported Value ($)'] is not None else f"**Supply:** {r['Supply Meter']}")
                if r.get('FBA Fulfillment ($)') is not None:
                    st.caption(f"Fees: referral ${r['Referral 15% ($)']:.2f} + closing ${r['Closing Fee ($)']:.2f} + FBA ${r['FBA Fulfillment ($)']:.2f} + inbound/ship ${r['Inbound+Ship to FBA ($)']:.2f} + prep ${r['Prep ($)']:.2f} · wt {r['Weight (lb)']:.2f} lb")
                else:
                    st.caption('FBA fee unavailable — verify weight/size in RevSeller.')
                if r.get('Total Amazon Fees ($)') is not None:
                    st.caption(f"Amazon fees est.: ${r['Total Amazon Fees ($)']:.2f} = referral ${r['Amazon Referral Fee ($)']:.2f} + closing $1.80 + FBA ${r['FBA Fulfillment Fee ($)']:.2f}")
                else:
                    st.caption('Amazon fees: enter FBA fulfillment fee to calculate profit/max buy.')
                if r.get('Recovery Cycles',0)>0: st.caption(f"Price recovery cycles detected: {r['Recovery Cycles']}")
            with decision:
                st.markdown(f"### {r['Decision']}")
                st.caption(r['Reason'])
                st.caption(f"Saved status: **{current_status}**")
                c1,c2=st.columns(2)
                if c1.button('👁 Reviewed',key=f"rev_{r['ASIN']}"):
                    save_status(r['ASIN'],'Reviewed',r['Title'],f"Scanner: {r['Decision']} — {r['Reason']}"); st.rerun()
                if c2.button('⭐ Watch',key=f"wat_{r['ASIN']}"):
                    save_status(r['ASIN'],'Watch',r['Title'],f"Scanner: {r['Decision']} — {r['Reason']}"); st.rerun()
                c3,c4=st.columns(2)
                if c3.button('🟢 Buy',key=f"buy_{r['ASIN']}"):
                    save_status(r['ASIN'],'Buy',r['Title'],f"Scanner: {r['Decision']} — {r['Reason']}"); st.rerun()
                if c4.button('🔴 Pass',key=f"pas_{r['ASIN']}"):
                    save_status(r['ASIN'],'Pass',r['Title'],f"Scanner: {r['Decision']} — {r['Reason']}"); st.rerun()

    out=pd.DataFrame(results)
    st.download_button('Download V7.7 results CSV',out.to_csv(index=False).encode(),'bookfinder_v7_7_results.csv','text/csv')

st.divider(); st.subheader('Saved ASIN history')
statuses=load_statuses()
if statuses:
    hist=pd.DataFrame(list(statuses.values()))
    if 'status' in hist.columns:
        hist['decision_type']=hist['status'].apply(lambda x:'Automatic analyzed marker' if x=='Analyzed' else 'Your decision')
    st.dataframe(hist,use_container_width=True,hide_index=True)
else:st.caption('No ASINs marked yet.')

st.info('V7.7 decisions are screening aids. Analyzed ASINs are persisted immediately after Keepa returns product data. Demand-supported value uses rank drops as a demand proxy, not confirmed sales. Verify exact edition, condition, shipped checkout cost, restrictions and Amazon fees in Amazon Revenue Calculator before purchasing. Recent Drops uses Keepa Deals; Value/Seasonal analysis uses product history. V7.7 avoids requesting full marketplace offers on every book because those calls cost additional tokens.')
