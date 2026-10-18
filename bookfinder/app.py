import os, json, re
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
import streamlit as st
import matplotlib.pyplot as plt

st.set_page_config(page_title='Book Finder V6.2.2', page_icon='📚', layout='wide')
st.title('📚 Private Used-Book Finder — V6.2.2')
st.caption('A2A sourcing: demand-supported value + Amazon book fee engine + recent Used drops + seasonal value + demand/supply + review history.')

KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)

# Keepa Product.csv / stats.current indexes used here.
USED, SALES, COUNT_USED = 2, 3, 12
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

statuses=load_statuses()

with st.sidebar:
    st.header('Buying rules')
    max_cost = st.number_input('Maximum shipped buy cost ($)', 1., 500., 20.)
    min_intrinsic = st.number_input('Minimum intrinsic Used value ($)', 1., 500., 20.)
    min_profit = st.number_input('Minimum estimated profit ($)', 0., 100., 5.)
    min_roi = st.number_input('Minimum ROI (%)', 0, 1000, 50)
    extra_cost = st.number_input('Inbound shipping + prep ($)', 0., 30., 2.)
    st.caption('Amazon fees are now calculated per book: 15% referral + $1.80 media closing fee + estimated FBA fulfillment. You can edit the FBA fee per candidate before buying.')
    max_rank = st.number_input('Maximum current sales rank', 1000, 10000000, 2000000, step=100000)
    st.divider(); st.header('Mining mode')
    mining_mode = st.radio('Discovery engine', ['🔎 Value Mine','⚡ Recent Used Drops','🎓 Seasonal Mine'], index=0)
    deal_window = st.selectbox('Recent-drop window', [('24 hours',0),('7 days',1),('31 days',2),('90 days',3)], index=1, format_func=lambda x:x[0])
    min_drop_pct = st.slider('Minimum recent price drop (%)', 10, 80, 30, 5)
    st.caption('Recent Drops uses Keepa Deals first, then spends detail-history calls only on the candidates you choose to analyze.')
    st.divider(); st.header('Value model')
    floor_days = st.selectbox('Intrinsic-value lookback', [90,180,365,730], index=2)
    low_band = st.slider('Low-price band used for intrinsic value',10,50,25,5)
    chart_days = st.selectbox('Mini graph period',[90,365,730],index=1,format_func=lambda x:'90 days' if x==90 else ('1 year' if x==365 else '2 years'))
    st.divider(); st.header('History')
    view_filter=st.selectbox('Show books marked as',['New only','Watch','Buy','Reviewed','Pass','All'],index=0)
    if db_enabled(): st.success('Persistent ASIN history: ON')
    else: st.warning('Persistent ASIN history: session only. Add Supabase later so marks survive app restarts.')

@st.cache_data(ttl=3600, show_spinner=False)
def discover(api_key,max_rank,max_cost,min_hist_value):
    selection={'rootCategory':[283155],'productType':0,'current_SALES_lte':int(max_rank),'current_USED_lte':int(max_cost*100),'avg90_USED_gte':int(min_hist_value*100),'perPage':50,'page':0,'sort':[['current_USED','asc']]}
    r=requests.get('https://api.keepa.com/query',params={'key':api_key,'domain':1,'selection':json.dumps(selection,separators=(',',':'))},timeout=45)
    r.raise_for_status(); data=r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

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
            'high':round(float(np.quantile(a,.75)),2)}

def supported_value_reason(ds, floor, med):
    if not ds:
        return 'Insufficient rank-drop history to establish demand-supported value.'
    v=ds['value']; events=ds['events']
    if med and med>0 and v<med*.55:
        return f'Historical asking prices look inflated versus demand: supported value ${v:.2f} from {events} rank-drop events.'
    if floor and floor>0 and v<floor*.70:
        return f'Demand occurred at materially lower prices than the old intrinsic model: ${v:.2f} from {events} events.'
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
            intervals=math.ceil(max(0,shipping_lb-3)*4)
            return base + 0.08*intervals
    return None

def is_peak_fba_date(d=None):
    d=d or datetime.now().date()
    # 2026 holiday peak: Oct 15, 2026 through Jan 14, 2027.
    return d>=datetime(2026,10,15).date() and d<=datetime(2027,1,14).date()

def fba_fulfillment_fee(p,sell_price):
    wt=product_weight_lb(p); dims=product_dimensions_in(p)
    tier,ship_wt=fba_size_tier(wt,dims)
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


def classify_book(title):
    t=(title or '').lower()
    if any(x in t for x in ['textbook','edition','calculus','algebra','anatomy','chemistry','biology','organizational behavior','economics','accounting']):return 'College/Textbook'
    if any(x in t for x in ['sat ','act ','gre ','gmat','mcat','nclex','test prep','study guide']):return 'Test Prep'
    if any(x in t for x in ['christmas','halloween','thanksgiving','easter','valentine','advent','hanukkah']):return 'Holiday/Seasonal'
    if any(x in t for x in ['ap ','common core','grade ','teacher edition','student edition','workbook']):return 'K-12/Education'
    if any(x in t for x in ['manual','handbook','certification','nursing','medical','engineering','law ','coding','programming']):return 'Professional/Technical'
    if any(x in t for x in ['travel guide','national park','road atlas','cruise']):return 'Travel/Seasonal'
    return 'General/All-Year'

def month_name(m):return datetime(2000,m,1).strftime('%b') if m else ''

def mini_chart(price_s,rank_s,days,title):
    ends=[x.index.max() for x in (price_s,rank_s) if not x.empty]
    if not ends:return None
    end=max(ends); start=end-pd.Timedelta(days=days); p=price_s[price_s.index>=start]; r=rank_s[rank_s.index>=start]
    fig,ax1=plt.subplots(figsize=(5.2,2.0))
    if not p.empty:ax1.plot(p.index,p.values,color='black',linewidth=1.35)
    ax1.set_ylabel('Used $',fontsize=7); ax1.grid(alpha=.18); ax1.tick_params(axis='both',labelsize=7)
    ax2=ax1.twinx()
    if not r.empty:ax2.plot(r.index,r.values,color='green',linewidth=.8,alpha=.8); ax2.invert_yaxis()
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
st.write('Choose **Value Mine**, **Recent Used Drops**, or **Seasonal Mine**. Previously marked ASINs are removed before detailed analysis.')
st.caption('V6 keeps generic USED history visible, but BUY economics use demand-supported value when enough rank-drop evidence exists. When Keepa exposes a shipping-inclusive Used condition/Used Buy Box reference, V5 uses it as the starting shipped-cost estimate; always verify checkout.')

button_label = '⚡ Find recent Used price drops' if mining_mode.startswith('⚡') else ('🎓 Find seasonal candidates' if mining_mode.startswith('🎓') else '🔎 Discover value opportunities')
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
        else:
            with st.spinner('Searching Keepa…'):
                response=discover(key,max_rank,max_cost,min_intrinsic)
            raw=response.get('asinList',[])
            st.session_state['deal_meta_v5']={a:{'source':'Seasonal Mine' if mining_mode.startswith('🎓') else 'Value Mine'} for a in raw}
        known=set(statuses)
        new=[a for a in raw if a not in known]
        st.session_state['asins_v5_raw']=raw; st.session_state['asins_v5']=new
        st.success(f'Keepa returned {len(raw)} candidates; {len(new)} are new to you. Tokens left: {response.get("tokensLeft","unknown")}')
    except Exception as e:st.error(f'Keepa discovery failed: {e}')

asins=st.session_state.get('asins_v5',[])
if asins:
    n=st.slider('New books to analyze now',1,min(20,len(asins)),min(5,len(asins)))
    st.caption('Start small. Product detail history is cached for 24 hours.')
    if st.button(f'Analyze first {n} new books'):
        try:
            with st.spinner('Analyzing Used history, rank drops and supply…'):response=details(key,asins[:n])
            rows=[]; histories={}
            for p in response.get('products',[]):
                current=p.get('stats',{}).get('current',[]); csv=p.get('csv',[]); asin=p.get('asin'); title=p.get('title','')
                used=at_price(current,USED); rank=at_raw(current,SALES); offers=int(at_raw(current,COUNT_USED)) if at_raw(current,COUNT_USED) is not None else None
                used_hist=keepa_series(csv[USED],100.0) if isinstance(csv,list) and len(csv)>USED else pd.Series(dtype=float)
                rank_hist=keepa_series(csv[SALES],1.0) if isinstance(csv,list) and len(csv)>SALES else pd.Series(dtype=float)
                offer_hist=keepa_series(csv[COUNT_USED],1.0) if isinstance(csv,list) and len(csv)>COUNT_USED else pd.Series(dtype=float)
                floor=intrinsic_floor(used_hist,floor_days,low_band); ds=demand_supported_value(used_hist,rank_hist,max(730,floor_days)); season=seasonal_model(used_hist); drops90=rank_drop_count(rank_hist,90)
                shipped_ref=shipped_used_reference(current); strend,offers90,offers_now=supply_trend(offer_hist); recoveries=recovery_cycles(used_hist)
                x365=used_hist[used_hist.index>=used_hist.index.max()-pd.Timedelta(days=365)] if not used_hist.empty else pd.Series(dtype=float)
                med=round(float(x365.median()),2) if not x365.empty else None
                histories[asin]=(used_hist,rank_hist)
                dealmeta=st.session_state.get('deal_meta_v5',{}).get(asin,{})
                source=dealmeta.get('source','Value Mine')
                drop_pct=dealmeta.get('drop_pct')
                crash=price_crash_signal(used,med,floor)
                rows.append({'ASIN':asin,'Title':title,'Source':source,'Recent Drop %':drop_pct,'Book Type':classify_book(title),'Rank':rank,
                    'Current Used ref ($)':used,'Buy Cost SHIPPED ($)':shipped_ref if shipped_ref is not None else used,
                    'Shipped Ref Available':shipped_ref is not None,'FBA Fulfillment Fee ($)':keepa_fba_fulfillment_fee(p),'Intrinsic ($)':floor,'Demand-Supported Value ($)':ds['value'] if ds else None,'Demand Events':ds['events'] if ds else 0,'Demand-Supported Median ($)':ds['median'] if ds else None,'Demand Value Reason':supported_value_reason(ds,floor,med),'365d Median ($)':med,
                    'Rank Drops 90d':drops90,'Used Offers':offers,'Supply Meter':supply_meter(offers,drops90),
                    'Supply Trend':strend,'Offers ~90d Ago':offers90,'Recovery Cycles':recoveries,'Price Signal':crash,
                    'Seasonal Value ($)':season['value'] if season else None,'Peak':month_name(season['month']) if season else '',
                    'Seasonal Years':season['years'] if season else 0,'Seasonal Confidence':season['confidence'] if season else 'None',
                    'Amazon':f'https://www.amazon.com/dp/{asin}','Keepa':f'https://keepa.com/#!product/1-{asin}','_product':p})
            if mining_mode.startswith('🎓'):
                rows=sorted(rows,key=lambda x:(0 if x['Seasonal Value ($)'] is not None else 1,-x['Seasonal Years']))
            st.session_state['books_v5']=rows; st.session_state['histories_v5']=histories
            st.success(f'Analyzed {len(rows)} books. Tokens remaining: {response.get("tokensLeft","unknown")}')
        except Exception as e:st.error(f'Keepa details failed: {e}')

books=st.session_state.get('books_v5',[])
if books:
    st.subheader('2 · Verify shipped buy cost')
    df=pd.DataFrame(books)
    st.caption("V6.1 pre-fills **Buy Cost SHIPPED** from Keepa when available. **FBA Fulfillment Fee** comes from Keepa product metadata when available; verify/edit it against Amazon's Revenue Calculator before purchasing. Referral (15%) and the $1.80 media closing fee are calculated automatically from the demand-supported sell value.")
    edited=st.data_editor(df[['ASIN','Title','Source','Buy Cost SHIPPED ($)','FBA Fulfillment Fee ($)','Current Used ref ($)','Intrinsic ($)','Demand-Supported Value ($)','Demand Events','365d Median ($)','Rank Drops 90d','Used Offers','Supply Meter','Supply Trend','Price Signal']],hide_index=True,use_container_width=True,disabled=[c for c in ['ASIN','Title','Source','Current Used ref ($)','Intrinsic ($)','Demand-Supported Value ($)','Demand Events','365d Median ($)','Rank Drops 90d','Used Offers','Supply Meter','Supply Trend','Price Signal']])
    buy_map=dict(zip(edited['ASIN'],edited['Buy Cost SHIPPED ($)']))
    fba_map=dict(zip(edited['ASIN'],edited['FBA Fulfillment Fee ($)']))

    results=[]
    for b in books:
        buy=buy_map.get(b['ASIN']); floor=b['Intrinsic ($)']; supported=b.get('Demand-Supported Value ($)'); seasonal=b['Seasonal Value ($)']
        valuation=supported if supported is not None else floor
        buy=float(buy) if pd.notna(buy) else None
        fb=fee_breakdown(b.get('_product',{}),valuation,inbound_per_lb,prep_cost) if valuation is not None else None

        amazon_fees=fb['amazon_fees'] if fb else None
        inbound_cost=fb['inbound'] if fb else None
        total_before_buy=fb['fees_before_buy'] if fb else None

        profit=round(valuation-buy-total_before_buy,2) if buy is not None and valuation is not None and total_before_buy is not None else None
        roi=round(100*profit/(buy+inbound_cost+prep_cost),1) if profit is not None and (buy+(inbound_cost or 0)+prep_cost)>0 else None

        sfb=fee_breakdown(b.get('_product',{}),seasonal,inbound_per_lb,prep_cost) if seasonal is not None else None
        sprofit=round(seasonal-buy-sfb['fees_before_buy'],2) if buy is not None and seasonal is not None and sfb and sfb['fees_before_buy'] is not None else None
        sroi=round(100*sprofit/(buy+(sfb['inbound'] or 0)+prep_cost),1) if sprofit is not None and (buy+(sfb['inbound'] or 0)+prep_cost)>0 else None

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
        normal_ok=fee_known and valuation is not None and valuation>=min_intrinsic and buy is not None and buy<=maxbuy and profit>=min_profit and roi>=min_roi and b['Rank Drops 90d']>=2 and (supported is not None or b['Rank Drops 90d']>=5)
        seasonal_ok=sfb is not None and sfb['fee'] is not None and seasonal is not None and buy is not None and b['Seasonal Years']>=2 and sprofit>=min_profit and sroi>=min_roi
        action='🟢 BUY' if normal_ok else ('🟡 ANALYZE' if seasonal_ok else ('🔴 PASS' if valuation is not None and (valuation<min_intrinsic or (maxbuy is not None and buy is not None and buy>maxbuy*1.5)) else '🟡 ANALYZE'))
        reason=decision_reason(action,buy,valuation,maxbuy,b['Rank Drops 90d'],b['Used Offers'])
        if not fee_known:
            action='🟡 ANALYZE' if action!='🔴 PASS' else action
            reason='FBA fee/weight could not be calculated reliably; verify in RevSeller/Amazon calculator.'
        elif supported is None and action=='🟢 BUY':
            action='🟡 ANALYZE'; reason='Old intrinsic model passes, but demand-supported value is not established yet.'
        elif supported is not None and action!='🟢 BUY' and b.get('Demand Value Reason'):
            reason=b['Demand Value Reason'] if supported < (floor or supported)*.70 else reason

        clean={k:v for k,v in b.items() if k!='_product'}
        results.append({**clean,'Buy Cost SHIPPED ($)':buy,'Weight (lb)':round(fb['weight_lb'],2) if fb and fb['weight_lb'] is not None else None,
            'FBA Size Tier':fb['tier'] if fb else 'UNKNOWN','FBA Peak?':fb['peak'] if fb else is_peak_fba_date(),
            'Referral 15% ($)':fb['referral'] if fb else None,'Closing Fee ($)':fb['closing'] if fb else None,
            'FBA Fulfillment ($)':fb['fee'] if fb else None,'Inbound+Ship to FBA ($)':inbound_cost,'Prep ($)':prep_cost,
            'Amazon Fees ($)':amazon_fees,'Total Fees Before Buy ($)':total_before_buy,
            'Max Buy ($)':maxbuy,'Profit @ Supported Value ($)':profit,'ROI (%)':roi,'Decision':action,'Reason':reason})

    st.subheader('3 · Quick-glance sourcing board')
    st.caption('⚫ mini graph = generic Used price. 🟢 = sales rank. Supply Meter combines current Used offer count with recent rank-drop activity. It is a heuristic, not a guarantee of unit sales.')
    histories=st.session_state.get('histories_v5',{})
    order={'🟢 BUY':0,'🟡 ANALYZE':2,'🔴 PASS':3}
    results=sorted(results,key=lambda x:(order.get(x['Decision'],9),-(x['Profit @ Supported Value ($)'] or -999)))
    for r in results:
        current_status=statuses.get(r['ASIN'],{}).get('status','NEW')
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
                p,rk=histories.get(r['ASIN'],(pd.Series(dtype=float),pd.Series(dtype=float)))
                fig=mini_chart(p,rk,chart_days,f'{chart_days}d Used price + sales rank')
                if fig is not None:st.pyplot(fig,use_container_width=True); plt.close(fig)
            with metrics:
                a,b,c=st.columns(3)
                a.metric('Buy shipped',f"${r['Buy Cost SHIPPED ($)']:.2f}" if r['Buy Cost SHIPPED ($)'] is not None else '—')
                b.metric('Max buy',f"${r['Max Buy ($)']:.2f}" if r['Max Buy ($)'] is not None else '—')
                c.metric('Demand-supported',f"${r['Demand-Supported Value ($)']:.2f}" if r['Demand-Supported Value ($)'] is not None else '—')
                d,e,f=st.columns(3)
                d.metric('Old intrinsic',f"${r['Intrinsic ($)']:.2f}" if r['Intrinsic ($)'] is not None else '—')
                st.caption(r.get('Demand Value Reason',''))
                d,e,f=st.columns(3)
                d.metric('365 median',f"${r['365d Median ($)']:.2f}" if r['365d Median ($)'] is not None else '—')
                e.metric('Drops 90d',r['Rank Drops 90d'])
                f.metric('Used offers',r['Used Offers'] if r['Used Offers'] is not None else '—')
                st.write(f"**Supply:** {r['Supply Meter']} · **Trend:** {r['Supply Trend']} · **Profit:** ${r['Profit @ Supported Value ($)']:.2f}" if r['Profit @ Supported Value ($)'] is not None else f"**Supply:** {r['Supply Meter']} · **Trend:** {r['Supply Trend']}")
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
    st.download_button('Download V6.1 results CSV',out.to_csv(index=False).encode(),'bookfinder_v6_1_results.csv','text/csv')

st.divider(); st.subheader('Saved ASIN history')
statuses=load_statuses()
if statuses:
    hist=pd.DataFrame(list(statuses.values()))
    st.dataframe(hist,use_container_width=True,hide_index=True)
else:st.caption('No ASINs marked yet.')

st.info('V6.1 decisions are screening aids. Demand-supported value uses rank drops as a demand proxy, not confirmed sales. Verify exact edition, condition, shipped checkout cost, restrictions and Amazon fees in Amazon Revenue Calculator before purchasing. Recent Drops uses Keepa Deals; Value/Seasonal analysis uses product history. V5 avoids requesting full marketplace offers on every book because those calls cost additional tokens.')
