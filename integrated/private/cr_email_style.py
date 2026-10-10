"""Fixed escaped HTML template. Never render model-provided markup or remote assets."""
import html

def render(body,signature=False):
 paragraphs=''.join('<p style="margin:0 0 16px;line-height:1.65">'+html.escape(p).replace('\n','<br>')+'</p>' for p in body.split('\n\n'))
 footer='<div style="border-top:1px solid #e4e1dc;padding-top:16px;margin-top:24px;color:#8a5139;font-size:12px;font-weight:600">Sent by Crayon AI</div>' if signature else ''
 return '<!doctype html><html><body style="margin:0;background:#f7f5f1"><div style="max-width:620px;margin:24px auto;background:white;border:1px solid #e4e1dc;border-radius:12px;padding:32px;color:#252522;font:15px Arial,sans-serif">'+paragraphs+footer+'</div></body></html>'
