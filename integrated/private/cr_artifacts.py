"""Bounded user-requested CSV and chart attachments. No generated code or disk paths."""
import csv
import io
import math
from PIL import Image, ImageDraw, ImageFont

def csv_bytes(headers,rows):
    if not isinstance(headers,list) or not 1<=len(headers)<=20 or not isinstance(rows,list) or not 1<=len(rows)<=100:raise ValueError('CSV needs 1-20 headers and 1-100 rows')
    def cell(x):
        if not isinstance(x,(str,int,float)) or isinstance(x,bool):raise ValueError('CSV cells must be text or numbers')
        s=str(x)
        if len(s)>500:raise ValueError('CSV cell too long')
        # Spreadsheet formula injection defense, including leading whitespace.
        if s.lstrip().startswith(('=','+','-','@')):s="'"+s
        return s
    stream=io.StringIO(newline='');w=csv.writer(stream);w.writerow([cell(x) for x in headers])
    for row in rows:
        if not isinstance(row,list) or len(row)!=len(headers):raise ValueError('CSV row width does not match headers')
        w.writerow([cell(x) for x in row])
    return stream.getvalue().encode('utf-8-sig')

def chart_bytes(title,labels,values,unit='',footer='Crayon | Supplied data, not independently verified'):
    if not isinstance(labels,list) or not isinstance(values,list) or not 1<=len(labels)<=12 or len(labels)!=len(values):raise ValueError('chart needs matching1-12 labels and values')
    if not isinstance(title,str) or not 1<=len(title)<=70 or len(unit)>25:raise ValueError('title or unit too long')
    if any(not isinstance(x,str) or not 1<=len(x)<=22 for x in labels):raise ValueError('labels must be1-22 characters')
    nums=[float(x) for x in values]
    if any(not math.isfinite(x) or x<0 or x>1e12 for x in nums):raise ValueError('bar chart supports finite non-negative values only')
    height=180+len(labels)*54;im=Image.new('RGB',(1000,height),'white');d=ImageDraw.Draw(im)
    def font(size):
        try:return ImageFont.truetype('DejaVuSans.ttf',size)
        except OSError:return ImageFont.load_default(size=size)
    if d.textlength(title,font=font(28))>930 or any(d.textlength(x,font=font(18))>240 for x in labels):raise ValueError('title or label is too wide for this chart')
    d.text((32,25),title,fill='#2B2D31',font=font(28));d.text((32,70),'Values'+(' ('+unit+')' if unit else ''),fill='#6B7280',font=font(18))
    maximum=max(nums) or 1
    for i,(label,v) in enumerate(zip(labels,nums)):
        y=112+i*54;d.text((32,y+4),label,fill='#2B2D31',font=font(18));w=round(540*v/maximum)
        d.rectangle((285,y,285+w,y+30),fill='#FFC93C');d.text((840,y+3),format(v,'.8g'),fill='#2B2D31',font=font(18))
    d.text((32,height-40),footer[:110],fill='#6B7280',font=font(16))
    b=io.BytesIO();im.save(b,format='PNG');return b.getvalue()
