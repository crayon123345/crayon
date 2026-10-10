"""Non-executable pseudocode reference. Review contract only, not live authentication.
No keys/provider calls/audio persistence. Real implementation must supply atomic stores.
"""

def issue(request, auth, clock, tickets, conversations, limits, consent):
    owner,session=auth.canonical(request.bearer)
    origin=auth.exact_https_origin(request.origin) # rejects absent/null, never UA
    body=request.strict_body('version','conversation_id','mode','provider_consent_version')
    conversations.require_owned(owner,session,body['conversation_id'])
    consent.require_explicit_current(owner,body['mode'],body['provider_consent_version'])
    limits.issue(owner,session,request.ip,6,60)
    # tickets.create returns opaque ticket, persists only hash, expiry exactly now+120.
    return tickets.create(owner=owner,session=session,conversation=body['conversation_id'],
                          origin=origin,mode=body['mode'],expires_at=clock.now()+120)

def redeem(request, auth, clock, tickets, leases):
    adapter=auth.scoped_adapter(request.bearer,'redeem')
    body=request.strict_body('version','ticket','observed_origin','mode')
    auth.exact_https_origin(body['observed_origin'])
    # atomic claim verifies session live, strict now<expiry, exact mode+observed origin.
    bound=tickets.claim_once(body['ticket'],body['observed_origin'],body['mode'],clock.now(),auth)
    return leases.create(bound,adapter,clock.now(),max_seconds=600,max_bytes=20000000)

def revalidate(request, auth, clock, leases):
    adapter=auth.scoped_adapter(request.bearer,'revalidate')
    body=request.strict_body('version','lease_id')
    bound=leases.active_owned_adapter(body['lease_id'],adapter,clock.now())
    auth.require_live_session(bound.owner,bound.session) # unavailable means deny
    return {'active':True,'next_check_seconds':15}

def final(request, auth, clock, leases, finals, history):
    adapter=auth.scoped_adapter(request.bearer,'final')
    body=request.strict_body('version','lease_id','request_id','seq','type','state',
                             optional=('transcript_text','answer_text','error_code'))
    bound=leases.active_owned_adapter(body['lease_id'],adapter,clock.now())
    auth.require_live_session(bound.owner,bound.session)
    assert body['request_id']==bound.request_id and body['type']=='done'
    # One atomic terminal final per owner+request, hash exact-replay or reject conflict.
    # Sequence claim, final buffer and canonical history use one transaction.
    return finals.store_once_and_history(bound,body,history,ttl_seconds=300)

def recovery(request, auth, clock, finals, conversations):
    owner,session=auth.canonical(request.bearer)
    origin=auth.exact_https_origin(request.origin)
    result=finals.read_owned(owner,session,request.query_request_id,origin,clock.now())
    conversations.require_owned(owner,session,result.conversation)
    return result # read only, no effects/provider/audio replay


def progress(request, auth, clock, leases, events):
    adapter=auth.scoped_adapter(request.bearer,'progress')
    body=request.strict_body('version','lease_id','seq','type','state',optional=('text',))
    allowed={'accepted','progress','transcript_partial','transcript_final','answer_partial',
             'answer_final','review_required','artifact','blocked','done'}
    assert body['type'] in allowed
    bound=leases.active_owned_adapter(body['lease_id'],adapter,clock.now())
    auth.require_live_session(bound.owner,bound.session)
    # Final components eligible for short recovery, partials never persisted there.
    # done here is notification only, not terminal history/uniqueness commit.
    return events.monotonic_component(bound,body,recovery=body['type'] not in
                                      {'transcript_partial','answer_partial'})
