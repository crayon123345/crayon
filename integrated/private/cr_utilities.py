"""Bounded deterministic conversions and dated public exchange rates."""
from decimal import Decimal, InvalidOperation
import re
import httpx

# Factors convert to the category's base unit. No model-generated code.
UNITS={'m':('length','1'),'km':('length','1000'),'cm':('length','.01'),'mm':('length','.001'),
'in':('length','.0254'),'ft':('length','.3048'),'yd':('length','.9144'),'mi':('length','1609.344'),
'kg':('mass','1'),'g':('mass','.001'),'lb':('mass','.45359237'),'oz':('mass','.028349523125'),
'l':('volume','1'),'ml':('volume','.001'),'s':('time','1'),'min':('time','60'),'h':('time','3600'),
'm/s':('speed','1'),'km/h':('speed','0.2777777777777777777777777778'),'mph':('speed','.44704')}
ALIASES={'meter':'m','metre':'m','kilometer':'km','kilometre':'km','centimeter':'cm','centimetre':'cm','millimeter':'mm','millimetre':'mm','mile':'mi','foot':'ft','inch':'in','yard':'yd','yards':'yd','pound':'lb','kilogram':'kg','gram':'g','ounce':'oz','ounces':'oz','liter':'l','litre':'l','milliliter':'ml','millilitre':'ml','milliliters':'ml','millilitres':'ml','hour':'h','minute':'min','second':'s','meters':'m','metres':'m','kilometers':'km','kilometres':'km','feet':'ft','inches':'in','miles':'mi','pounds':'lb','kilograms':'kg','grams':'g','liters':'l','litres':'l','hours':'h','minutes':'min','seconds':'s','celsius':'c','fahrenheit':'f','kelvin':'k'}

def number(value):
    n=Decimal(str(value))
    if not n.is_finite() or abs(n)>Decimal('1e18'):raise ValueError('number is outside supported limits')
    return n

def convert(value,source,target):
    n=number(value);source=ALIASES.get(source.lower(),source.lower());target=ALIASES.get(target.lower(),target.lower())
    if source in ('c','f','k') and target in ('c','f','k'):
        c=(n-32)*Decimal(5)/9 if source=='f' else n-Decimal('273.15') if source=='k' else n
        if c<Decimal('-273.15'):raise ValueError('temperature is below absolute zero')
        result=c*9/5+32 if target=='f' else c+Decimal('273.15') if target=='k' else c
    else:
        if source not in UNITS or target not in UNITS or UNITS[source][0]!=UNITS[target][0]:raise ValueError('unsupported or mismatched units')
        result=n*Decimal(UNITS[source][1])/Decimal(UNITS[target][1])
    return {'value':str(n),'from':source,'to':target,'result':str(result.normalize()),'method':'fixed conversion factors, Decimal arithmetic'}

def currency(value,source,target):
    n=number(value);source=source.upper();target=target.upper()
    if not re.fullmatch('[A-Z]{3}',source) or not re.fullmatch('[A-Z]{3}',target):raise ValueError('use three-letter currency codes')
    url='https://api.frankfurter.dev/v2/rate/'+source+'/'+target
    with httpx.Client(timeout=12) as client:
        r=client.get(url)
        if r.status_code!=200:raise RuntimeError('rate provider did not return this pair')
        row=r.json()
    if not isinstance(row,dict) or row.get('base')!=source or row.get('quote')!=target or not row.get('date'):raise RuntimeError('rate response could not be verified')
    rate=number(row['rate']);result=n*rate
    return {'value':str(n),'from':source,'to':target,'result':str(result.normalize()),'rate':str(rate),'date':row['date'],'url':url,'note':'Dated reference exchange rate, not a live quote. Bank/card fees and spreads excluded.'}
