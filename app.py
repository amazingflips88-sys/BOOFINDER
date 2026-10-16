import os, json, re
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
import streamlit as st
import matplotlib.pyplot as plt

st.set_page_config(page_title='Book Finder V4', page_icon='📚', layout='wide')
st.title('📚 Private Used-Book Finder — V4')
st.caption('Fast sourcing dashboard: shipped buy cost + conservative Used value + demand + supply + mini Keepa graph + review history.')

KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)

# Keepa Product.csv / stats.current indexes used here.
USED, SALES, COUNT_USED = 2, 3, 12

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
    return st.session_state.setdefault('local_statuses_v4', {})

def save_status(asin, status, title='', reason=''):
    row={'asin':asin,'status':status,'title':title[:500],'reason':reason[:500],'updated_at':datetime.now(timezone.utc).isoformat()}
    if db_enabled():
        r=requests.post(f'{supa_url}/rest/v1/book_status?on_conflict=asin', headers={**db_headers(),'Prefer':'resolution=merge-duplicates,return=minimal'}, data=json.dumps(row), timeout=20)
        r.raise_for_status(); load_statuses_remote.clear()
    else:
        st.session_state.setdefault('local_statuses_v4', {})[asin]=row

statuses=load_statuses()

with st.sidebar:
    st.header('Buying rules')
    max_cost = st.number_input('Maximum shipped buy cost ($)', 1., 500., 20.)
    min_intrinsic = st.number_input('Minimum intrinsic Used value ($)', 1., 500., 20.)
    min_profit = st.number_input('Minimum estimated profit ($)', 0., 100., 5.)
    min_roi = st.number_input('Minimum ROI (%)', 0, 1000, 50)
    extra_cost = st.number_input('Inbound shipping + prep ($)', 0., 30., 2.)
    fee_est = st.number_input('Estimated Amazon fees ($) — placeholder', 0., 50., 8.)
    max_rank = st.number_input('Maximum current sales rank', 1000, 10000000, 2000000, step=100000)
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

def classify_book(title):
    t=(title or '').lower()
    if any(x in t for x in ['textbook','edition','calculus','algebra','anatomy','chemistry','biology','organizational behavior','economics','accounting']):return 'College/Textbook'
    if any(x in t for x in ['sat ','act ','gre ','gmat','mcat','nclex','test prep','study guide']):return 'Test Prep'
    if any(x in t for x in ['christmas','halloween','thanksgiving','easter','valentine']):return 'Holiday/Seasonal'
    if any(x in t for x in ['manual','handbook','certification','nursing','medical','engineering','law ']):return 'Professional/Technical'
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

def decision_reason(action,buy,floor,maxbuy,drops,offers):
    if action=='🟢 BUY':return 'Passes conservative value, profit and ROI rules.'
    if action=='🟠 BUY & HOLD':return 'Recurring seasonal value passes your rules.'
    if floor is None:return 'Not enough usable Used-price history.'
    if buy is not None and maxbuy is not None and buy>maxbuy:return f'Buy cost is ${buy-maxbuy:.2f} above max buy.'
    if drops<=1:return 'Demand signal is weak; inspect Keepa before deciding.'
    if offers is not None and supply_meter(offers,drops).endswith('HIGH'):return 'Supply is high relative to recent demand.'
    return 'Needs manual review.'

st.subheader('1 · Mine new Used-book opportunities')
st.write('Discovery uses generic **Used** pricing. Books you marked Reviewed/Buy/Watch/Pass are removed from the **New only** candidate list before detailed analysis, saving repeat detail calls.')
st.caption('Keepa generic marketplace prices do not necessarily include shipping. The editable Buy Cost below is the delivered/shipped cost you verify before buying. Keepa documents marketplace price and offer data separately. citeturn0search2turn1search5')

if st.button('🔎 Discover up to 50 Used opportunities',disabled=not bool(key),type='primary'):
    try:
        with st.spinner('Searching Keepa…'):response=discover(key,max_rank,max_cost,min_intrinsic)
        raw=response.get('asinList',[]); known=set(statuses)
        new=[a for a in raw if a not in known]
        st.session_state['asins_v4_raw']=raw; st.session_state['asins_v4']=new
        st.success(f'Keepa returned {len(raw)} candidates; {len(new)} are new to you. Tokens left: {response.get("tokensLeft","unknown")}')
    except Exception as e:st.error(f'Keepa discovery failed: {e}')

asins=st.session_state.get('asins_v4',[])
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
                floor=intrinsic_floor(used_hist,floor_days,low_band); season=seasonal_model(used_hist); drops90=rank_drop_count(rank_hist,90)
                x365=used_hist[used_hist.index>=used_hist.index.max()-pd.Timedelta(days=365)] if not used_hist.empty else pd.Series(dtype=float)
                med=round(float(x365.median()),2) if not x365.empty else None
                histories[asin]=(used_hist,rank_hist)
                rows.append({'ASIN':asin,'Title':title,'Book Type':classify_book(title),'Rank':rank,'Current Used ref ($)':used,'Buy Cost SHIPPED ($)':used,'Intrinsic ($)':floor,'365d Median ($)':med,'Rank Drops 90d':drops90,'Used Offers':offers,'Supply Meter':supply_meter(offers,drops90),'Seasonal Value ($)':season['value'] if season else None,'Peak':month_name(season['month']) if season else '','Seasonal Years':season['years'] if season else 0,'Seasonal Confidence':season['confidence'] if season else 'None','Amazon':f'https://www.amazon.com/dp/{asin}','Keepa':f'https://keepa.com/#!product/1-{asin}'})
            st.session_state['books_v4']=rows; st.session_state['histories_v4']=histories
            st.success(f'Analyzed {len(rows)} books. Tokens remaining: {response.get("tokensLeft","unknown")}')
        except Exception as e:st.error(f'Keepa details failed: {e}')

books=st.session_state.get('books_v4',[])
if books:
    st.subheader('2 · Verify shipped buy cost')
    df=pd.DataFrame(books)
    st.caption('IMPORTANT: Current Used is a Keepa market reference. Edit **Buy Cost SHIPPED** to the actual item + shipping you can purchase for.')
    edited=st.data_editor(df[['ASIN','Title','Buy Cost SHIPPED ($)','Current Used ref ($)','Intrinsic ($)','365d Median ($)','Rank Drops 90d','Used Offers','Supply Meter']],hide_index=True,use_container_width=True,disabled=[c for c in ['ASIN','Title','Current Used ref ($)','Intrinsic ($)','365d Median ($)','Rank Drops 90d','Used Offers','Supply Meter']])
    buy_map=dict(zip(edited['ASIN'],edited['Buy Cost SHIPPED ($)']))

    results=[]
    for b in books:
        buy=buy_map.get(b['ASIN']); floor=b['Intrinsic ($)']; seasonal=b['Seasonal Value ($)']
        buy=float(buy) if pd.notna(buy) else None
        profit=round(floor-buy-fee_est-extra_cost,2) if buy is not None and floor is not None else None
        roi=round(100*profit/(buy+extra_cost),1) if profit is not None and buy+extra_cost>0 else None
        sprofit=round(seasonal-buy-fee_est-extra_cost,2) if buy is not None and seasonal is not None else None
        sroi=round(100*sprofit/(buy+extra_cost),1) if sprofit is not None and buy+extra_cost>0 else None
        # max buy constrained by both min profit and min ROI
        max_profit_buy=floor-fee_est-extra_cost-min_profit if floor is not None else None
        max_roi_buy=(floor-fee_est-extra_cost)/(1+min_roi/100) if floor is not None else None
        maxbuy=round(max(0,min(max_cost,max_profit_buy,max_roi_buy)),2) if max_profit_buy is not None else None
        normal_ok=floor is not None and floor>=min_intrinsic and buy is not None and buy<=maxbuy and profit>=min_profit and roi>=min_roi and b['Rank Drops 90d']>=2
        seasonal_ok=seasonal is not None and buy is not None and b['Seasonal Years']>=2 and sprofit>=min_profit and sroi>=min_roi
        action='🟢 BUY' if normal_ok else ('🟠 BUY & HOLD' if seasonal_ok else ('🔴 PASS' if floor is not None and (floor<min_intrinsic or (maxbuy is not None and buy is not None and buy>maxbuy*1.5)) else '🟡 ANALYZE'))
        reason=decision_reason(action,buy,floor,maxbuy,b['Rank Drops 90d'],b['Used Offers'])
        results.append({**b,'Buy Cost SHIPPED ($)':buy,'Max Buy ($)':maxbuy,'Profit @ Intrinsic ($)':profit,'ROI (%)':roi,'Decision':action,'Reason':reason})

    st.subheader('3 · Quick-glance sourcing board')
    st.caption('⚫ mini graph = generic Used price. 🟢 = sales rank. Supply Meter combines current Used offer count with recent rank-drop activity. It is a heuristic, not a guarantee of unit sales.')
    histories=st.session_state.get('histories_v4',{})
    order={'🟢 BUY':0,'🟠 BUY & HOLD':1,'🟡 ANALYZE':2,'🔴 PASS':3}
    results=sorted(results,key=lambda x:(order.get(x['Decision'],9),-(x['Profit @ Intrinsic ($)'] or -999)))
    for r in results:
        current_status=statuses.get(r['ASIN'],{}).get('status','NEW')
        if view_filter!='All' and view_filter!='New only' and current_status.upper()!=view_filter.upper():continue
        if view_filter=='New only' and current_status!='NEW':continue
        with st.container(border=True):
            left,graph,metrics,decision=st.columns([2.2,3.1,3.0,1.8])
            with left:
                st.markdown(f"**{r['Title'][:110]}**")
                st.caption(f"ASIN: `{r['ASIN']}` · {r['Book Type']}")
                st.markdown(f"[Keepa]({r['Keepa']}) · [Amazon]({r['Amazon']})")
            with graph:
                p,rk=histories.get(r['ASIN'],(pd.Series(dtype=float),pd.Series(dtype=float)))
                fig=mini_chart(p,rk,chart_days,f'{chart_days}d Used price + sales rank')
                if fig is not None:st.pyplot(fig,use_container_width=True); plt.close(fig)
            with metrics:
                a,b,c=st.columns(3)
                a.metric('Buy shipped',f"${r['Buy Cost SHIPPED ($)']:.2f}" if r['Buy Cost SHIPPED ($)'] is not None else '—')
                b.metric('Max buy',f"${r['Max Buy ($)']:.2f}" if r['Max Buy ($)'] is not None else '—')
                c.metric('Intrinsic',f"${r['Intrinsic ($)']:.2f}" if r['Intrinsic ($)'] is not None else '—')
                d,e,f=st.columns(3)
                d.metric('365 median',f"${r['365d Median ($)']:.2f}" if r['365d Median ($)'] is not None else '—')
                e.metric('Drops 90d',r['Rank Drops 90d'])
                f.metric('Used offers',r['Used Offers'] if r['Used Offers'] is not None else '—')
                st.write(f"**Supply:** {r['Supply Meter']} · **Profit:** ${r['Profit @ Intrinsic ($)']:.2f}" if r['Profit @ Intrinsic ($)'] is not None else f"**Supply:** {r['Supply Meter']}")
            with decision:
                st.markdown(f"### {r['Decision']}")
                st.caption(r['Reason'])
                st.caption(f"Saved status: **{current_status}**")
                c1,c2=st.columns(2)
                if c1.button('👁 Reviewed',key=f"rev_{r['ASIN']}"):
                    save_status(r['ASIN'],'Reviewed',r['Title'],r['Reason']); st.rerun()
                if c2.button('⭐ Watch',key=f"wat_{r['ASIN']}"):
                    save_status(r['ASIN'],'Watch',r['Title'],r['Reason']); st.rerun()
                c3,c4=st.columns(2)
                if c3.button('🟢 Buy',key=f"buy_{r['ASIN']}"):
                    save_status(r['ASIN'],'Buy',r['Title'],r['Reason']); st.rerun()
                if c4.button('🔴 Pass',key=f"pas_{r['ASIN']}"):
                    save_status(r['ASIN'],'Pass',r['Title'],r['Reason']); st.rerun()

    out=pd.DataFrame(results)
    st.download_button('Download V4 results CSV',out.to_csv(index=False).encode(),'bookfinder_v4_results.csv','text/csv')

st.divider(); st.subheader('Saved ASIN history')
statuses=load_statuses()
if statuses:
    hist=pd.DataFrame(list(statuses.values()))
    st.dataframe(hist,use_container_width=True,hide_index=True)
else:st.caption('No ASINs marked yet.')

st.info('V4 decisions are screening aids. Verify exact edition, condition, shipped checkout cost, restrictions and Amazon fees before purchasing. Keepa states product requests provide price history and optional marketplace offer data; offer requests can consume additional tokens. citeturn1search2turn1search5')
