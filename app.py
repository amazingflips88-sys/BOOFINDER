import os, json
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
import numpy as np
import streamlit as st

st.set_page_config(page_title='Book Finder V2', page_icon='📚', layout='wide')
st.title('📚 Private FBA Book Finder — V2')
st.caption('A2A price-gap discovery + conservative intrinsic value + recurring seasonal value from Keepa history.')

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
    min_profit = st.number_input('Minimum estimated profit ($)', 0., 100., 5.)
    min_roi = st.number_input('Minimum ROI (%)', 0, 1000, 50)
    extra_cost = st.number_input('Inbound shipping + prep ($)', 0., 30., 2.)
    fee_est = st.number_input('Estimated Amazon fees ($) — placeholder', 0., 50., 8.)
    max_rank = st.number_input('Maximum current sales rank', 1000, 10000000, 1000000, step=100000)
    min_fba = st.number_input('Minimum current Used FBA price ($)', 1., 500., 20.)
    st.divider()
    st.header('Value model')
    floor_days = st.selectbox('Intrinsic-value lookback', [30, 90, 180, 365], index=1)
    low_band = st.slider('Low-price band used for intrinsic value', 10, 50, 25, 5)
    st.caption('Example: 25% = average of the lower quarter of daily prices after trimming extreme lows.')

@st.cache_data(ttl=3600, show_spinner=False)
def discover(api_key, max_rank, max_cost, min_fba):
    # Discovery is intentionally sorted by cheap Used acquisition price, NOT sales rank.
    # current_USED is a market reference; exact buyability/condition must still be verified.
    selection = {
        'rootCategory': [283155], 'productType': 0,
        'current_SALES_lte': int(max_rank),
        'current_USED_lte': int(max_cost * 100),
        'current_USED_FBA_gte': int(min_fba * 100),
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
    # Keep full price history for seasonality; stats gives convenient current values.
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

def keepa_series(csv_arr):
    """Convert Keepa [minute, cents, minute, cents...] change points to daily forward-filled prices."""
    if not isinstance(csv_arr, list) or len(csv_arr) < 2: return pd.Series(dtype=float)
    pts = []
    for i in range(0, len(csv_arr) - 1, 2):
        t, v = csv_arr[i], csv_arr[i + 1]
        if not isinstance(t, (int, float)) or not isinstance(v, (int, float)) or v < 0: continue
        dt = KEEPA_EPOCH + timedelta(minutes=int(t))
        pts.append((pd.Timestamp(dt).tz_convert(None).normalize(), v / 100.0))
    if not pts: return pd.Series(dtype=float)
    s = pd.Series({d: v for d, v in pts}).sort_index()
    end = pd.Timestamp.utcnow().tz_localize(None).normalize()
    start = s.index.min()
    return s.reindex(pd.date_range(start, end, freq='D')).ffill().dropna()

def intrinsic_floor(s, days=90, band_pct=25):
    if s.empty: return None
    x = s[s.index >= s.index.max() - pd.Timedelta(days=days)].dropna()
    if len(x) < 7: return None
    # Remove extreme one-off lows, then average the lower band of observed daily prices.
    trim = x[x >= x.quantile(.05)]
    cutoff = trim.quantile(band_pct / 100)
    low = trim[trim <= cutoff]
    return round(float(low.mean()), 2) if len(low) else None

def seasonal_model(s):
    """Find recurring calendar-month premium using completed prior years; require >=2 years."""
    if s.empty or len(s) < 365 * 2: return None
    df = s.rename('price').to_frame(); df['year'] = df.index.year; df['month'] = df.index.month
    current_year = pd.Timestamp.utcnow().year
    hist = df[df['year'] < current_year]
    if hist.empty: return None
    monthly = hist.groupby(['year', 'month']).price.median().reset_index()
    years_per_month = monthly.groupby('month').year.nunique()
    valid_months = years_per_month[years_per_month >= 2].index
    if len(valid_months) == 0: return None
    month_stats = monthly[monthly.month.isin(valid_months)].groupby('month').price.agg(['median','count'])
    baseline = float(hist.price.median())
    if baseline <= 0: return None
    month_stats['premium'] = month_stats['median'] / baseline
    best_month = int(month_stats['premium'].idxmax())
    premium = float(month_stats.loc[best_month, 'premium'])
    if premium < 1.20: return None
    # Conservative seasonal value: lower of median seasonal month and 25th percentile across yearly medians.
    vals = monthly[monthly.month == best_month].price
    seasonal_value = float(min(vals.median(), vals.quantile(.25)))
    return {'month': best_month, 'value': round(seasonal_value,2), 'premium': round((premium-1)*100,1), 'years': int(len(vals))}

def month_name(m):
    return datetime(2000, m, 1).strftime('%b') if m else ''

st.subheader('1 · Find A2A price-gap candidates')
st.write('V2 searches for **cheap Used market prices with a higher Used FBA market**, then evaluates whether the higher value is supported by history.')
st.caption('Keepa prices are market references, not guaranteed live purchasable offers. Exact seller, condition, edition, eligibility and checkout price must be verified before buying.')

if st.button('🔎 Discover up to 50 A2A candidates', disabled=not bool(key), type='primary'):
    try:
        with st.spinner('Searching Keepa for price gaps…'):
            response = discover(key, max_rank, max_cost, min_fba)
        st.session_state['asins'] = response.get('asinList', [])
        st.success(f"Found {len(st.session_state['asins'])} candidates. Keepa tokens left: {response.get('tokensLeft','unknown')}")
    except Exception as e:
        st.error(f'Keepa discovery failed: {e}')

asins = st.session_state.get('asins', [])
if asins:
    st.write(f'Candidate pool: **{len(asins)} books**')
    n = st.slider('Books to analyze now', 1, min(20, len(asins)), min(5, len(asins)))
    st.caption('Start small on a 1-token/minute Keepa plan. Cached books will not be re-requested for 24 hours.')
    if st.button(f'Analyze first {n} books'):
        try:
            with st.spinner('Building intrinsic and seasonal values from Keepa history…'):
                response = details(key, asins[:n])
            rows = []
            for p in response.get('products', []):
                current = p.get('stats', {}).get('current', [])
                csv = p.get('csv', [])
                used = at(current, 2)
                used_fba = at(current, 10)
                rank = current[3] if len(current) > 3 and isinstance(current[3], (int,float)) and current[3] >= 0 else None
                # Prefer Used FBA history; fall back to Used history when FBA history is unavailable.
                fba_hist = keepa_series(csv[10]) if isinstance(csv, list) and len(csv) > 10 else pd.Series(dtype=float)
                used_hist = keepa_series(csv[2]) if isinstance(csv, list) and len(csv) > 2 else pd.Series(dtype=float)
                hist = fba_hist if len(fba_hist) >= 30 else used_hist
                floor = intrinsic_floor(hist, floor_days, low_band)
                season = seasonal_model(hist)
                rows.append({
                    'ASIN': p.get('asin'), 'Title': p.get('title',''), 'Rank': rank,
                    'Market buy ref ($)': used, 'Current Used FBA ($)': used_fba,
                    'Intrinsic floor ($)': floor,
                    'Seasonal value ($)': season['value'] if season else None,
                    'Best month': month_name(season['month']) if season else '',
                    'Seasonal premium (%)': season['premium'] if season else None,
                    'Seasonal years': season['years'] if season else 0,
                    'Amazon link': f"https://www.amazon.com/dp/{p.get('asin')}"
                })
            st.session_state['books_v2'] = rows
            st.success(f"Analyzed {len(rows)} books. Tokens remaining: {response.get('tokensLeft','unknown')}")
        except Exception as e:
            st.error(f'Keepa details failed: {e}')

books = st.session_state.get('books_v2', [])
if books:
    st.subheader('2 · Opportunity scoring')
    df = pd.DataFrame(books)
    df.insert(3, 'Verified buy cost ($)', df['Market buy ref ($)'].fillna(max_cost))
    edited = st.data_editor(df, hide_index=True, use_container_width=True, num_rows='fixed',
        disabled=[c for c in df.columns if c != 'Verified buy cost ($)'])

    outcomes = []
    now_month = pd.Timestamp.utcnow().month
    for _, r in edited.iterrows():
        buy = float(r['Verified buy cost ($)']) if pd.notna(r['Verified buy cost ($)']) else None
        floor = float(r['Intrinsic floor ($)']) if pd.notna(r['Intrinsic floor ($)']) else None
        seasonal = float(r['Seasonal value ($)']) if pd.notna(r['Seasonal value ($)']) else None
        best_month = r['Best month'] if pd.notna(r['Best month']) else ''
        # Evergreen case uses conservative floor. Seasonal case is separate and never silently replaces it.
        evergreen_profit = round(floor - buy - fee_est - extra_cost, 2) if buy is not None and floor is not None else None
        evergreen_roi = round(100 * evergreen_profit / (buy + extra_cost), 1) if evergreen_profit is not None and buy + extra_cost > 0 else None
        seasonal_profit = round(seasonal - buy - fee_est - extra_cost, 2) if buy is not None and seasonal is not None else None
        seasonal_roi = round(100 * seasonal_profit / (buy + extra_cost), 1) if seasonal_profit is not None and buy + extra_cost > 0 else None
        evergreen_ok = evergreen_profit is not None and evergreen_profit >= min_profit and evergreen_roi >= min_roi and buy <= max_cost
        seasonal_ok = seasonal_profit is not None and seasonal_profit >= min_profit and seasonal_roi >= min_roi and buy <= max_cost and (r['Seasonal years'] or 0) >= 2
        if evergreen_ok: action = '🟢 BUY NOW'
        elif seasonal_ok: action = '🟠 BUY & HOLD'
        elif evergreen_profit is None and seasonal_profit is None: action = '⚪ REVIEW'
        else: action = '🔴 PASS'
        outcomes.append({
            'Action': action, 'ASIN': r['ASIN'], 'Title': r['Title'], 'Rank': r['Rank'],
            'Buy ($)': buy, 'Current FBA ($)': r['Current Used FBA ($)'],
            'Intrinsic floor ($)': floor, 'Profit @ floor ($)': evergreen_profit, 'ROI @ floor (%)': evergreen_roi,
            'Seasonal value ($)': seasonal, 'Peak month': best_month,
            'Seasonal profit ($)': seasonal_profit, 'Seasonal ROI (%)': seasonal_roi,
            'Recurring years': r['Seasonal years'], 'Amazon link': r['Amazon link']
        })
    result = pd.DataFrame(outcomes)
    order = {'🟢 BUY NOW':0, '🟠 BUY & HOLD':1, '⚪ REVIEW':2, '🔴 PASS':3}
    result['_order'] = result.Action.map(order).fillna(9)
    result = result.sort_values(['_order','Profit @ floor ($)','Seasonal profit ($)'], ascending=[True,False,False]).drop(columns='_order')
    st.dataframe(result, use_container_width=True, hide_index=True)
    st.download_button('Download V2 results CSV', result.to_csv(index=False).encode(), 'bookfinder_v2_results.csv', 'text/csv')
    st.info('🟢 BUY NOW = profitable at conservative historical floor.  🟠 BUY & HOLD = not attractive at the floor, but has a recurring seasonal premium in at least 2 prior years. Always verify the actual offer and exact edition before purchase.')
else:
    st.info('Run discovery, then analyze a small batch. V2 deliberately does not rank candidates simply by best sales rank.')
