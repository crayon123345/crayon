"""Curated first-party plugins. No arbitrary code, remote endpoints or private context."""
CATALOG=[{'id':'games','name':'Quick games','privacy':'Requester/chat-bound quiz and guess; no model memory.'},{'id':'voice','name':'Voice','privacy':'Opt-in explicit text only; verified Free ElevenLabs shared cap.'},{'id':'persona','name':'Persona','privacy':'Explicit nickname/style only; no permission changes.'},{'id':'mcp','name':'MCP public tools','privacy':'Connect your own public HTTPS server in web Menu > Plugins / MCP. One-time risk consent, per-call exact review. No ambient Google/account context.'}]
def handle(uid,chat,text,out):
    if text.strip() not in ('/plugins','/mcp'):return False
    if text.strip()=='/mcp':
        from cr_mcp import catalog
        servers={**catalog(),**__import__('cr_mcp_user').connectors(uid)};out.send(chat,'MCP read-only public tools: '+(', '.join(servers) if servers else 'none connected')+'. Add your own HTTPS connector in the web Plugins / MCP panel. Tool calls require exact review there. OAuth, stdio, sampling and private account imports are not supported.');return True
    out.send(chat,'Available plugins:\n'+'\n'.join(x['name']+': '+x['privacy'] for x in CATALOG));return True
