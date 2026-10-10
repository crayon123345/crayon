"""Narrow verified research-to-chart recipe. No model-generated numbers."""
import json,math
URL='https://api.worldbank.org/v2/country/IND;CHN;USA/indicator/NY.GDP.PCAP.CD?date=2024&format=json&per_page=3'
def extract(text):
    payload=json.loads(text)
    if not isinstance(payload,list) or len(payload)!=2 or payload[0].get('total')!=3:raise ValueError('Expected three World Bank records')
    found={}
    for row in payload[1]:
        code=row.get('countryiso3code')
        if code not in ('IND','CHN','USA') or row.get('date')!='2024' or row.get('indicator',{}).get('id')!='NY.GDP.PCAP.CD':raise ValueError('Source country/year/indicator mismatch')
        value=row.get('value')
        if type(value) not in (int,float) or not math.isfinite(value) or value<0 or code in found:raise ValueError('Missing or invalid source value')
        found[code]=value
    if len(found)!=3:raise ValueError('Missing countries')
    return [found[x] for x in ('IND','CHN','USA')],payload[0].get('lastupdated','not supplied')
def run(ctx):
    import cr_tools as T,cr_artifacts as A
    res=T.computer_browse(ctx,URL)
    if not res.get('verified'):raise ValueError(res.get('error','Official source unavailable'))
    page=res['pages'][0]
    if page['url']!=URL:raise ValueError('Unexpected source redirect')
    values,updated=extract(page['text'])
    labels=['India','China','United States']
    ctx['meta'].setdefault('artifacts',[]).extend([
        {'filename':'world-bank-gdp-2024.csv','mime':'text/csv','data':A.csv_bytes(['Country','Year','GDP per capita current USD','Source'],[[n,2024,v,page['url']] for n,v in zip(labels,values)])},
        {'filename':'world-bank-gdp-2024.png','mime':'image/png','data':A.chart_bytes('GDP per capita,2024',labels,values,'current US$',footer='Source: World Bank | NY.GDP.PCAP.CD |2024')}])
    lines=['World Bank GDP per capita,2024 (current US$):']+[n+': $'+format(v,',.2f') for n,v in zip(labels,values)]
    lines+=['This is nominal GDP per person, not income, purchasing power or a living-standard ranking.','Source updated: '+updated,'Source: '+page['url'],'Work log:']+res.get('action_log',[])+['Validated three country/year/indicator records.','Generated CSV and chart from those exact values.','Narrow World Bank demo recipe, not arbitrary research automation.']
    return '\n'.join(lines)
