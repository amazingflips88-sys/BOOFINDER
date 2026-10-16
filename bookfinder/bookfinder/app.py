import os, json
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
import streamlit as st
import matplotlib.pyplot as plt

st.set_page_config(page_title='Book Finder V3', page_icon='📚', layout='wide')
st.title('📚 Private Used-Book Finder — V3')
st.caption('Generic USED market value + intrinsic floor + recurring seasonality + sales-rank drops + mini Keepa-style charts.')

KEEPA_EPOCH = datetime(2011, 1, 1, tzinfo=timezone.utc)

def secret(name):
    try: return st.secrets.get(name, os.getenv(name, ''))
    except Exception: return os.getenv(name, '')

key = secret('KEEPA_API_KEY')
if not key:
    st.warning('Add KEEPA_API_KEY to Streamlit Secrets. Never put your API key in GitHub.')

with st.sidebar:
    st.header('Buying rules')
    max_cost = st.number_input('Maximum acquisition price ($)', 1., 500., 20.)
    min_hist_value = st.number_input('Minimum historical Used value ($)', 1., 500., 20.)
    min_profit = st.number_input('Minimum estimated profit ($)', 0., 100., 5.)
    min_roi = st.number_input('Minimum ROI (%)', 0, 1000, 50)
    extra_cost = st.number_input('Inbound shipping + prep ($)', 0., 30., 2.)
    fee_est = st.number_input('Estimated Amazon fees ($) — placeholder', 0., 50., 8.)
    max_rank = st.number_input('Maximum current sales rank', 1000, 10000000, 2000000, step=100000)
    st.divider()
    st.header('Value model')
    floor_days = st.selectbox('Intrinsic-value lookback', [90, 180, 365, 730], index=2)
    low_band = st.slider('Low-price band used for intrinsic value', 10, 50, 25, 5)
    chart_days = st.selectbox('Mini chart period', [90, 365, 730], index=1, format_func=lambda x: '90 days' if x==90 else ('1 year' if x==365 else '2 years'))
    st.caption('Intrinsic value uses generic USED history—not an FBA premium. The mini chart shows Used price plus sales rank/rank drops.')

@st.cache_data(ttl=3600, show_spinner=False)
def discover(api_key, max_rank, max_cost, min_hist_value):
    # Find books cheap today relative to their 90-day generic USED average.
    selection = {
        'rootCategory': [283155], 'productType': 0,
        'current_SALES_lte': int(max_rank),
        'current_USED_lte': int(max_cost * 100),
        'avg90_USED_gte': int(min_hist_value * 100),
        'perPage': 50, 'page': 0,
        'sort': [['current_USED', 'asc']]
    }
    r = requests.get('https://api.keepa.com/query', params={
        'key': api_key, 'domain': 1,
        'selection': json.dumps(selection, separators=(',', ':'))
    }, timeout=45)
    r.raise_for_status(); data = r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

@st.cache_data(ttl=86400, show_spinner=False)
def details(api_key, asins):
    r = requests.get('https://api.keepa.com/product', params={
        'key': api_key, 'domain': 1, 'asin': ','.join(asins), 'stats': 365, 'history': 1
    }, timeout=90)
    r.raise_for_status(); data = r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

def cents(v):
    return round(v / 100, 2) if isinstance(v, (int, float)) and v >= 0 else None

def at(arr, idx):
    return cents(arr[idx]) if isinstance(arr, list) and len(arr) > idx else None

def keepa_series(csv_arr, divide=100.0):
    if not isinstance(csv_arr, list) or len(csv_arr) < 2: return pd.Series(dtype=float)
    pts = []
    for i in range(0, len(csv_arr)-1, 2):
        t, v = csv_arr[i], csv_arr[i+1]
        if not isinstance(t,(int,float)) or not isinstance(v,(int,float)) or v < 0: continue
        dt = KEEPA_EPOCH + timedelta(minutes=int(t))
        pts.append((pd.Timestamp(dt).tz_convert(None).normalize(), v/divide))
    if not pts: return pd.Series(dtype=float)
    s = pd.Series({d:v for d,v in pts}).sort_index()
    end = pd.Timestamp.utcnow().tz_localize(None).normalize()
    return s.reindex(pd.date_range(s.index.min(), end, freq='D')).ffill().dropna()

def intrinsic_floor(s, days=365, band_pct=25):
    if s.empty: return None
    x = s[s.index >= s.index.max()-pd.Timedelta(days=days)].dropna()
    if len(x)<7: return None
    trim = x[x >= x.quantile(.05)]
    cutoff = trim.quantile(band_pct/100)
    low = trim[trim <= cutoff]
    return round(float(low.mean()),2) if len(low) else None

def seasonal_model(s):
    if s.empty or len(s)<365*2: return None
    df=s.rename('price').to_frame(); df['year']=df.index.year; df['month']=df.index.month
    hist=df[df.year < pd.Timestamp.utcnow().year]
    monthly=hist.groupby(['year','month']).price.median().reset_index()
    valid=monthly.groupby('month').year.nunique(); valid=valid[valid>=2].index
    if not len(valid): return None
    stats=monthly[monthly.month.isin(valid)].groupby('month').price.agg(['median','count'])
    baseline=float(hist.price.median())
    if baseline<=0:return None
    stats['premium']=stats['median']/baseline
    m=int(stats.premium.idxmax()); premium=float(stats.loc[m,'premium'])
    if premium<1.20:return None
    vals=monthly[monthly.month==m].price
    value=float(min(vals.median(), vals.quantile(.25)))
    confidence='High' if len(vals)>=3 else 'Medium'
    return {'month':m,'value':round(value,2),'premium':round((premium-1)*100,1),'years':int(len(vals)),'confidence':confidence}

def rank_drop_count(rank_s, days=90):
    if rank_s.empty:return 0
    x=rank_s[rank_s.index>=rank_s.index.max()-pd.Timedelta(days=days)].dropna()
    # Meaningful downward rank moves (lower rank number = improvement). This is a proxy, not exact unit sales.
    prev=x.shift(1)
    return int(((prev-x) > np.maximum(prev*0.08, 5000)).sum())

def month_name(m): return datetime(2000,m,1).strftime('%b') if m else ''

def mini_chart(price_s, rank_s, days, title):
    end=max(price_s.index.max() if not price_s.empty else pd.Timestamp.min, rank_s.index.max() if not rank_s.empty else pd.Timestamp.min)
    start=end-pd.Timedelta(days=days)
    p=price_s[price_s.index>=start]; r=rank_s[rank_s.index>=start]
    fig, ax1=plt.subplots(figsize=(8,2.35))
    if not p.empty:
        ax1.plot(p.index,p.values,color='black',linewidth=1.4,label='Used price')
    ax1.set_ylabel('Used $'); ax1.grid(alpha=.18); ax1.tick_params(axis='x',labelsize=8); ax1.tick_params(axis='y',labelsize=8)
    ax2=ax1.twinx()
    if not r.empty:
        ax2.plot(r.index,r.values,color='green',linewidth=.9,alpha=.8,label='Sales rank')
        ax2.invert_yaxis()
    ax2.set_ylabel('Sales rank'); ax2.tick_params(axis='y',labelsize=8)
    ax1.set_title(title,fontsize=9,loc='left')
    fig.tight_layout()
    return fig

st.subheader('1 · Discover undervalued USED books')
st.write('V3 looks for books whose **current generic Used price is low relative to their historical Used market value**. It does not assume an FBA price premium.')
st.caption('Keepa prices are market references. Verify the exact offer, condition, edition, seller, eligibility and checkout price before buying.')

if st.button('🔎 Discover up to 50 Used opportunities', disabled=not bool(key), type='primary'):
    try:
        with st.spinner('Searching Keepa for Used-market price gaps…'):
            response=discover(key,max_rank,max_cost,min_hist_value)
        st.session_state['asins_v3']=response.get('asinList',[])
        st.success(f"Found {len(st.session_state['asins_v3'])} candidates. Keepa tokens left: {response.get('tokensLeft','unknown')}")
    except Exception as e: st.error(f'Keepa discovery failed: {e}')

asins=st.session_state.get('asins_v3',[])
if asins:
    st.write(f'Candidate pool: **{len(asins)} books**')
    n=st.slider('Books to analyze now',1,min(20,len(asins)),min(5,len(asins)))
    st.caption('Start small on a 1-token/minute plan. Detailed history is cached for 24 hours.')
    if st.button(f'Analyze first {n} books'):
        try:
            with st.spinner('Analyzing Used price history, seasonality and rank drops…'):
                response=details(key,asins[:n])
            rows=[]; histories={}
            for p in response.get('products',[]):
                current=p.get('stats',{}).get('current',[]); csv=p.get('csv',[]); asin=p.get('asin')
                used=at(current,2); rank=current[3] if len(current)>3 and isinstance(current[3],(int,float)) and current[3]>=0 else None
                used_hist=keepa_series(csv[2],100.0) if isinstance(csv,list) and len(csv)>2 else pd.Series(dtype=float)
                rank_hist=keepa_series(csv[3],1.0) if isinstance(csv,list) and len(csv)>3 else pd.Series(dtype=float)
                floor=intrinsic_floor(used_hist,floor_days,low_band); season=seasonal_model(used_hist)
                drops90=rank_drop_count(rank_hist,90); median365=round(float(used_hist[used_hist.index>=used_hist.index.max()-pd.Timedelta(days=365)].median()),2) if not used_hist.empty else None
                histories[asin]=(used_hist,rank_hist)
                rows.append({'ASIN':asin,'Title':p.get('title',''),'Rank':rank,'Current Used ($)':used,'Intrinsic floor ($)':floor,'365d Used median ($)':median365,'Rank drops 90d':drops90,'Seasonal value ($)':season['value'] if season else None,'Peak month':month_name(season['month']) if season else '','Seasonal premium (%)':season['premium'] if season else None,'Seasonal years':season['years'] if season else 0,'Seasonal confidence':season['confidence'] if season else 'None','Amazon link':f'https://www.amazon.com/dp/{asin}','Keepa link':f'https://keepa.com/#!product/1-{asin}'})
            st.session_state['books_v3']=rows; st.session_state['histories_v3']=histories
            st.success(f"Analyzed {len(rows)} books. Tokens remaining: {response.get('tokensLeft','unknown')}")
        except Exception as e: st.error(f'Keepa details failed: {e}')

books=st.session_state.get('books_v3',[])
if books:
    st.subheader('2 · Used-book opportunity scoring')
    df=pd.DataFrame(books); df.insert(3,'Verified buy cost ($)',df['Current Used ($)'].fillna(max_cost))
    edited=st.data_editor(df,hide_index=True,use_container_width=True,num_rows='fixed',disabled=[c for c in df.columns if c!='Verified buy cost ($)'])
    outcomes=[]
    for _,r in edited.iterrows():
        buy=float(r['Verified buy cost ($)']) if pd.notna(r['Verified buy cost ($)']) else None
        floor=float(r['Intrinsic floor ($)']) if pd.notna(r['Intrinsic floor ($)']) else None
        seasonal=float(r['Seasonal value ($)']) if pd.notna(r['Seasonal value ($)']) else None
        ep=round(floor-buy-fee_est-extra_cost,2) if buy is not None and floor is not None else None
        er=round(100*ep/(buy+extra_cost),1) if ep is not None and buy+extra_cost>0 else None
        sp=round(seasonal-buy-fee_est-extra_cost,2) if buy is not None and seasonal is not None else None
        sr=round(100*sp/(buy+extra_cost),1) if sp is not None and buy+extra_cost>0 else None
        eok=ep is not None and ep>=min_profit and er>=min_roi and buy<=max_cost
        sok=sp is not None and sp>=min_profit and sr>=min_roi and buy<=max_cost and (r['Seasonal years'] or 0)>=2
        action='🟢 BUY NOW' if eok else ('🟠 BUY & HOLD' if sok else ('⚪ REVIEW' if ep is None and sp is None else '🔴 PASS'))
        maxbuy=round(floor-fee_est-extra_cost-min_profit,2) if floor is not None else None
        outcomes.append({'Action':action,'ASIN':r['ASIN'],'Title':r['Title'],'Rank':r['Rank'],'Rank drops 90d':r['Rank drops 90d'],'Buy ($)':buy,'Intrinsic floor ($)':floor,'365d median ($)':r['365d Used median ($)'],'Max buy @ min profit ($)':maxbuy,'Profit @ floor ($)':ep,'ROI @ floor (%)':er,'Seasonal value ($)':seasonal,'Peak month':r['Peak month'],'Seasonal confidence':r['Seasonal confidence'],'Seasonal profit ($)':sp,'Seasonal ROI (%)':sr,'Amazon link':r['Amazon link'],'Keepa link':r['Keepa link']})
    result=pd.DataFrame(outcomes); order={'🟢 BUY NOW':0,'🟠 BUY & HOLD':1,'⚪ REVIEW':2,'🔴 PASS':3}; result['_o']=result.Action.map(order).fillna(9); result=result.sort_values(['_o','Profit @ floor ($)','Seasonal profit ($)'],ascending=[True,False,False]).drop(columns='_o')
    st.dataframe(result,use_container_width=True,hide_index=True)

    st.subheader('3 · Mini Keepa-style visual checks')
    st.caption('⚫ black = generic Used price. 🟢 green = sales rank (lower/better rank plots upward). Rank drops are a demand proxy, not exact unit sales.')
    histories=st.session_state.get('histories_v3',{})
    for _,r in result.iterrows():
        asin=r['ASIN']; p,rk=histories.get(asin,(pd.Series(dtype=float),pd.Series(dtype=float)))
        with st.expander(f"{r['Action']} · {r['Title'][:90]} · {asin}", expanded=r['Action'] in ['🟢 BUY NOW','🟠 BUY & HOLD']):
            c1,c2,c3,c4=st.columns(4)
            c1.metric('Buy',f"${r['Buy ($)']:.2f}" if pd.notna(r['Buy ($)']) else '—'); c2.metric('Intrinsic Used',f"${r['Intrinsic floor ($)']:.2f}" if pd.notna(r['Intrinsic floor ($)']) else '—'); c3.metric('Rank drops 90d',int(r['Rank drops 90d']) if pd.notna(r['Rank drops 90d']) else 0); c4.metric('Peak month',r['Peak month'] or '—')
            fig=mini_chart(p,rk,chart_days,f"{chart_days}-day Used price + sales rank")
            st.pyplot(fig,use_container_width=True); plt.close(fig)
            st.markdown(f"[Amazon]({r['Amazon link']}) · [Full Keepa chart]({r['Keepa link']})")

    st.download_button('Download V3 results CSV',result.to_csv(index=False).encode(),'bookfinder_v3_results.csv','text/csv')
    st.info('BUY NOW = works at the conservative generic Used floor. BUY & HOLD = recurring seasonal Used-market value in at least 2 prior years. Always verify exact condition and edition before purchase.')
else:
    st.info('Run discovery, then analyze a small batch. V3 uses generic Used history throughout the value model.')
