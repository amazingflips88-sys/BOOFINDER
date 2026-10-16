import os, time, json
import requests
import pandas as pd
import streamlit as st

st.set_page_config(page_title='Book Finder',page_icon='📚',layout='wide')
st.title('📚 Private FBA Book Finder — V1')
st.caption('Keepa-powered candidate discovery. All results require manual verification before buying.')

def secret(name):
    try: return st.secrets.get(name, os.getenv(name,''))
    except Exception: return os.getenv(name,'')

key=secret('KEEPA_API_KEY')
if not key:
    st.warning('Add KEEPA_API_KEY to your deployment secrets to enable Keepa searches. Never paste it into source code.')

with st.sidebar:
    st.header('Buying rules')
    max_cost=st.number_input('Maximum purchase cost ($)',1.,500.,20.)
    min_profit=st.number_input('Minimum profit ($)',0.,100.,5.)
    min_roi=st.number_input('Minimum ROI (%)',0,1000,50)
    extra_cost=st.number_input('Inbound shipping + prep per book ($)',0.,30.,2.)
    fee_est=st.number_input('Estimated total Amazon fees ($) — placeholder',0.,50.,8.)
    st.info('Fee placeholder is NOT an Amazon fee quote. Verify actual FBA fees, restrictions and condition.')

@st.cache_data(ttl=86400,show_spinner=False)
def discover(api_key, max_rank, min_used_price):
    selection={'rootCategory':[283155], 'productType':0, 'current_SALES_lte':int(max_rank), 'current_USED_gte':int(min_used_price*100), 'perPage':50, 'page':0, 'sort':[['current_SALES','asc']]}
    r=requests.get('https://api.keepa.com/query',params={'key':api_key,'domain':1,'selection':json.dumps(selection,separators=(',',':'))},timeout=45)
    r.raise_for_status(); data=r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

@st.cache_data(ttl=86400,show_spinner=False)
def details(api_key, asins):
    r=requests.get('https://api.keepa.com/product',params={'key':api_key,'domain':1,'asin':','.join(asins),'stats':90},timeout=60)
    r.raise_for_status(); data=r.json()
    if data.get('error'): raise ValueError(str(data['error']))
    return data

def cents(v):
    return round(v/100,2) if isinstance(v,(int,float)) and v>=0 else None

def price_at(arr, idx):
    return cents(arr[idx]) if isinstance(arr,list) and len(arr)>idx else None

st.subheader('1 · Discover candidate books')
col1,col2=st.columns(2)
with col1: max_rank=st.number_input('Maximum current sales rank',1000,10000000,1000000,step=100000)
with col2: min_used_price=st.number_input('Minimum current used price ($)',1.,500.,20.)
st.caption('Product Finder costs at least 10 tokens + 1 per 100 ASINs. Details cost 1 token per ASIN. Your 1-token/minute plan may require waiting for tokens to replenish.')
if st.button('Discover 50 book ASINs',disabled=not bool(key)):
    try:
        with st.spinner('Searching Keepa…'):
            response=discover(key,max_rank,min_used_price)
        st.session_state['asins']=response.get('asinList',[])
        st.success(f"Found {len(st.session_state['asins'])} candidate ASINs. Keepa tokens left: {response.get('tokensLeft','unknown')}")
    except Exception as e: st.error(f'Keepa discovery failed: {e}')

asins=st.session_state.get('asins',[])
if asins:
    st.write('First candidates:',', '.join(asins[:15]))
    n=st.slider('Number of candidates to analyze (token cost: approximately 1 per book)',1,min(20,len(asins)),min(5,len(asins)))
    if st.button(f'Analyze first {n} books'):
        try:
            with st.spinner('Retrieving book details…'):
                response=details(key,asins[:n])
            rows=[]
            for p in response.get('products',[]):
                current=p.get('stats',{}).get('current',[])
                avg90=p.get('stats',{}).get('avg90',[])
                # Keepa CSV indices: 2=used, 3=sales rank, 10=used FBA, 12=warehouse; verify all offers live.
                used=price_at(current,2)
                used_fba=price_at(current,10)
                historical=price_at(avg90,10) or price_at(avg90,2)
                rank=current[3] if len(current)>3 and current[3]>=0 else None
                rows.append({'ASIN':p.get('asin'),'Title':p.get('title',''), 'Used market ($)':used,'Used FBA ($)':used_fba,'90d reference ($)':historical,'Rank':rank,'Amazon link':f"https://www.amazon.com/dp/{p.get('asin')}"})
            st.session_state['books']=rows
            st.success(f"Analyzed {len(rows)} books. Tokens remaining: {response.get('tokensLeft','unknown')}")
        except Exception as e: st.error(f'Keepa details failed: {e}')

books=st.session_state.get('books',[])
if books:
    st.subheader('2 · Evaluate purchase opportunities')
    st.warning('Keepa prices are market references, NOT verified purchasable offers. Enter a real sourcing cost and check the exact condition/edition before interpreting BUY.')
    df=pd.DataFrame(books)
    df.insert(2,'Verified buy cost ($)',max_cost)
    edited=st.data_editor(df,hide_index=True,disabled=['ASIN','Title','Used market ($)','Used FBA ($)','90d reference ($)','Rank','Amazon link'],num_rows='fixed',use_container_width=True)
    outcomes=[]
    for _,row in edited.iterrows():
        buy=float(row['Verified buy cost ($)'])
        reference=row['90d reference ($)']
        sell=float(reference) if pd.notna(reference) else None
        profit=round(sell-buy-fee_est-extra_cost,2) if sell is not None else None
        roi=round(100*profit/(buy+extra_cost),1) if profit is not None and buy+extra_cost>0 else None
        status='REVIEW' if profit is None else ('CANDIDATE' if profit>=min_profit and roi>=min_roi and buy<=max_cost else 'PASS')
        outcomes.append({'ASIN':row['ASIN'],'Title':row['Title'],'Buy ($)':buy,'90d reference ($)':sell,'Est. profit ($)':profit,'Est. ROI (%)':roi,'Status':status,'Amazon link':row['Amazon link']})
    result=pd.DataFrame(outcomes)
    st.dataframe(result,use_container_width=True,hide_index=True)
    st.download_button('Download results CSV',result.to_csv(index=False).encode(),'bookfinder_results.csv','text/csv')
    st.caption('CANDIDATE is not an automatic purchase recommendation. FBA fees, eligibility, availability, tax, exact condition, and source pricing must be confirmed.')
else:
    st.info('Start with Discover. After results arrive, analyze a small batch to conserve tokens.')
