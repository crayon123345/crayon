"""Tests for PR-3 capability + policy MODEL - SYNTHETIC, local disposable.

Scope (peer controlling addendum 1/4-4/4, DRAFT-READY 2026-10-11): the three
NEW files only; MODEL semantics. This file proves NOTHING about production
authentication, store trust binding, envelope persistence, keys, or real
consent gates - all production entry points are fail-closed stubs and are
asserted to refuse BEFORE touching any dependency.

Adversarial boundaries are exercised through the REAL model APIs
(mint_model / recheck_model / production_* / SqliteModelSnapshotStore.read):
shape validators are the real dataclass __post_init__ code; malformed STORE
records are injected through the actual sqlite store; NO test asserts a fake
self-raised exception as if it were writer-level rejection. Verifier, store
and clock are declared SYNTHETIC fixtures - a fabricated proof minting a
capability demonstrates non-authority, never authentication.

All identifiers, refs, labels and keys are synthetic public test values.
"""

import os
import sqlite3
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import capability as cap  # noqa: E402

NOW = 1_700_000_000
TTL = 300
UID = 76


# ---------------------------------------------------------------------------
# Synthetic fixtures (declared non-authoritative)
# ---------------------------------------------------------------------------

class FakeVerifier:
    def __init__(self, identity=None, error=None):
        self.identity = identity
        self.error = error
        self.calls = 0

    def verify(self, proof):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.identity


class FakeStore:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.reads = 0

    def read(self, owner_uid):
        self.reads += 1
        return self.snapshot


class FakeClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


class Exploding:
    """Any attribute access or call raises - proves the production stubs
    refuse BEFORE touching a dependency."""
    def __getattr__(self, name):
        raise AssertionError("dependency touched")

    def __call__(self, *a, **k):
        raise AssertionError("dependency touched")


def make_identity(**kw):
    base = dict(
        owner_uid=UID, channel=cap.Channel.WEB,
        surface=cap.Surface.PRIVATE, issuer_id="synthetic-issuer",
        session_binding="synthetic-session", audience_id=None,
        lifetime_id=1, origin=cap.Origin.DIRECT,
    )
    base.update(kw)
    return cap.Identity(**base)


def make_proof(identity=None, label="synthetic-proof-label"):
    return cap.ModelProof(identity=identity or make_identity(),
                          proof_label=label)


def make_request(**kw):
    base = dict(
        operation_id="op-synthetic-1",
        operation_class=cap.OperationClass.EXTRACT,
        required_scopes=frozenset({cap.Scope.MEMORY_EXTRACT}),
        captured_epoch=7, requested_audience_id=None,
        permitted_information_ref=None,
        extraction_intent=cap.ExtractionIntent.EXPLICIT,
        group_action=None, sensitive=False,
    )
    base.update(kw)
    return cap.Request(**base)


def make_grant(scope, granted_at=NOW - 100, expires_at=None,
               per_op_required=False):
    return cap.Grant(scope=scope, granted_at=granted_at,
                     expires_at=expires_at, per_op_required=per_op_required)


def make_policy(extraction=cap.ExtractionPolicy.EXPLICIT_ONLY,
                group_write=cap.GroupEligibility.NEVER):
    return cap.Policy(extraction=extraction,
                      sharing=cap.SharingPolicy.EXPLICIT_PER_OP,
                      group_write=group_write)


def make_envelope(policy=None, issued_at=NOW - 100, expires_at=None,
                  revision=3, owner_uid=UID):
    return cap.DecisionEnvelope(
        owner_uid=owner_uid, policy=policy or make_policy(),
        issued_at=issued_at, expires_at=expires_at, revision=revision)


def make_consent(**kw):
    base = dict(
        owner_uid=UID, operation_id="op-synthetic-1",
        operation_class=cap.OperationClass.SHARE,
        scopes=frozenset({cap.Scope.DATA_SHARE}),
        audience_id="aud-synthetic-1",
        permitted_information_ref="ref-synthetic-1",
        issued_at=NOW - 10, expires_at=NOW + 600, consent_generation=11,
    )
    base.update(kw)
    return cap.OperationConsent(**base)


def make_snapshot(**kw):
    base = dict(
        owner_uid=UID, lifetime_id=1, current_epoch=7, grants_generation=5,
        grants=(make_grant(cap.Scope.MEMORY_READ),
                make_grant(cap.Scope.MEMORY_WRITE),
                make_grant(cap.Scope.MEMORY_EXTRACT)),
        policy_revision=2, decision_revision=3,
        config=make_policy(), decision_envelope=make_envelope(),
        consent_generation=11, consents=(),
    )
    base.update(kw)
    return cap.Snapshot(**base)


def make_eval(identity=None, snapshot=None, now=NOW, verifier=None):
    return cap.ModelOnlyEvaluator(
        verifier or FakeVerifier(identity or make_identity()),
        FakeStore(snapshot or make_snapshot()),
        FakeClock(now), TTL)


def mint_ok():
    ev = make_eval()
    return ev, ev.mint_model(make_proof(), make_request())


class TestIdentityAndProof(unittest.TestCase):
    def test_mint_ok_explicit_extract(self):
        _, capability = mint_ok()
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)
        self.assertEqual(capability.granted_scopes,
                         frozenset({cap.Scope.MEMORY_EXTRACT}))
        self.assertEqual(capability.issued_at, NOW)
        self.assertEqual(capability.expires_at, NOW + TTL)
        self.assertEqual(capability.grants_generation, 5)
        self.assertEqual(capability.policy_revision, 2)
        self.assertEqual(capability.decision_revision, 3)
        self.assertEqual(capability.consent_generation, 11)

    def test_one_atomic_snapshot_read_per_call(self):
        store = FakeStore(make_snapshot())
        ev = cap.ModelOnlyEvaluator(FakeVerifier(make_identity()), store,
                                    FakeClock(NOW), TTL)
        capability = ev.mint_model(make_proof(), make_request())
        self.assertEqual(store.reads, 1)
        ev.recheck_model(capability, make_request())
        self.assertEqual(store.reads, 2)

    def test_verifier_error_refuses(self):
        ev = make_eval(verifier=FakeVerifier(error=cap.AuthError()))
        with self.assertRaises(cap.AuthError) as ctx:
            ev.mint_model(make_proof(), make_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.MISSING_IDENTITY)

    def test_verifier_non_identity_refuses(self):
        ev = make_eval(verifier=FakeVerifier(identity="not-an-identity"))
        with self.assertRaises(cap.AuthError):
            ev.mint_model(make_proof(), make_request())

    def test_malformed_identity_fields_refuse_at_construction(self):
        for bad in (dict(owner_uid=True), dict(owner_uid=-1),
                    dict(owner_uid=cap.BIGINT_MAX + 1),
                    dict(lifetime_id=0), dict(issuer_id="   "),
                    dict(channel="whatsapp"), dict(origin="DIRECT")):
            with self.assertRaises(cap.AuthError, msg=repr(bad)):
                make_identity(**bad)

    def test_fabricated_proof_is_synthetic_only(self):
        # A forged label still mints via the synthetic verifier fixture -
        # proving the proof is NOT authentication - and the production path
        # refuses the very capability that resulted.
        ev = make_eval()
        capability = ev.mint_model(make_proof(label="forged-fixture"),
                                   make_request())
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)
        with self.assertRaises(cap.ProductionStubRefused):
            cap.production_authorize(capability)


class TestRequestValidation(unittest.TestCase):
    def test_malformed_request_fields(self):
        for bad in (dict(operation_id="  "), dict(captured_epoch=True),
                    dict(captured_epoch=0),
                    dict(required_scopes=frozenset({"memory.read"})),
                    dict(required_scopes=frozenset()),
                    dict(sensitive=1),
                    dict(operation_class="EXTRACT")):
            with self.assertRaises(cap.ModelInputError, msg=repr(bad)):
                make_request(**bad)

    def test_group_action_rules(self):
        with self.assertRaises(cap.ModelInputError):
            make_request(operation_class=cap.OperationClass.GROUP_WRITE,
                         group_action=None)
        with self.assertRaises(cap.ModelInputError):
            make_request(group_action=cap.GroupAction.WRITE)

    def test_extraction_intent_rules(self):
        with self.assertRaises(cap.ModelInputError):
            make_request(extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE)
        with self.assertRaises(cap.ModelInputError):
            make_request(operation_class=cap.OperationClass.READ,
                         required_scopes=frozenset({cap.Scope.MEMORY_READ}),
                         captured_epoch=None,
                         extraction_intent=cap.ExtractionIntent.EXPLICIT)

    def test_share_requires_ref_and_audience(self):
        good = dict(operation_class=cap.OperationClass.SHARE,
                    required_scopes=frozenset({cap.Scope.DATA_SHARE}),
                    captured_epoch=None,
                    extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
                    requested_audience_id="aud-synthetic-1",
                    permitted_information_ref="ref-synthetic-1")
        for field in ("permitted_information_ref", "requested_audience_id"):
            bad = dict(good)
            bad[field] = None
            with self.assertRaises(cap.ModelInputError, msg=field):
                make_request(**bad)

    def test_caller_scope_omission_rejected(self):
        # derived set is authoritative; a narrower caller set is drift
        ev = make_eval()
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request(
                required_scopes=frozenset()))

    def test_caller_scope_widening_rejected(self):
        ev = make_eval()
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request(
                required_scopes=frozenset({cap.Scope.MEMORY_EXTRACT,
                                           cap.Scope.MEMORY_ERASE})))


class TestSnapshotBinding(unittest.TestCase):
    def test_cross_owner_snapshot_refused(self):
        ev = make_eval(snapshot=make_snapshot(owner_uid=UID + 1))
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request())

    def test_lifetime_mismatch_refused_no_resurrection_claim(self):
        ev = make_eval(snapshot=make_snapshot(lifetime_id=2))
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), make_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.REVOKED)

    def test_duplicate_grant_scopes_rejected(self):
        with self.assertRaises(cap.ModelInputError):
            make_snapshot(grants=(make_grant(cap.Scope.MEMORY_READ),
                                  make_grant(cap.Scope.MEMORY_READ)))


class TestSensitive(unittest.TestCase):
    def test_sensitive_flag_refused(self):
        ev = make_eval()
        with self.assertRaises(cap.SensitiveRefused) as ctx:
            ev.mint_model(make_proof(), make_request(sensitive=True))
        self.assertIs(ctx.exception.code, cap.ModelCode.TIER_DISABLED)

    def test_sensitive_class_refused(self):
        ev = make_eval()
        with self.assertRaises(cap.SensitiveRefused):
            ev.mint_model(make_proof(), make_request(
                operation_class=cap.OperationClass.SENSITIVE,
                required_scopes=frozenset({cap.Scope.MEMORY_SENSITIVE}),
                captured_epoch=None,
                extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE))

    def test_sensitive_scope_refused_even_with_full_grants(self):
        snap = make_snapshot(grants=tuple(
            make_grant(s) for s in cap.Scope))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.SensitiveRefused):
            ev.mint_model(make_proof(), make_request(
                operation_class=cap.OperationClass.SENSITIVE,
                required_scopes=frozenset({cap.Scope.MEMORY_SENSITIVE}),
                captured_epoch=None,
                extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE))

    def test_sensitive_scope_with_mismatched_class_is_malformed(self):
        # shared pipeline tier 1 (malformed) precedes tier 2 (sensitive):
        # memory.sensitive in required_scopes with a non-SENSITIVE class
        # fails derived-scope matching first - deterministic, documented
        ev = make_eval()
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request(
                required_scopes=frozenset({cap.Scope.MEMORY_EXTRACT,
                                           cap.Scope.MEMORY_SENSITIVE})))


class TestExpiryAndWindows(unittest.TestCase):
    def test_expired_envelope(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(expires_at=NOW - 1)))
        with self.assertRaises(cap.ExpiredRefused):
            ev.mint_model(make_proof(), make_request())

    def test_not_yet_valid_envelope(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(issued_at=NOW + 100)))
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request())

    def test_expired_required_grant(self):
        snap = make_snapshot(grants=(
            make_grant(cap.Scope.MEMORY_READ),
            make_grant(cap.Scope.MEMORY_EXTRACT, expires_at=NOW - 1)))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ExpiredRefused):
            ev.mint_model(make_proof(), make_request())

    def test_future_grant_is_missing_scope(self):
        snap = make_snapshot(grants=(
            make_grant(cap.Scope.MEMORY_READ),
            make_grant(cap.Scope.MEMORY_EXTRACT, granted_at=NOW + 100)))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ScopeRefused) as ctx:
            ev.mint_model(make_proof(), make_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.MISSING_SCOPE)

    def test_grant_window_rejected(self):
        with self.assertRaises(cap.ModelInputError):
            make_grant(cap.Scope.MEMORY_READ, expires_at=NOW - 200,
                       granted_at=NOW - 100)

    def test_missing_scope(self):
        snap = make_snapshot(grants=(make_grant(cap.Scope.MEMORY_READ),))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ScopeRefused):
            ev.mint_model(make_proof(), make_request())

    def test_capability_expiry_uses_earliest_relevant(self):
        snap = make_snapshot(
            grants=(make_grant(cap.Scope.MEMORY_EXTRACT,
                               expires_at=NOW + 50),),
            decision_envelope=make_envelope())
        _, capability = (lambda ev: (ev, ev.mint_model(
            make_proof(), make_request())))(make_eval(snapshot=snap))
        self.assertEqual(capability.expires_at, NOW + 50)

    def test_missing_envelope_or_config_safest_refusal(self):
        for snap in (make_snapshot(decision_envelope=None),
                     make_snapshot(config=None)):
            ev = make_eval(snapshot=snap)
            with self.assertRaises(cap.PolicyRefused) as ctx:
                ev.mint_model(make_proof(), make_request())
            self.assertIs(ctx.exception.code, cap.ModelCode.POLICY_REFUSED)

    def test_envelope_owner_mismatch(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(owner_uid=UID + 1)))
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request())

    def test_envelope_revision_stale(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(revision=99)))
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), make_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.STALE_POLICY)


class TestEpoch(unittest.TestCase):
    def test_mutation_epoch_required(self):
        ev = make_eval()
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), make_request(captured_epoch=None))
        self.assertIs(ctx.exception.code, cap.ModelCode.EPOCH)

    def test_epoch_mismatch(self):
        ev = make_eval()
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), make_request(captured_epoch=8))
        self.assertIs(ctx.exception.code, cap.ModelCode.EPOCH)

    def test_read_may_omit_epoch(self):
        ev = make_eval()
        capability = ev.mint_model(make_proof(), make_request(
            operation_class=cap.OperationClass.READ,
            required_scopes=frozenset({cap.Scope.MEMORY_READ}),
            captured_epoch=None,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE))
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)


class TestSharingConsent(unittest.TestCase):
    def share_request(self, **kw):
        base = dict(
            operation_class=cap.OperationClass.SHARE,
            required_scopes=frozenset({cap.Scope.DATA_SHARE}),
            captured_epoch=None,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            requested_audience_id="aud-synthetic-1",
            permitted_information_ref="ref-synthetic-1",
        )
        base.update(kw)
        return make_request(**base)

    def share_snapshot(self, **consent_kw):
        return make_snapshot(
            grants=(make_grant(cap.Scope.DATA_SHARE),),
            consents=(make_consent(**consent_kw),))

    def test_share_ok(self):
        ev = make_eval(snapshot=self.share_snapshot())
        capability = ev.mint_model(make_proof(), self.share_request())
        self.assertEqual(capability.granted_scopes,
                         frozenset({cap.Scope.DATA_SHARE}))
        self.assertEqual(capability.expires_at, NOW + TTL)

    def test_share_missing_consent(self):
        ev = make_eval(snapshot=make_snapshot(
            grants=(make_grant(cap.Scope.DATA_SHARE),)))
        with self.assertRaises(cap.ConsentRefused) as ctx:
            ev.mint_model(make_proof(), self.share_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.MISSING_CONSENT)

    def test_standing_grant_alone_never_satisfies_sharing(self):
        # full standing grants but no per-op consent => MISSING_CONSENT
        snap = make_snapshot(grants=tuple(make_grant(s) for s in cap.Scope
                                          if s is not cap.Scope.MEMORY_SENSITIVE))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(), self.share_request())

    def test_consent_wrong_operation_class(self):
        snap = self.share_snapshot(
            operation_class=cap.OperationClass.READ)
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(), self.share_request())

    def test_consent_wrong_scope(self):
        snap = self.share_snapshot(
            scopes=frozenset({cap.Scope.MEMORY_READ}))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(), self.share_request())

    def test_consent_wrong_audience(self):
        snap = self.share_snapshot(audience_id="aud-synthetic-OTHER")
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(), self.share_request())

    def test_consent_wrong_ref(self):
        snap = self.share_snapshot(
            permitted_information_ref="ref-synthetic-OTHER")
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(), self.share_request())

    def test_consent_expired(self):
        snap = self.share_snapshot(expires_at=NOW - 1)
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ExpiredRefused):
            ev.mint_model(make_proof(), self.share_request())

    def test_consent_generation_mismatch_revoked(self):
        snap = self.share_snapshot(consent_generation=10)
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), self.share_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.REVOKED)

    def test_origin_ambiguity_refuses_share(self):
        for origin in (cap.Origin.ANONYMOUS, cap.Origin.SENDER_CHAT,
                       cap.Origin.FORWARDED_AMBIGUOUS):
            ev = make_eval(identity=make_identity(origin=origin),
                           snapshot=self.share_snapshot())
            with self.assertRaises(cap.PolicyRefused, msg=origin.value):
                ev.mint_model(make_proof(make_identity(origin=origin)),
                              self.share_request())


class TestExtractionAxis(unittest.TestCase):
    def auto_request(self):
        return make_request(
            extraction_intent=cap.ExtractionIntent.AUTOMATIC)

    def test_explicit_intent_allowed_under_explicit_only(self):
        _, capability = mint_ok()
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)

    def test_automatic_refused_under_explicit_only(self):
        ev = make_eval()
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(), self.auto_request())

    def test_automatic_allowed_under_guarded_auto(self):
        policy = make_policy(extraction=cap.ExtractionPolicy.GUARDED_AUTO)
        snap = make_snapshot(config=policy,
                             decision_envelope=make_envelope(policy=policy))
        ev = make_eval(snapshot=snap)
        capability = ev.mint_model(make_proof(), self.auto_request())
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)

    def test_automatic_refused_when_envelope_disagrees(self):
        snap = make_snapshot(
            config=make_policy(extraction=cap.ExtractionPolicy.GUARDED_AUTO),
            decision_envelope=make_envelope(
                policy=make_policy(cap.ExtractionPolicy.EXPLICIT_ONLY)))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(), self.auto_request())

    def test_sharing_unaffected_by_extraction_policy(self):
        snap = make_snapshot(
            grants=(make_grant(cap.Scope.DATA_SHARE),),
            consents=(make_consent(),))
        ev = make_eval(snapshot=snap)
        capability = ev.mint_model(make_proof(), TestSharingConsent().share_request())
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)


class TestGroupAxis(unittest.TestCase):
    def group_identity(self, origin=cap.Origin.DIRECT):
        return make_identity(surface=cap.Surface.GROUP, origin=origin,
                             audience_id="group-synthetic-1")

    def group_write_request(self, action=cap.GroupAction.WRITE):
        scopes = {cap.Scope.MEMORY_GROUP_WRITE}
        scopes.add(cap.Scope.MEMORY_WRITE if action is cap.GroupAction.WRITE
                   else cap.Scope.MEMORY_EXTRACT)
        return make_request(
            operation_class=cap.OperationClass.GROUP_WRITE,
            required_scopes=frozenset(scopes),
            requested_audience_id="group-synthetic-1",
            extraction_intent=(cap.ExtractionIntent.EXPLICIT
                               if action is cap.GroupAction.EXTRACT
                               else cap.ExtractionIntent.NOT_APPLICABLE),
            group_action=action,
            permitted_information_ref="ref-synthetic-g1")

    def group_source_request(self):
        return make_request(
            required_scopes=frozenset({cap.Scope.MEMORY_WRITE,
                                       cap.Scope.MEMORY_GROUP_WRITE}),
            requested_audience_id="group-synthetic-1",
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            operation_class=cap.OperationClass.WRITE,
            permitted_information_ref="ref-synthetic-g1")

    def group_snapshot(self, eligibility=cap.GroupEligibility.DEFAULT_OFF_OPT_IN,
                       consent_class=cap.OperationClass.GROUP_WRITE,
                       consent_scopes=None):
        policy = make_policy(group_write=eligibility)
        if consent_scopes is None:
            consent_scopes = frozenset({cap.Scope.MEMORY_GROUP_WRITE,
                                        cap.Scope.MEMORY_WRITE})
        consent = make_consent(
            operation_class=consent_class, scopes=consent_scopes,
            audience_id="group-synthetic-1",
            permitted_information_ref="ref-synthetic-g1")
        return make_snapshot(
            grants=(make_grant(cap.Scope.MEMORY_WRITE),
                    make_grant(cap.Scope.MEMORY_EXTRACT),
                    make_grant(cap.Scope.MEMORY_GROUP_WRITE)),
            config=policy, decision_envelope=make_envelope(policy=policy),
            consents=(consent,))

    def test_group_write_class_ok(self):
        ev = make_eval(identity=self.group_identity(),
                       snapshot=self.group_snapshot())
        capability = ev.mint_model(make_proof(self.group_identity()),
                                   self.group_write_request())
        self.assertIn(cap.Scope.MEMORY_WRITE, capability.granted_scopes)
        self.assertIn(cap.Scope.MEMORY_GROUP_WRITE,
                      capability.granted_scopes)

    def test_group_write_class_refused_under_never(self):
        ev = make_eval(identity=self.group_identity(),
                       snapshot=self.group_snapshot(
                           eligibility=cap.GroupEligibility.NEVER))
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(self.group_identity()),
                          self.group_write_request())

    def test_group_write_missing_underlying_scope_is_drift(self):
        # caller omitted the underlying memory.write scope => derived
        # mismatch => MALFORMED (no silent repair)
        ev = make_eval(identity=self.group_identity(),
                       snapshot=self.group_snapshot())
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(self.group_identity()),
                          make_request(
                              operation_class=cap.OperationClass.GROUP_WRITE,
                              required_scopes=frozenset(
                                  {cap.Scope.MEMORY_GROUP_WRITE}),
                              group_action=cap.GroupAction.WRITE,
                              permitted_information_ref="ref-synthetic-g1"))

    def test_group_write_missing_consent(self):
        snap = self.group_snapshot(consent_scopes=frozenset(
            {cap.Scope.MEMORY_WRITE}))
        ev = make_eval(identity=self.group_identity(), snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(self.group_identity()),
                          self.group_write_request())

    def test_group_source_write_allowed_with_consent(self):
        snap = self.group_snapshot(
            consent_class=cap.OperationClass.WRITE)
        ev = make_eval(identity=self.group_identity(), snapshot=snap)
        capability = ev.mint_model(make_proof(self.group_identity()),
                                   self.group_source_request())
        self.assertIn(cap.Scope.MEMORY_GROUP_WRITE,
                      capability.granted_scopes)

    def test_group_source_write_needs_group_write_consent(self):
        snap = self.group_snapshot(
            consent_class=cap.OperationClass.WRITE,
            consent_scopes=frozenset({cap.Scope.MEMORY_WRITE}))
        ev = make_eval(identity=self.group_identity(), snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(self.group_identity()),
                          self.group_source_request())

    def test_eligibility_never_refuses_group_source_config(self):
        # peer clarification on addendum 3/4: NEVER is the owner's hard NO
        # surface-wide - a group-source write refuses regardless of which
        # operation class names it
        snap = self.group_snapshot(
            eligibility=cap.GroupEligibility.NEVER,
            consent_class=cap.OperationClass.WRITE)
        ev = make_eval(identity=self.group_identity(), snapshot=snap)
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(self.group_identity()),
                          self.group_source_request())

    def test_eligibility_never_refuses_group_source_envelope(self):
        policy = make_policy(group_write=cap.GroupEligibility.DEFAULT_OFF_OPT_IN)
        env_policy = make_policy(group_write=cap.GroupEligibility.NEVER)
        consent = make_consent(
            operation_class=cap.OperationClass.WRITE,
            scopes=frozenset({cap.Scope.MEMORY_GROUP_WRITE,
                              cap.Scope.MEMORY_WRITE}),
            audience_id="group-synthetic-1",
            permitted_information_ref="ref-synthetic-g1")
        snap = make_snapshot(
            grants=(make_grant(cap.Scope.MEMORY_WRITE),
                    make_grant(cap.Scope.MEMORY_GROUP_WRITE)),
            config=policy, decision_envelope=make_envelope(policy=env_policy),
            consents=(consent,))
        ev = make_eval(identity=self.group_identity(), snapshot=snap)
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(self.group_identity()),
                          self.group_source_request())

    def test_group_source_allowed_under_default_off_opt_in(self):
        snap = self.group_snapshot(
            eligibility=cap.GroupEligibility.DEFAULT_OFF_OPT_IN,
            consent_class=cap.OperationClass.WRITE)
        ev = make_eval(identity=self.group_identity(), snapshot=snap)
        capability = ev.mint_model(make_proof(self.group_identity()),
                                   self.group_source_request())
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)

    def test_group_source_scope_omission_rejected(self):
        ev = make_eval(identity=self.group_identity(),
                       snapshot=self.group_snapshot(
                           consent_class=cap.OperationClass.WRITE))
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(self.group_identity()),
                          make_request(
                              operation_class=cap.OperationClass.WRITE,
                              required_scopes=frozenset(
                                  {cap.Scope.MEMORY_WRITE}),
                              extraction_intent=
                              cap.ExtractionIntent.NOT_APPLICABLE,
                              permitted_information_ref="ref-synthetic-g1"))

    def test_group_source_ambiguous_origin_refused(self):
        snap = self.group_snapshot(consent_class=cap.OperationClass.WRITE)
        ident = self.group_identity(origin=cap.Origin.SENDER_CHAT)
        ev = make_eval(identity=ident, snapshot=snap)
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(ident), self.group_source_request())

    def test_private_write_never_needs_sharing_consent(self):
        ev = make_eval()
        capability = ev.mint_model(make_proof(), make_request(
            operation_class=cap.OperationClass.WRITE,
            required_scopes=frozenset({cap.Scope.MEMORY_WRITE}),
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE))
        self.assertEqual(capability.granted_scopes,
                         frozenset({cap.Scope.MEMORY_WRITE}))


class TestPerOpGrantConsent(unittest.TestCase):
    def test_per_op_required_grant_needs_consent(self):
        snap = make_snapshot(grants=(
            make_grant(cap.Scope.MEMORY_EXTRACT, per_op_required=True),))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConsentRefused):
            ev.mint_model(make_proof(), make_request())

    def test_per_op_required_grant_with_consent(self):
        consent = make_consent(
            operation_class=cap.OperationClass.EXTRACT,
            scopes=frozenset({cap.Scope.MEMORY_EXTRACT}), audience_id=None,
            permitted_information_ref="ref-synthetic-x")
        snap = make_snapshot(
            grants=(make_grant(cap.Scope.MEMORY_EXTRACT,
                               per_op_required=True),),
            consents=(consent,))
        ev = make_eval(snapshot=snap)
        request = make_request(permitted_information_ref="ref-synthetic-x")
        capability = ev.mint_model(make_proof(), request)
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)


class TestRecheck(unittest.TestCase):
    def test_recheck_ok_returns_authority_free_decision(self):
        ev, capability = mint_ok()
        decision = ev.recheck_model(capability, make_request())
        self.assertIs(decision.status, cap.ModelStatus.MODEL_ONLY)
        self.assertTrue(decision.allowed)
        self.assertIs(decision.code, cap.ModelCode.MODEL_ALLOWED)

    def test_recheck_expired_capability(self):
        ev, capability = mint_ok()
        ev._clock = FakeClock(NOW + TTL)
        with self.assertRaises(cap.ExpiredRefused):
            ev.recheck_model(capability, make_request())

    def test_recheck_request_drift_fields(self):
        for field, value in (
                ("operation_id", "op-synthetic-2"),
                ("requested_audience_id", "aud-synthetic-9"),
                ("permitted_information_ref", "ref-synthetic-9"),
                ("extraction_intent", cap.ExtractionIntent.AUTOMATIC),
                ("captured_epoch", 7),  # placeholder replaced below
        ):
            if field == "captured_epoch":
                continue  # epoch drift has its own assertion below
            ev, capability = mint_ok()
            kw = {field: value}
            if field == "extraction_intent":
                policy = make_policy(
                    extraction=cap.ExtractionPolicy.GUARDED_AUTO)
                ev = make_eval(snapshot=make_snapshot(
                    config=policy,
                    decision_envelope=make_envelope(policy=policy)))
                capability = ev.mint_model(make_proof(), make_request())
            with self.assertRaises(cap.ConflictError, msg=field) as ctx:
                ev.recheck_model(capability, make_request(**kw))
            self.assertIs(ctx.exception.code, cap.ModelCode.REQUEST_DRIFT)

    def test_recheck_sensitive_drift(self):
        # shared staging pipeline: tier 2 (sensitive) precedes tier 4
        # (drift), so a drifted sensitive=True request refuses TIER_DISABLED
        # deterministically - never a successful decision
        ev, capability = mint_ok()
        with self.assertRaises(cap.SensitiveRefused) as ctx:
            ev.recheck_model(capability, make_request(sensitive=True))
        self.assertIs(ctx.exception.code, cap.ModelCode.TIER_DISABLED)

    def test_recheck_generation_drifts(self):
        cases = (
            (dict(grants_generation=6), cap.ModelCode.REVOKED),
            (dict(policy_revision=9), cap.ModelCode.STALE_POLICY),
            (dict(decision_revision=9), cap.ModelCode.STALE_POLICY),
            (dict(consent_generation=12), cap.ModelCode.REVOKED),
            (dict(current_epoch=8), cap.ModelCode.EPOCH),
        )
        for snap_kw, expected in cases:
            store = FakeStore(make_snapshot())
            ev = cap.ModelOnlyEvaluator(FakeVerifier(make_identity()),
                                        store, FakeClock(NOW), TTL)
            capability = ev.mint_model(make_proof(), make_request())
            store.snapshot = make_snapshot(**snap_kw)
            with self.assertRaises(cap.ConflictError,
                                   msg=repr(snap_kw)) as ctx:
                ev.recheck_model(capability, make_request())
            self.assertIs(ctx.exception.code, expected)

    def test_recheck_grant_expired_after_mint(self):
        snap = make_snapshot(grants=(
            make_grant(cap.Scope.MEMORY_EXTRACT, expires_at=NOW + 50),))
        store = FakeStore(snap)
        clock = FakeClock(NOW)
        ev = cap.ModelOnlyEvaluator(FakeVerifier(make_identity()),
                                    store, clock, TTL)
        capability = ev.mint_model(make_proof(), make_request())
        self.assertEqual(capability.expires_at, NOW + 50)
        clock._now = NOW + 60
        with self.assertRaises(cap.ExpiredRefused):
            ev.recheck_model(capability, make_request())

    def test_idempotent_same_operation_recheck(self):
        ev, capability = mint_ok()
        for _ in range(3):
            decision = ev.recheck_model(capability, make_request())
            self.assertTrue(decision.allowed)


class TestProductionStubs(unittest.TestCase):
    def test_stubs_refuse_before_any_dependency(self):
        for stub in (cap.production_mint, cap.production_export,
                     cap.production_load, cap.production_authorize):
            with self.assertRaises(cap.ProductionStubRefused,
                                   msg=stub.__name__) as ctx:
                stub(Exploding(), Exploding(), Exploding())
            self.assertIs(ctx.exception.code, cap.ModelCode.MODEL_ONLY)

    def test_production_authorize_refuses_valid_looking_capability(self):
        _, capability = mint_ok()
        with self.assertRaises(cap.ProductionStubRefused):
            cap.production_authorize(capability)
        with self.assertRaises(cap.ProductionStubRefused):
            cap.production_load(b"serialized-synthetic-capability")


class TestCapRefusedType(unittest.TestCase):
    def test_resulting_count_validated(self):
        err = cap.CapRefused(3)
        self.assertEqual(err.resulting_count, 3)
        for bad in (True, -1, "3", 1.5):
            with self.assertRaises(cap.ModelInputError, msg=repr(bad)):
                cap.CapRefused(bad)


class TestImmutabilityAndNoEcho(unittest.TestCase):
    def test_capability_frozen(self):
        _, capability = mint_ok()
        with self.assertRaises(FrozenInstanceError):
            capability.expires_at = 0
        self.assertIsInstance(capability.granted_scopes, frozenset)

    def test_request_frozen(self):
        request = make_request()
        with self.assertRaises(FrozenInstanceError):
            request.sensitive = True

    def test_error_messages_carry_no_fixture_content(self):
        secrets = ("synthetic-proof-label", "ref-synthetic-1",
                   "synthetic-session", "aud-synthetic-1",
                   "op-synthetic-1")
        errors = []
        ev = make_eval(snapshot=make_snapshot(
            grants=(make_grant(cap.Scope.DATA_SHARE),)))
        try:
            ev.mint_model(make_proof(),
                          TestSharingConsent().share_request())
        except cap.CapabilityModelError as exc:
            errors.append(exc)
        ev2 = make_eval()
        try:
            ev2.mint_model(make_proof(), make_request(sensitive=True))
        except cap.CapabilityModelError as exc:
            errors.append(exc)
        self.assertEqual(len(errors), 2)
        for exc in errors:
            text = str(exc)
            for secret in secrets:
                self.assertNotIn(secret, text)


class TestGroupNeverVariants(unittest.TestCase):
    """Item 1: NEVER refuses ALL group-source memory writes/extraction -
    GROUP_WRITE with either underlying action AND Surface.GROUP WRITE/EXTRACT,
    config OR envelope NEVER, even with favorable grants/consent."""

    def _snap(self, config_elig, envelope_elig, consent_class, consent_scopes):
        config_policy = make_policy(group_write=config_elig)
        envelope_policy = make_policy(group_write=envelope_elig)
        consent = make_consent(
            operation_class=consent_class, scopes=consent_scopes,
            audience_id="group-synthetic-1",
            permitted_information_ref="ref-synthetic-g1")
        return make_snapshot(
            grants=(make_grant(cap.Scope.MEMORY_WRITE),
                    make_grant(cap.Scope.MEMORY_EXTRACT),
                    make_grant(cap.Scope.MEMORY_GROUP_WRITE)),
            config=config_policy,
            decision_envelope=make_envelope(policy=envelope_policy),
            consents=(consent,))

    N = cap.GroupEligibility.NEVER
    O = cap.GroupEligibility.DEFAULT_OFF_OPT_IN
    GW = frozenset({cap.Scope.MEMORY_GROUP_WRITE, cap.Scope.MEMORY_WRITE})
    GX = frozenset({cap.Scope.MEMORY_GROUP_WRITE, cap.Scope.MEMORY_EXTRACT})

    def _attempt(self, snap, request):
        ident = TestGroupAxis().group_identity()
        ev = make_eval(identity=ident, snapshot=snap)
        return ev.mint_model(make_proof(ident), request)

    def _gw_request(self, action):
        return TestGroupAxis().group_write_request(action)

    def _gs_request(self, underlying):
        scope = (cap.Scope.MEMORY_WRITE if underlying == "write"
                 else cap.Scope.MEMORY_EXTRACT)
        return make_request(
            operation_class=(cap.OperationClass.WRITE
                             if underlying == "write"
                             else cap.OperationClass.EXTRACT),
            required_scopes=frozenset({scope, cap.Scope.MEMORY_GROUP_WRITE}),
            requested_audience_id="group-synthetic-1",
            extraction_intent=(cap.ExtractionIntent.NOT_APPLICABLE
                               if underlying == "write"
                               else cap.ExtractionIntent.EXPLICIT),
            permitted_information_ref="ref-synthetic-g1")

    def test_never_refuses_every_group_variant(self):
        variants = []
        for elig_side in ("config", "envelope", "both"):
            cfg = self.N if elig_side in ("config", "both") else self.O
            env = self.N if elig_side in ("envelope", "both") else self.O
            variants.append((elig_side + "/group_write-write", cfg, env,
                             cap.OperationClass.GROUP_WRITE, self.GW,
                             self._gw_request(cap.GroupAction.WRITE)))
            variants.append((elig_side + "/group_write-extract", cfg, env,
                             cap.OperationClass.GROUP_WRITE, self.GX,
                             self._gw_request(cap.GroupAction.EXTRACT)))
            variants.append((elig_side + "/group-source-write", cfg, env,
                             cap.OperationClass.WRITE, self.GW,
                             self._gs_request("write")))
            variants.append((elig_side + "/group-source-extract", cfg, env,
                             cap.OperationClass.EXTRACT, self.GX,
                             self._gs_request("extract")))
        self.assertEqual(len(variants), 12)
        for name, cfg, env, consent_class, consent_scopes, request in variants:
            snap = self._snap(cfg, env, consent_class, consent_scopes)
            with self.assertRaises(cap.PolicyRefused, msg=name):
                self._attempt(snap, request)

    def test_opt_in_allows_every_group_variant_with_consent(self):
        variants = (
            (cap.OperationClass.GROUP_WRITE, self.GW,
             self._gw_request(cap.GroupAction.WRITE)),
            (cap.OperationClass.GROUP_WRITE, self.GX,
             self._gw_request(cap.GroupAction.EXTRACT)),
            (cap.OperationClass.WRITE, self.GW, self._gs_request("write")),
            (cap.OperationClass.EXTRACT, self.GX, self._gs_request("extract")),
        )
        names = ("gw-write", "gw-extract", "gs-write", "gs-extract")
        for name, (consent_class, consent_scopes, request) in zip(
                names, variants):
            snap = self._snap(self.O, self.O, consent_class, consent_scopes)
            capability = self._attempt(snap, request)
            self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY,
                          msg=name)


class TestGroupAudienceBinding(unittest.TestCase):
    """Item 2: group operations bind to the identity's concrete source
    group; None/mismatch refuses; disclosure destination may be distinct
    but is explicitly bound, never inferred."""

    def _group_identity(self, **kw):
        base = dict(surface=cap.Surface.GROUP,
                    audience_id="group-synthetic-1")
        base.update(kw)
        return make_identity(**base)

    def _group_snap(self):
        return TestGroupAxis().group_snapshot(
            consent_class=cap.OperationClass.WRITE)

    def _request(self, audience):
        req = TestGroupAxis().group_source_request()
        return make_request(
            operation_class=req.operation_class,
            required_scopes=req.required_scopes,
            requested_audience_id=audience,
            captured_epoch=req.captured_epoch,
            extraction_intent=req.extraction_intent,
            permitted_information_ref="ref-synthetic-g1")

    def test_group_surface_identity_requires_source_id(self):
        with self.assertRaises(cap.AuthError):
            make_identity(surface=cap.Surface.GROUP, audience_id=None)

    def test_group_write_none_audience_refused(self):
        ident = self._group_identity()
        ev = make_eval(identity=ident, snapshot=self._group_snap())
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(ident), self._request(None))

    def test_group_write_mismatched_audience_refused(self):
        ident = self._group_identity()  # group-synthetic-1
        ev = make_eval(identity=ident, snapshot=self._group_snap())
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(ident),
                          self._request("group-synthetic-OTHER"))

    def test_group_write_bound_audience_allowed(self):
        ident = self._group_identity()
        ev = make_eval(identity=ident, snapshot=self._group_snap())
        capability = ev.mint_model(make_proof(ident),
                                   self._request("group-synthetic-1"))
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)

    def test_group_disclosure_distinct_destination_allowed(self):
        # SHARE from a group source names a distinct, explicitly bound
        # destination audience; the consent binds to that destination
        ident = self._group_identity()
        snap = make_snapshot(
            grants=(make_grant(cap.Scope.DATA_SHARE),),
            consents=(make_consent(
                operation_class=cap.OperationClass.SHARE,
                scopes=frozenset({cap.Scope.DATA_SHARE}),
                audience_id="aud-synthetic-dest",
                permitted_information_ref="ref-synthetic-1"),))
        ev = make_eval(identity=ident, snapshot=snap)
        request = make_request(
            operation_class=cap.OperationClass.SHARE,
            required_scopes=frozenset({cap.Scope.DATA_SHARE}),
            captured_epoch=None,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            requested_audience_id="aud-synthetic-dest",
            permitted_information_ref="ref-synthetic-1")
        capability = ev.mint_model(make_proof(ident), request)
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)


class TestClockAndTtlGuards(unittest.TestCase):
    """Item 3: future-issued caps refused; exact signed BIGINT clock/TTL;
    typed expiry-addition overflow refuses before construction."""

    def test_future_issued_capability_refused(self):
        ev, capability = mint_ok()
        ev._clock = FakeClock(NOW - 1)  # now < cap.issued_at
        with self.assertRaises(cap.ModelInputError):
            ev.recheck_model(capability, make_request())

    def test_invalid_ttl_values(self):
        for bad in (0, -1, True, 1.5, "300", cap.BIGINT_MAX + 1):
            with self.assertRaises(cap.ModelInputError, msg=repr(bad)):
                cap.ModelOnlyEvaluator(FakeVerifier(make_identity()),
                                       FakeStore(make_snapshot()),
                                       FakeClock(NOW), bad)

    def test_invalid_clock_values(self):
        for bad in (True, -1, 1.5, "now", cap.BIGINT_MAX + 1):
            ev = make_eval(now=NOW)
            ev._clock = FakeClock(bad)
            with self.assertRaises(cap.ModelInputError, msg=repr(bad)):
                ev.mint_model(make_proof(), make_request())

    def test_expiry_addition_overflow_refused(self):
        ev = make_eval(now=cap.BIGINT_MAX - 10)
        ev._ttl = 300
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request(captured_epoch=7))


class TestTypedShapeGuards(unittest.TestCase):
    """Item 4: typed collection guards before frozenset/tuple; no raw
    TypeError; public errors ignore arbitrary prose."""

    def test_none_collections_refused_typed(self):
        with self.assertRaises(cap.ModelInputError):
            make_request(required_scopes=None)
        with self.assertRaises(cap.ModelInputError):
            make_consent(scopes=None)
        with self.assertRaises(cap.ModelInputError):
            make_snapshot(grants=None)
        with self.assertRaises(cap.ModelInputError):
            make_snapshot(consents=None)
        ev, capability = mint_ok()
        with self.assertRaises(cap.ModelInputError):
            cap.Capability(
                status=cap.ModelStatus.MODEL_ONLY,
                identity=capability.identity, request=capability.request,
                granted_scopes=None, grants_generation=5, policy_revision=2,
                decision_revision=3, consent_generation=11,
                issued_at=NOW, expires_at=NOW + TTL)

    def test_string_collections_refused_typed(self):
        with self.assertRaises(cap.ModelInputError):
            make_request(required_scopes="memory.extract")
        with self.assertRaises(cap.ModelInputError):
            make_snapshot(grants="grant")

    def test_public_errors_ignore_arbitrary_prose(self):
        secret = "secret-text-fixture-content"
        cases = [
            cap.AuthError(secret), cap.ModelInputError(secret),
            cap.SensitiveRefused(secret), cap.ExpiredRefused(secret),
            cap.ScopeRefused(secret), cap.ConsentRefused(secret),
            cap.PolicyRefused(secret), cap.CapRefused(0),
            cap.ProductionStubRefused(secret),
        ]
        for code in (cap.ModelCode.REVOKED, cap.ModelCode.STALE_POLICY,
                     cap.ModelCode.EPOCH, cap.ModelCode.REQUEST_DRIFT):
            cases.append(cap.ConflictError(code))
        for err in cases:
            text = str(err)
            self.assertNotIn(secret, text, msg=type(err).__name__)
            self.assertTrue(text, msg=type(err).__name__)
            self.assertIsNotNone(err.code, msg=type(err).__name__)
        # conflict messages carry only the closed code value
        err = cap.ConflictError(cap.ModelCode.REVOKED)
        self.assertEqual(str(err), "conflict: REVOKED")
        with self.assertRaises(cap.ModelInputError):
            cap.ConflictError(cap.ModelCode.MODEL_ALLOWED)


class TestPrecedenceCollisions(unittest.TestCase):
    """Item 6: collision probes through the shared staging pipeline -
    not just isolated errors."""

    def test_sensitive_beats_lifetime_revoked(self):
        ev = make_eval(snapshot=make_snapshot(lifetime_id=2))
        with self.assertRaises(cap.SensitiveRefused):
            ev.mint_model(make_proof(), make_request(sensitive=True))

    def test_malformed_envelope_owner_beats_sensitive(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(owner_uid=UID + 1)))
        with self.assertRaises(cap.ModelInputError):
            ev.mint_model(make_proof(), make_request(sensitive=True))

    def test_sensitive_beats_expired_envelope(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(expires_at=NOW - 1)))
        with self.assertRaises(cap.SensitiveRefused):
            ev.mint_model(make_proof(), make_request(sensitive=True))

    def test_expired_envelope_beats_stale_revision(self):
        ev = make_eval(snapshot=make_snapshot(
            decision_envelope=make_envelope(expires_at=NOW - 1,
                                            revision=99)))
        with self.assertRaises(cap.ExpiredRefused):
            ev.mint_model(make_proof(), make_request())

    def test_expired_envelope_beats_missing_scope(self):
        snap = make_snapshot(grants=(),
                             decision_envelope=make_envelope(
                                 expires_at=NOW - 1))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ExpiredRefused):
            ev.mint_model(make_proof(), make_request())

    def test_stale_revision_beats_missing_scope(self):
        snap = make_snapshot(grants=(),
                             decision_envelope=make_envelope(revision=99))
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), make_request())
        self.assertIs(ctx.exception.code, cap.ModelCode.STALE_POLICY)

    def test_epoch_drift_beats_missing_scope(self):
        snap = make_snapshot(grants=())
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.mint_model(make_proof(), make_request(captured_epoch=9))
        self.assertIs(ctx.exception.code, cap.ModelCode.EPOCH)

    def test_recheck_expired_beats_drift(self):
        ev, capability = mint_ok()
        ev._clock = FakeClock(NOW + TTL)
        with self.assertRaises(cap.ExpiredRefused):
            ev.recheck_model(capability, make_request(operation_id="op-x"))

    def test_recheck_stale_beats_drift(self):
        store = FakeStore(make_snapshot())
        ev = cap.ModelOnlyEvaluator(FakeVerifier(make_identity()),
                                    store, FakeClock(NOW), TTL)
        capability = ev.mint_model(make_proof(), make_request())
        store.snapshot = make_snapshot(policy_revision=9)
        with self.assertRaises(cap.ConflictError) as ctx:
            ev.recheck_model(capability, make_request(operation_id="op-x"))
        self.assertIs(ctx.exception.code, cap.ModelCode.STALE_POLICY)

    def test_policy_refusal_beats_missing_scope(self):
        snap = make_snapshot(grants=(), decision_envelope=None)
        ev = make_eval(snapshot=snap)
        with self.assertRaises(cap.PolicyRefused):
            ev.mint_model(make_proof(), make_request())


class TestSqliteStore(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            os.path.dirname(__file__), "test_pr3_model.sqlite3")
        if os.path.exists(self.path):
            os.remove(self.path)
        self.store = cap.SqliteModelSnapshotStore(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _write(self, sql, params=()):
        con = sqlite3.connect(self.path)
        try:
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def _seed_ok(self):
        self._write(
            "INSERT INTO model_snapshot VALUES (?, ?, ?, ?, ?, ?, ?)",
            (UID, 1, 7, 5, 2, 3, 11))
        for scope in ("memory.read", "memory.write", "memory.extract"):
            self._write(
                "INSERT INTO model_grants VALUES (?, ?, ?, ?, ?)",
                (UID, scope, NOW - 100, None, 0))
        self._write(
            "INSERT INTO model_policy VALUES (?, ?, ?, ?)",
            (UID, "EXPLICIT_ONLY", "EXPLICIT_PER_OP", "NEVER"))
        self._write(
            "INSERT INTO model_envelope VALUES (?, ?, ?, ?, ?, ?, ?)",
            (UID, "EXPLICIT_ONLY", "EXPLICIT_PER_OP", "NEVER",
             NOW - 100, None, 3))

    def test_roundtrip_read(self):
        self._seed_ok()
        snap = self.store.read(UID)
        self.assertEqual(snap.owner_uid, UID)
        self.assertEqual(snap.current_epoch, 7)
        self.assertEqual(len(snap.grants), 3)
        self.assertIsNotNone(snap.config)
        self.assertIsNotNone(snap.decision_envelope)
        # and the evaluator accepts the sqlite-backed snapshot
        ev = cap.ModelOnlyEvaluator(FakeVerifier(make_identity()),
                                    self.store, FakeClock(NOW), TTL)
        capability = ev.mint_model(make_proof(), make_request())
        self.assertIs(capability.status, cap.ModelStatus.MODEL_ONLY)

    def test_missing_snapshot_row(self):
        with self.assertRaises(cap.ModelInputError):
            self.store.read(UID)

    def test_malformed_scope_text_rejected_at_read(self):
        self._seed_ok()
        self._write("INSERT INTO model_grants VALUES (?, ?, ?, ?, ?)",
                    (UID, "memory.*", NOW - 100, None, 0))
        with self.assertRaises(cap.ModelInputError):
            self.store.read(UID)

    def test_malformed_policy_enum_rejected_at_read(self):
        self._seed_ok()
        self._write("UPDATE model_policy SET extraction = 'ALWAYS'")
        with self.assertRaises(cap.ModelInputError):
            self.store.read(UID)

    def test_malformed_bool_rejected_at_read(self):
        self._seed_ok()
        self._write("UPDATE model_grants SET per_op_required = 7")
        with self.assertRaises(cap.ModelInputError):
            self.store.read(UID)

    def test_operational_failure_propagates_unwrapped(self):
        os.remove(self.path)
        self._write  # noqa - store schema gone with the file
        store = cap.SqliteModelSnapshotStore(self.path)
        con = sqlite3.connect(self.path)
        con.execute("DROP TABLE model_snapshot")
        con.commit()
        con.close()
        with self.assertRaises(sqlite3.Error):
            store.read(UID)


if __name__ == "__main__":
    unittest.main()
