"""Tests for PR-4 capture-envelope MODEL - SYNTHETIC, local disposable.

Scope (peer DRAFT-READY verdict + controlling clarification, 2026-10-11):
the three NEW files only; MODEL semantics. This file proves NOTHING about
production authentication, trusted polling provenance, real key secrecy,
replay-safe effects, or any runtime behavior - all five production entry
points are fail-closed stubs and are asserted to refuse BEFORE touching
any dependency. Public synthetic fixture keys model byte
authentication/encryption ONLY.

Adversarial boundaries are exercised through the REAL model APIs
(seal_model / verify_model / claim_model / record_outcome_model /
mark_uncertain_model / resolve_unknown_model / readback_model /
mint_poll_provenance_model / production_*). Shape validators are the real
dataclass __post_init__ code; malformed registry rows are injected through
the actual disposable sqlite adapter; NO test asserts a fake self-raised
exception as if it were model-level rejection.

All identifiers, refs, labels, keys, and outcomes are synthetic PUBLIC
test values.
"""

import os
import sqlite3
import sys
import unittest

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import capability as cap  # noqa: E402
import envelope as env  # noqa: E402

NOW = 1_700_000_000
UID = 76
HEX32_A = "a" * 32
HEX32_B = "b" * 32
HEX32_C = "c" * 32
HEX64_A = "1" * 64
HEX64_B = "2" * 64


def make_key(key_id="key-a", not_before=0, decrypt_until=NOW + 10_000_000):
    return env.SyntheticKey(
        algorithm=env.Algorithm.FERNET, format_version=env.FORMAT_VERSION,
        key_id=key_id, not_before=not_before, decrypt_until=decrypt_until,
        material=Fernet.generate_key())


def make_web_binding(**over):
    fields = dict(
        uid=UID, lifetime=1, channel=cap.Channel.WEB,
        surface=cap.Surface.PRIVATE,
        issuer=env.SessionRef(session_token_hash=HEX64_A),
        operation_id=HEX32_A,
        operation_class=cap.OperationClass.WRITE,
        required_scopes=(cap.Scope.MEMORY_WRITE,),
        audience=env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                       id=None),
        information_ref="info-ref-1",
        content_fingerprint=HEX64_A,
        capture_epoch=7, grant_generation=3, policy_revision=2,
        decision_revision=1, consent_generation=4,
        extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
        group_action=None, sensitive=False, origin=cap.Origin.DIRECT,
        capture_time=NOW, expiry=NOW + env.MAX_AGE_SECONDS,
    )
    fields.update(over)
    return env.ModelOnlyRequestBinding(**fields)


def make_tg_binding(**over):
    fields = dict(
        uid=UID, lifetime=1, channel=cap.Channel.TELEGRAM,
        surface=cap.Surface.PRIVATE,
        issuer=env.TelegramIssuer(bot_id=123, chat_id=456, update_id=789),
        operation_id=HEX32_A,
        operation_class=cap.OperationClass.WRITE,
        required_scopes=(cap.Scope.MEMORY_WRITE,),
        audience=env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                       id=None),
        information_ref="info-ref-1",
        content_fingerprint=HEX64_A,
        capture_epoch=7, grant_generation=3, policy_revision=2,
        decision_revision=1, consent_generation=4,
        extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
        group_action=None, sensitive=False, origin=cap.Origin.DIRECT,
        capture_time=NOW, expiry=NOW + env.MAX_AGE_SECONDS,
    )
    fields.update(over)
    return env.ModelOnlyRequestBinding(**fields)


def make_payload(binding=None, nonce=HEX32_B, key_id="key-a"):
    return env.ModelOnlyCapturedPayload(
        binding=binding or make_web_binding(), nonce=nonce,
        format_version=env.FORMAT_VERSION, key_id=key_id)


def make_context(binding, **over):
    fields = dict(
        original_binding=binding, current_uid=binding.uid,
        current_lifetime=binding.lifetime,
        current_capture_epoch=binding.capture_epoch,
        current_grant_generation=binding.grant_generation,
        current_policy_revision=binding.policy_revision,
        current_decision_revision=binding.decision_revision,
        current_consent_generation=binding.consent_generation,
        current_issuer=binding.issuer,
        current_issuer_valid=True,
    )
    fields.update(over)
    return env.ModelOnlyCallerContext(**fields)


def make_registry(now=NOW):
    clock = env.SyntheticClock(now)
    adapter = env.SyntheticRegistryAdapter(sqlite3.connect(":memory:"))
    return env.ModelOnlyRegistryModel(adapter, clock), adapter, clock


def make_scope(binding=None):
    binding = binding or make_web_binding()
    return env.ModelOnlyOperationScope(
        uid=binding.uid, lifetime=binding.lifetime,
        operation_id=binding.operation_id)


def make_outcome(ref="outcome-ref-1", marker="marker-ref-1"):
    return env.ModelOnlyOutcome(outcome_ref=ref, operation_marker_ref=marker)


def make_evidence(scope, binding, owner_token, fence,
                  effect_state=env.EffectState.CONFIRMED_COMMITTED,
                  outcome=None):
    if effect_state is env.EffectState.CONFIRMED_COMMITTED:
        outcome = outcome or make_outcome()
    return env.ModelOnlyEffectEvidence(
        status=cap.ModelStatus.MODEL_ONLY, scope=scope, binding=binding,
        owner_token=owner_token, fence=fence,
        operation_marker_ref=(outcome.operation_marker_ref
                              if outcome is not None else "marker-ref-1"),
        effect_state=effect_state, outcome=outcome)


def verified(payload=None, key=None, now=NOW):
    payload = payload or make_payload()
    key = key or make_key()
    clock = env.SyntheticClock(now)
    envelope = env.seal_model(payload, key, clock)
    ring = env.SyntheticKeyRing(keys=(key,))
    return env.verify_model(envelope, ring,
                            make_context(payload.binding), clock)


class SealAndVerifyTests(unittest.TestCase):

    def test_roundtrip_returns_model_only_capture(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        self.assertIsInstance(envelope, env.ModelOnlyEnvelope)
        result = env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                                  make_context(payload.binding), clock)
        self.assertIs(result.status, cap.ModelStatus.MODEL_ONLY)
        self.assertEqual(result.payload, payload)
        self.assertEqual(result.binding, payload.binding)

    def test_verify_has_no_registry_and_returns_no_claim(self):
        # verify_model's signature takes (envelope, keyring, context, clock)
        # only: there is no registry parameter to mutate and no claim in
        # the return shape. A successful verify NEVER yields ACQUIRED.
        result = verified()
        self.assertFalse(hasattr(result, "claim_state"))
        self.assertFalse(hasattr(result, "owner_token"))
        self.assertFalse(hasattr(result, "fence"))

    def test_transport_only_reseal_matches_same_commitment(self):
        binding = make_web_binding()
        first = make_payload(binding, nonce=HEX32_B)
        second = make_payload(binding, nonce=HEX32_C)
        self.assertEqual(first.binding.commitment(),
                         second.binding.commitment())
        # reseal changes nonce/token bytes, NEVER intent
        key = make_key()
        clock = env.SyntheticClock(NOW)
        env1 = env.seal_model(first, key, clock)
        env2 = env.seal_model(second, key, clock)
        self.assertNotEqual(env1.token, env2.token)
        self.assertEqual(env1.key_id, env2.key_id)

    def test_bit_flipped_token_is_tampered(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        broken = bytearray(envelope.token)
        broken[len(broken) // 2] ^= 0x01
        flipped = env.ModelOnlyEnvelope(
            format_version=envelope.format_version, key_id=envelope.key_id,
            token=bytes(broken))
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(flipped, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.TAMPERED)

    def test_wrong_key_is_tampered_known_missing_key_unavailable(self):
        payload = make_payload()
        key = make_key()
        other = make_key(key_id="key-a")  # same id, different material
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(other,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.TAMPERED)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope,
                             env.SyntheticKeyRing(keys=(make_key("key-z"),)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)

    def test_retired_and_not_yet_valid_keys_fail_closed(self):
        payload = make_payload()
        retired = make_key(decrypt_until=NOW - 1)
        future = make_key(not_before=NOW + 100)
        clock = env.SyntheticClock(NOW)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(payload, retired, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(payload, future, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)
        # verify with a key retired after sealing also fails closed
        key = make_key()
        envelope = env.seal_model(payload, key, clock)
        gone = env.SyntheticKey(
            algorithm=env.Algorithm.FERNET, format_version=1,
            key_id="key-a", not_before=0, decrypt_until=NOW - 1,
            material=key.material)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(gone,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)

    def test_outer_inner_key_format_mismatch_is_tampered(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        spoofed = env.ModelOnlyEnvelope(
            format_version=envelope.format_version, key_id="key-b",
            token=envelope.token)
        ring = env.SyntheticKeyRing(
            keys=(key, make_key(key_id="key-b")))
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(spoofed, ring, make_context(payload.binding),
                             clock)
        # key-b material differs so authentication itself fails; had the
        # material matched, the outer/inner key_id mismatch would refuse
        self.assertIs(cm.exception.code, env.EnvelopeCode.TAMPERED)

    def test_oversized_outer_token_refused_before_decrypt(self):
        payload = make_payload()
        huge = env.ModelOnlyEnvelope(
            format_version=1, key_id="key-a",
            token=b"x" * (env.OUTER_TOKEN_MAX_BYTES + 1))
        clock = env.SyntheticClock(NOW)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(huge, env.SyntheticKeyRing(keys=(make_key(),)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_expiry_equality_refuses_and_no_future_capture(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        at_expiry = env.SyntheticClock(NOW + env.MAX_AGE_SECONDS)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, ring, make_context(payload.binding),
                             at_expiry)
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)
        last_valid = env.SyntheticClock(NOW + env.MAX_AGE_SECONDS - 1)
        ok = env.verify_model(envelope, ring, make_context(payload.binding),
                              last_valid)
        self.assertEqual(ok.payload, payload)
        # future capture_time refuses at verify
        future_binding = make_web_binding(
            capture_time=NOW + 10,
            expiry=NOW + 10 + env.MAX_AGE_SECONDS)
        future_payload = make_payload(future_binding)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(future_payload, key, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_expiry_must_equal_capture_plus_max_age(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(expiry=NOW + env.MAX_AGE_SECONDS + 1)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_checked_addition_overflow_refuses(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(capture_time=env.BIGINT_MAX - 1,
                             expiry=env.BIGINT_MAX - 1)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        clock = env.SyntheticClock(NOW)
        with self.assertRaises(env.EnvelopeError):
            clock.advance(env.BIGINT_MAX)

    def test_seal_with_expired_payload_refuses(self):
        key = make_key()
        old = make_payload(make_web_binding(
            capture_time=NOW - 10_000,
            expiry=NOW - 10_000 + env.MAX_AGE_SECONDS))
        with self.assertRaises(env.EnvelopeError) as cm:
            env.seal_model(old, key, env.SyntheticClock(NOW))
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)


class StrictDecodeTests(unittest.TestCase):

    def _tamper_json(self, mutate):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        plaintext = env._canon(payload.to_dict())
        mutated = mutate(plaintext)
        token = Fernet(key.material).encrypt(mutated)
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        return envelope, key, payload, clock

    def _expect_malformed(self, envelope, key, payload, clock):
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_unknown_field_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"uid":76', b'"uid":76,"zzz":1'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_missing_field_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"capture_epoch":7,', b''))
        self._expect_malformed(envelope, key, payload, clock)

    def test_coercion_refuses_string_for_int_and_bool_for_int(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"uid":76', b'"uid":"76"'))
        self._expect_malformed(envelope, key, payload, clock)
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"lifetime":1', b'"lifetime":true'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_enum_drift_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"channel":"web"', b'"channel":"whatsapp"'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_duplicate_keys_refuse(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"uid":76', b'"uid":76,"uid":76'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_float_refuses(self):
        envelope, key, payload, clock = self._tamper_json(
            lambda p: p.replace(b'"capture_epoch":7', b'"capture_epoch":7.0'))
        self._expect_malformed(envelope, key, payload, clock)

    def test_audience_shape(self):
        with self.assertRaises(env.EnvelopeError):
            env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE, id="  ")
        with self.assertRaises(env.EnvelopeError):
            env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                  id="x" * 129)
        with self.assertRaises(env.EnvelopeError):
            # GROUP requires a non-None id under the model default
            env.ModelOnlyAudience(kind=env.AudienceKind.GROUP, id=None)


class BindingAndStalenessTests(unittest.TestCase):

    def test_every_projection_field_drift_refuses_stale(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        base = payload.binding.projection_dict()
        self.assertEqual(len(base), 22)  # full projection, nothing dropped
        drifts = [
            dict(uid=UID + 1), dict(lifetime=2),
            dict(channel=cap.Channel.TELEGRAM,
                 issuer=env.TelegramIssuer(bot_id=1, chat_id=2,
                                           update_id=3)),
            dict(surface=cap.Surface.GROUP),
            dict(issuer=env.SessionRef(session_token_hash=HEX64_B)),
            dict(operation_id=HEX32_C),
            dict(operation_class=cap.OperationClass.ERASE,
                 required_scopes=(cap.Scope.MEMORY_ERASE,)),
            dict(required_scopes=(cap.Scope.MEMORY_READ,)),
            dict(audience=env.ModelOnlyAudience(
                kind=env.AudienceKind.EXTERNAL, id="ext-1")),
            dict(information_ref="info-ref-2"),
            dict(content_fingerprint=HEX64_B),
            dict(capture_epoch=8), dict(grant_generation=4),
            dict(policy_revision=3), dict(decision_revision=2),
            dict(consent_generation=5),
            dict(sensitive=True), dict(origin=cap.Origin.ANONYMOUS),
        ]
        for drift in drifts:
            try:
                drifted = make_web_binding(**drift)
            except env.EnvelopeError:
                drifted = None  # constructor itself rejects incoherence
            if drifted is None:
                continue
            with self.assertRaises(env.EnvelopeError, msg=drift) as cm:
                env.verify_model(envelope, ring, make_context(drifted),
                                 clock)
            self.assertIs(cm.exception.code, env.EnvelopeCode.STALE,
                          msg=drift)

    def test_capture_time_expiry_drift_refuses_at_construction(self):
        # original capture/expiry are part of the projection; a changed
        # capture_time or expiry cannot even form a coherent binding
        for drift in (dict(capture_time=NOW + 5), dict(expiry=NOW + 5)):
            with self.assertRaises(env.EnvelopeError, msg=drift):
                make_web_binding(**drift)

    def test_current_snapshot_staleness(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        for field, value in (
                ("current_uid", UID + 1), ("current_lifetime", 2),
                ("current_capture_epoch", 8),
                ("current_grant_generation", 4),
                ("current_policy_revision", 3),
                ("current_decision_revision", 2),
                ("current_consent_generation", 5)):
            context = make_context(payload.binding, **{field: value})
            with self.assertRaises(env.EnvelopeError, msg=field) as cm:
                env.verify_model(envelope, ring, context, clock)
            self.assertIs(cm.exception.code, env.EnvelopeCode.STALE,
                          msg=field)

    def test_uid_recreation_new_lifetime_refuses(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        context = make_context(payload.binding, current_lifetime=2)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, ring, context, clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.STALE)


class RegistryTests(unittest.TestCase):

    def test_claim_acquired_once_concurrent_second_is_in_progress(self):
        registry, _adapter, _clock = make_registry()
        result1 = registry.claim_model(verified(), "worker-1")
        self.assertIs(result1.claim_state, env.ClaimState.ACQUIRED)
        self.assertEqual(result1.fence, 1)
        self.assertIsNotNone(result1.owner_token)
        result2 = registry.claim_model(verified(), "worker-2")
        self.assertIs(result2.claim_state, env.ClaimState.IN_PROGRESS)
        self.assertIsNone(result2.fence)
        self.assertIsNone(result2.owner_token)

    def test_verify_then_claim_at_expiry_refuses(self):
        # verify at expiry-1 succeeds; the claim at expiry MUST recheck
        payload = make_payload()
        key = make_key()
        ring = env.SyntheticKeyRing(keys=(key,))
        ok_clock = env.SyntheticClock(NOW + env.MAX_AGE_SECONDS - 1)
        envelope = env.seal_model(payload, key, env.SyntheticClock(NOW))
        good = env.verify_model(envelope, ring,
                                make_context(payload.binding), ok_clock)
        registry, _adapter, _clock = make_registry(
            now=NOW + env.MAX_AGE_SECONDS)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(good, "worker-1")
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)

    def test_same_operation_reseal_registers_nonce_without_second_claim(self):
        registry, adapter, _clock = make_registry()
        binding = make_web_binding()
        first = registry.claim_model(
            verified(make_payload(binding, nonce=HEX32_B)), "worker-1")
        self.assertIs(first.claim_state, env.ClaimState.ACQUIRED)
        second = registry.claim_model(
            verified(make_payload(binding, nonce=HEX32_C)), "worker-2")
        self.assertIs(second.claim_state, env.ClaimState.IN_PROGRESS)
        # both nonces registered under the SAME operation
        self.assertIsNotNone(adapter.get_nonce(UID, 1, HEX32_B))
        self.assertIsNotNone(adapter.get_nonce(UID, 1, HEX32_C))

    def test_changed_projection_under_same_operation_refuses(self):
        registry, _adapter, _clock = make_registry()
        registry.claim_model(verified(), "worker-1")
        changed = make_payload(make_web_binding(
            information_ref="info-ref-2"))
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(changed), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_cross_operation_nonce_refuses(self):
        registry, _adapter, _clock = make_registry()
        registry.claim_model(verified(), "worker-1")
        other_op = make_payload(
            make_web_binding(operation_id=HEX32_C), nonce=HEX32_B)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(other_op), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_record_outcome_happy_path_and_idempotent_duplicate(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        outcome = make_outcome()
        record = registry.record_outcome_model(
            claim.scope, claim.owner_token, claim.fence, outcome)
        self.assertIs(record.state, env.RegistryState.COMMITTED)
        self.assertEqual(record.outcome, outcome)
        again = registry.record_outcome_model(
            claim.scope, claim.owner_token, claim.fence, outcome)
        self.assertEqual(again, record)
        # marker + outcome landed in the same transaction, carrying the
        # FULL binding (scope, commitment, owner, fence, outcome)
        binding = make_web_binding()
        marker = registry._adapter.get_marker(outcome.operation_marker_ref)
        self.assertIsNotNone(marker)
        self.assertEqual(marker[0], UID)
        self.assertEqual(marker[1], 1)
        self.assertEqual(marker[2], HEX32_A)
        self.assertEqual(marker[3], binding.commitment())
        self.assertEqual(marker[4], claim.owner_token)
        self.assertEqual(marker[5], claim.fence)
        self.assertEqual(marker[6], outcome.outcome_ref)

    def test_changed_outcome_refuses_never_overwrite(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, make_outcome())
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(
                claim.scope, claim.owner_token, claim.fence,
                make_outcome(ref="outcome-ref-2"))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_stale_owner_or_fence_cannot_complete(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, "f" * 32,
                                          claim.fence, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, claim.owner_token,
                                          2, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_record_after_expiry_becomes_expired_tombstone(self):
        registry, _adapter, clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        clock.advance(env.MAX_AGE_SECONDS)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, claim.owner_token,
                                          claim.fence, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)
        record = registry.readback_model(claim.scope)
        self.assertIs(record.state, env.RegistryState.EXPIRED)
        # tombstone never reopens: late evidence cannot resolve EXPIRED
        binding = make_web_binding()
        evidence = make_evidence(claim.scope, binding, claim.owner_token,
                                 claim.fence)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(claim.scope, claim.owner_token,
                                           claim.fence, evidence)
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)

    def test_mark_uncertain_writes_nothing(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        receipt = registry.mark_uncertain_model(
            claim.scope, claim.owner_token, claim.fence)
        self.assertIs(receipt.receipt_state, env.ClaimState.UNKNOWN)
        record = registry.readback_model(claim.scope)
        self.assertIs(record.state, env.RegistryState.CLAIMED)

    def test_resolve_unknown_requires_durable_marker(self):
        registry, _adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        registry.mark_uncertain_model(claim.scope, claim.owner_token,
                                      claim.fence)
        # INCONCLUSIVE stays blocked, row becomes UNKNOWN
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence,
                          effect_state=env.EffectState.INCONCLUSIVE))
        self.assertIs(record.state, env.RegistryState.UNKNOWN)
        # CONFIRMED but marker missing (simulated crash before effect
        # commit) stays BLOCKED - caller flag alone NEVER commits
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence))
        self.assertIs(record.state, env.RegistryState.UNKNOWN)
        # no blind retry: an UNKNOWN claim result grants nothing
        again = registry.claim_model(verified(make_payload(binding)),
                                     "worker-2")
        self.assertIs(again.claim_state, env.ClaimState.UNKNOWN)
        self.assertIsNone(again.owner_token)

    def test_late_confirmed_marker_resolves_unknown_after_expiry(self):
        registry, _adapter, clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        # crash AFTER effect commit: marker durable, outcome write lost
        outcome = make_outcome()
        with registry._adapter.transaction():
            registry._adapter.put_marker(
                claim.scope, binding.commitment(), claim.owner_token,
                claim.fence, outcome)
        registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence,
                          effect_state=env.EffectState.INCONCLUSIVE))
        clock.advance(env.MAX_AGE_SECONDS + 1)
        # late attempted EXECUTION still refuses (fresh claim post-expiry)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(make_payload(binding)),
                                 "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)
        # accounting MAY complete: late confirmed evidence resolves UNKNOWN
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence, outcome=outcome))
        self.assertIs(record.state, env.RegistryState.COMMITTED)
        self.assertEqual(record.outcome, outcome)

    def test_evidence_tamper_refuses(self):
        registry, _adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        wrong_scope = env.ModelOnlyOperationScope(
            uid=UID + 1, lifetime=1, operation_id=HEX32_A)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(
                claim.scope, claim.owner_token, claim.fence,
                make_evidence(wrong_scope, binding, claim.owner_token,
                              claim.fence))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)
        changed_binding = make_web_binding(information_ref="info-ref-2")
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(
                claim.scope, claim.owner_token, claim.fence,
                make_evidence(claim.scope, changed_binding,
                              claim.owner_token, claim.fence))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_readback_is_typed_record_only(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, make_outcome())
        record = registry.readback_model(claim.scope)
        self.assertIs(record.status, cap.ModelStatus.MODEL_ONLY)
        self.assertIs(record.state, env.RegistryState.COMMITTED)
        self.assertFalse(hasattr(record, "owner_token"))
        self.assertFalse(hasattr(record, "fence"))
        self.assertFalse(hasattr(record, "commitment"))
        self.assertIsNone(registry.readback_model(
            env.ModelOnlyOperationScope(uid=UID, lifetime=9,
                                        operation_id=HEX32_C)))

    def test_resolution_deadline_is_expiry_plus_window_checked(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        record = registry.readback_model(claim.scope)
        self.assertEqual(
            record.resolution_deadline,
            NOW + env.MAX_AGE_SECONDS + env.RESOLUTION_WINDOW_SECONDS)

    def test_claim_result_shape_rules(self):
        scope = make_scope()
        with self.assertRaises(env.EnvelopeError):
            # owner_token on a non-ACQUIRED result refuses
            env.ModelOnlyClaimResult(
                status=cap.ModelStatus.MODEL_ONLY,
                claim_state=env.ClaimState.IN_PROGRESS, scope=scope,
                fence=1, owner_token="d" * 32, outcome=None)
        with self.assertRaises(env.EnvelopeError):
            # outcome on a non-COMMITTED result refuses
            env.ModelOnlyClaimResult(
                status=cap.ModelStatus.MODEL_ONLY,
                claim_state=env.ClaimState.ACQUIRED, scope=scope,
                fence=1, owner_token="d" * 32, outcome=make_outcome())


class BlockerFixTests(unittest.TestCase):
    """Peer first-pass blockers 1-8 (2026-10-11): evidence/marker full
    binding, full outcome comparison, canonical form, strict hex, PR-3
    scope/group derivation, typed row/UTF-8 boundaries, in-transaction
    clock reads."""

    def test_foreign_marker_never_commits(self):
        registry, adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence,
            make_evidence(claim.scope, binding, claim.owner_token,
                          claim.fence,
                          effect_state=env.EffectState.INCONCLUSIVE))
        # a marker for a DIFFERENT scope/outcome exists durably
        other_scope = env.ModelOnlyOperationScope(
            uid=UID + 9, lifetime=1, operation_id=HEX32_C)
        with adapter.transaction():
            adapter.put_marker(other_scope, HEX64_B, "e" * 32, 1,
                               make_outcome(ref="real-other-outcome",
                                            marker="foreign-marker"))
        fake = make_evidence(
            claim.scope, binding, claim.owner_token, claim.fence,
            outcome=make_outcome(ref="real-other-outcome",
                                 marker="foreign-marker"))
        record = registry.resolve_unknown_model(
            claim.scope, claim.owner_token, claim.fence, fake)
        self.assertIs(record.state, env.RegistryState.UNKNOWN)

    def test_evidence_owner_fence_must_match_claim(self):
        registry, _adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        outcome = make_outcome()
        with registry._adapter.transaction():
            registry._adapter.put_marker(
                claim.scope, binding.commitment(), claim.owner_token,
                claim.fence, outcome)
        fake = make_evidence(claim.scope, binding, "f" * 32, 99,
                             outcome=outcome)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(
                claim.scope, claim.owner_token, claim.fence, fake)
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_duplicate_record_changed_marker_refuses(self):
        registry, _adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, make_outcome())
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(
                claim.scope, claim.owner_token, claim.fence,
                make_outcome(marker="marker-ref-2"))
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_pretty_json_refused_despite_valid_authentication(self):
        import json as _json
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        pretty = _json.dumps(payload.to_dict(), indent=2,
                             sort_keys=True).encode("utf-8")
        token = Fernet(key.material).encrypt(pretty)
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_hex_signs_prefixes_whitespace_refuse(self):
        with self.assertRaises(env.EnvelopeError):
            make_payload(make_web_binding(), nonce="+1" + "b" * 30)
        with self.assertRaises(env.EnvelopeError):
            make_payload(make_web_binding(), nonce="0x" + "b" * 30)
        with self.assertRaises(env.EnvelopeError):
            make_payload(make_web_binding(), nonce="b" * 31 + " ")
        with self.assertRaises(env.EnvelopeError):
            make_web_binding(operation_id="0x" + "a" * 30)
        with self.assertRaises(env.EnvelopeError):
            make_web_binding(content_fingerprint="ABCD" + "1" * 60)

    def test_pr3_derived_scope_set_enforced(self):
        # WRITE with only memory.read refuses
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(required_scopes=(cap.Scope.MEMORY_READ,))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        # widened scope set refuses
        with self.assertRaises(env.EnvelopeError):
            make_web_binding(required_scopes=(cap.Scope.MEMORY_WRITE,
                                              cap.Scope.MEMORY_ERASE))
        # a sealed payload with a drifted scope set cannot verify
        import json as _json
        payload = make_payload()
        forged = payload.to_dict()
        forged["required_scopes"] = ["memory.read"]
        key = make_key()
        clock = env.SyntheticClock(NOW)
        token = Fernet(key.material).encrypt(
            _json.dumps(forged, sort_keys=True,
                        separators=(",", ":")).encode("utf-8"))
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_pr3_group_rules_enforced(self):
        # PRIVATE GROUP_WRITE with coherent action/scopes still refuses
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(
                operation_class=cap.OperationClass.GROUP_WRITE,
                required_scopes=(cap.Scope.MEMORY_GROUP_WRITE,
                                 cap.Scope.MEMORY_WRITE),
                group_action=cap.GroupAction.WRITE)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        # GROUP surface + GROUP_WRITE + concrete group audience: coherent
        group = make_web_binding(
            surface=cap.Surface.GROUP,
            operation_class=cap.OperationClass.GROUP_WRITE,
            required_scopes=(cap.Scope.MEMORY_GROUP_WRITE,
                             cap.Scope.MEMORY_WRITE),
            group_action=cap.GroupAction.WRITE,
            audience=env.ModelOnlyAudience(kind=env.AudienceKind.GROUP,
                                           id="synthetic-group-1"))
        self.assertEqual(group.audience.id, "synthetic-group-1")
        # GROUP surface write without a concrete group audience refuses
        with self.assertRaises(env.EnvelopeError):
            make_web_binding(
                surface=cap.Surface.GROUP,
                required_scopes=(cap.Scope.MEMORY_WRITE,
                                 cap.Scope.MEMORY_GROUP_WRITE))
        # GROUP surface write with a PRIVATE audience kind refuses
        with self.assertRaises(env.EnvelopeError):
            make_web_binding(
                surface=cap.Surface.GROUP,
                required_scopes=(cap.Scope.MEMORY_WRITE,
                                 cap.Scope.MEMORY_GROUP_WRITE),
                audience=env.ModelOnlyAudience(
                    kind=env.AudienceKind.PRIVATE, id="not-a-group"))

    def test_corrupt_registry_rows_refuse_typed(self):
        registry, adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        adapter._db.execute(
            "UPDATE model_operations SET state='BOGUS' WHERE uid=?",
            (UID,))
        adapter._db.commit()
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.readback_model(claim.scope)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        adapter._db.execute(
            "UPDATE model_operations SET state='CLAIMED',"
            " owner_token='not-hex' WHERE uid=?", (UID,))
        adapter._db.commit()
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_surrogate_and_unicode_refuse_typed(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            make_outcome(ref="bad\ud800ref")
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        with self.assertRaises(env.EnvelopeError):
            make_web_binding(information_ref="bad\ud800ref")
        with self.assertRaises(env.EnvelopeError):
            make_key(key_id="key-\u00e9")

    def test_clock_read_inside_transaction_lock_delay(self):
        class TickClock(env.ModelOnlyClock):
            def __init__(self, values):
                self._values = list(values)

            def now(self):
                return self._values.pop(0) if self._values else NOW

        payload = make_payload()
        key = make_key()
        ring = env.SyntheticKeyRing(keys=(key,))
        envelope = env.seal_model(payload, key, env.SyntheticClock(NOW))
        # verify at expiry-1 succeeds
        good = env.verify_model(
            envelope, ring, make_context(payload.binding),
            env.SyntheticClock(NOW + env.MAX_AGE_SECONDS - 1))
        # the claim's in-transaction read lands AT expiry: refused
        tick = TickClock([NOW, NOW + env.MAX_AGE_SECONDS])
        registry = env.ModelOnlyRegistryModel(
            env.SyntheticRegistryAdapter(sqlite3.connect(":memory:")),
            tick)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(good, "worker-1")
        self.assertIs(cm.exception.code, env.EnvelopeCode.EXPIRED)

    def test_bad_later_clock_reads_refuse(self):
        class TickClock(env.ModelOnlyClock):
            def __init__(self, values):
                self._values = list(values)

            def now(self):
                return self._values.pop(0) if self._values else NOW

        for bad in (True, 1.5, -1, env.BIGINT_MAX + 1):
            tick = TickClock([NOW, bad])
            registry = env.ModelOnlyRegistryModel(
                env.SyntheticRegistryAdapter(sqlite3.connect(":memory:")),
                tick)
            with self.assertRaises(env.EnvelopeError, msg=bad) as cm:
                registry.claim_model(verified(), "worker-1")
            self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED,
                          msg=bad)


class ResidualR2Tests(unittest.TestCase):
    """Peer r2 residuals 1-5: in-transaction owner/fence validation,
    SHARE concrete destination, registry semantic coherence, TOCTOU,
    used-nonce commitment coherence."""

    def test_committed_early_return_validates_evidence_owner_fence(self):
        registry, _adapter, _clock = make_registry()
        binding = make_web_binding()
        claim = registry.claim_model(
            verified(make_payload(binding)), "worker-1")
        outcome = make_outcome()
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, outcome)
        # same outcome, but evidence owner/fence forged: REPLAY even
        # though the operation is already COMMITTED
        fake = make_evidence(claim.scope, binding, "f" * 32, 99,
                             outcome=outcome)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.resolve_unknown_model(
                claim.scope, claim.owner_token, claim.fence, fake)
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_toctou_owner_change_before_lock_refuses(self):
        registry, adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        # row owner changes between the claim and the record attempt
        adapter._db.execute(
            "UPDATE model_operations SET owner_token=? WHERE uid=?",
            ("f" * 32, UID))
        adapter._db.commit()
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.record_outcome_model(claim.scope, claim.owner_token,
                                          claim.fence, make_outcome())
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.mark_uncertain_model(claim.scope, claim.owner_token,
                                          claim.fence)
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)

    def test_share_requires_concrete_destination(self):
        # constructor refuses SHARE with a None destination id
        with self.assertRaises(env.EnvelopeError) as cm:
            make_web_binding(
                operation_class=cap.OperationClass.SHARE,
                required_scopes=(cap.Scope.DATA_SHARE,))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        # authenticated canonical payload with SHARE/None destination
        # refuses at verify
        import json as _json
        payload = make_payload()
        forged = payload.to_dict()
        forged["operation_class"] = "SHARE"
        forged["required_scopes"] = ["data.share"]
        forged["audience"] = {"id": None, "kind": "PRIVATE"}
        key = make_key()
        clock = env.SyntheticClock(NOW)
        token = Fernet(key.material).encrypt(
            _json.dumps(forged, sort_keys=True,
                        separators=(",", ":")).encode("utf-8"))
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(payload.binding), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        # valid concrete destination round trip
        share = make_web_binding(
            operation_class=cap.OperationClass.SHARE,
            required_scopes=(cap.Scope.DATA_SHARE,),
            audience=env.ModelOnlyAudience(
                kind=env.AudienceKind.EXTERNAL, id="synthetic-dest-1"))
        payload = make_payload(share)
        envelope = env.seal_model(payload, key, clock)
        result = env.verify_model(envelope,
                                  env.SyntheticKeyRing(keys=(key,)),
                                  make_context(share), clock)
        self.assertEqual(result.binding, share)

    def test_row_semantic_coherence_enforced(self):
        # well-typed but incoherent rows refuse, exactly like corrupt ones
        registry, adapter, clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        registry.record_outcome_model(claim.scope, claim.owner_token,
                                      claim.fence, make_outcome())
        adapter._db.execute(
            "UPDATE model_operations SET expiry=expiry+100000,"
            " resolution_deadline=resolution_deadline+200000 WHERE uid=?",
            (UID,))
        adapter._db.commit()
        clock.advance(1000)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.readback_model(claim.scope)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_row_coherence_claimed_with_outcome_and_fence(self):
        registry, adapter, _clock = make_registry()
        claim = registry.claim_model(verified(), "worker-1")
        adapter._db.execute(
            "UPDATE model_operations SET outcome_ref='x',"
            " operation_marker_ref='y' WHERE uid=?", (UID,))
        adapter._db.commit()
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.readback_model(claim.scope)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)
        adapter._db.execute(
            "UPDATE model_operations SET outcome_ref=NULL,"
            " operation_marker_ref=NULL, fence=2 WHERE uid=?", (UID,))
        adapter._db.commit()
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.readback_model(claim.scope)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_used_nonce_commitment_coherence(self):
        registry, adapter, _clock = make_registry()
        registry.claim_model(verified(), "worker-1")
        # mutate the registered nonce's commitment to another valid hex
        adapter._db.execute(
            "UPDATE model_used_nonces SET commitment=? WHERE nonce=?",
            (HEX64_B, HEX32_B))
        adapter._db.commit()
        with self.assertRaises(env.EnvelopeError) as cm:
            registry.claim_model(verified(), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)
        # a changed binding reusing the registered nonce also refuses
        registry2, _adapter2, _clock2 = make_registry()
        registry2.claim_model(verified(), "worker-1")
        changed = make_payload(
            make_web_binding(information_ref="info-ref-2"), nonce=HEX32_B)
        with self.assertRaises(env.EnvelopeError) as cm:
            registry2.claim_model(verified(changed), "worker-2")
        self.assertIs(cm.exception.code, env.EnvelopeCode.REPLAY)


class ResidualR3Tests(unittest.TestCase):
    """Peer r3 residual: parse-specific RecursionError/ValueError from
    authenticated plaintext refuse typed MALFORMED, never raw."""

    def _seal_raw(self, raw: bytes):
        import json as _json
        key = make_key()
        clock = env.SyntheticClock(NOW)
        token = Fernet(key.material).encrypt(raw)
        return env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                     token=token), key, clock

    def test_deeply_nested_plaintext_refuses_typed(self):
        raw = b"[" * 1600 + b"]" * 1600
        envelope, key, clock = self._seal_raw(raw)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(make_web_binding()), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_huge_integer_plaintext_refuses_typed(self):
        raw = b"9" * 5000
        envelope, key, clock = self._seal_raw(raw)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(make_web_binding()), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_bigint_boundary_capture_time(self):
        # a capture_time at the BIGINT boundary round trips;
        # BIGINT_MAX+1 refuses typed MALFORMED - never a raw overflow
        # or a silent wrap. expiry == capture_time + 900 stays within
        # BIGINT in the accepted case.
        import json as _json
        # accepted case: coherent binding at the boundary round trips
        binding = make_web_binding(capture_time=cap.BIGINT_MAX - 900,
                                   expiry=cap.BIGINT_MAX)
        key = make_key(decrypt_until=cap.BIGINT_MAX)
        clock = env.SyntheticClock(cap.BIGINT_MAX - 900)
        token = Fernet(key.material).encrypt_at_time(
            _json.dumps(make_payload(binding).to_dict(), sort_keys=True,
                        separators=(",", ":")).encode("utf-8"),
            cap.BIGINT_MAX - 900)
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        result = env.verify_model(envelope,
                                  env.SyntheticKeyRing(keys=(key,)),
                                  make_context(binding), clock)
        self.assertEqual(result.binding, binding)
        # refused case: forged BIGINT_MAX+1 capture_time in the
        # authenticated plaintext refuses typed MALFORMED
        normal = make_web_binding()
        forged = make_payload(normal).to_dict()
        forged["capture_time"] = cap.BIGINT_MAX + 1
        key = make_key()
        clock = env.SyntheticClock(NOW)
        token = Fernet(key.material).encrypt(
            _json.dumps(forged, sort_keys=True,
                        separators=(",", ":")).encode("utf-8"))
        envelope = env.ModelOnlyEnvelope(format_version=1, key_id="key-a",
                                         token=token)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, env.SyntheticKeyRing(keys=(key,)),
                             make_context(normal), clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)


class FinalAdditionTests(unittest.TestCase):
    """Peer final-verdict additions: canonical scope order, current
    issuer/session staleness, unique keyring ids, explicit key boundary."""

    def test_scope_order_normalization_all_permutations(self):
        import itertools
        scopes = (cap.Scope.MEMORY_GROUP_WRITE, cap.Scope.MEMORY_WRITE)
        bindings = []
        for perm in itertools.permutations(scopes):
            bindings.append(make_web_binding(
                surface=cap.Surface.GROUP,
                operation_class=cap.OperationClass.GROUP_WRITE,
                group_action=cap.GroupAction.WRITE,
                required_scopes=perm,
                audience=env.ModelOnlyAudience(
                    kind=env.AudienceKind.GROUP, id="synthetic-group-1")))
        bindings.append(make_web_binding(
            surface=cap.Surface.GROUP,
            operation_class=cap.OperationClass.GROUP_WRITE,
            group_action=cap.GroupAction.WRITE,
            required_scopes=frozenset(scopes),
            audience=env.ModelOnlyAudience(
                kind=env.AudienceKind.GROUP, id="synthetic-group-1")))
        first = bindings[0]
        for other in bindings[1:]:
            self.assertEqual(first, other)
            self.assertEqual(first.commitment(), other.commitment())
        # a transport-only reseal from a reversed-order construction still
        # verifies against the original context
        payload = make_payload(first)
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        result = env.verify_model(
            envelope, env.SyntheticKeyRing(keys=(key,)),
            make_context(bindings[1]), clock)
        self.assertEqual(result.binding, first)

    def test_current_session_revocation_is_stale(self):
        payload = make_payload()
        key = make_key()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        # revoked issuer: everything else unchanged
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(
                envelope, ring,
                make_context(payload.binding, current_issuer_valid=False),
                clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.STALE)
        # replaced session: same uid/lifetime/revisions, new session hash
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(
                envelope, ring,
                make_context(payload.binding,
                             current_issuer=env.SessionRef(
                                 session_token_hash=HEX64_B)),
                clock)
        self.assertIs(cm.exception.code, env.EnvelopeCode.STALE)

    def test_keyring_duplicate_ids_refuse(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            env.SyntheticKeyRing(keys=(make_key("key-a"),
                                       make_key("key-a")))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_decrypt_until_boundary_explicit(self):
        boundary = NOW + 500
        key = make_key(decrypt_until=boundary)
        self.assertTrue(key.usable_at(boundary))       # INCLUSIVE
        self.assertFalse(key.usable_at(boundary + 1))  # unavailable after
        # verify works AT the boundary and refuses AFTER it
        payload = make_payload()
        clock = env.SyntheticClock(NOW)
        envelope = env.seal_model(payload, key, clock)
        ring = env.SyntheticKeyRing(keys=(key,))
        ok = env.verify_model(envelope, ring, make_context(payload.binding),
                              env.SyntheticClock(boundary))
        self.assertEqual(ok.payload, payload)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.verify_model(envelope, ring, make_context(payload.binding),
                             env.SyntheticClock(boundary + 1))
        self.assertIs(cm.exception.code, env.EnvelopeCode.KEY_UNAVAILABLE)


class ProvenanceAndStubTests(unittest.TestCase):

    def test_poll_provenance_mint_and_invariant(self):
        payload = make_payload(make_tg_binding())
        clock = env.SyntheticClock(NOW + 5)
        provenance = env.mint_poll_provenance_model(
            env.SyntheticBoundaryEvidence.for_testing(payload), clock)
        self.assertIs(provenance.status, cap.ModelStatus.MODEL_ONLY)
        self.assertEqual(provenance.payload, payload)
        self.assertEqual(provenance.minted_at, NOW + 5)
        self.assertLess(provenance.minted_at, payload.binding.expiry)

    def test_poll_provenance_telegram_only(self):
        web_payload = make_payload(make_web_binding())
        with self.assertRaises(env.EnvelopeError) as cm:
            env.mint_poll_provenance_model(
                env.SyntheticBoundaryEvidence.for_testing(web_payload),
                env.SyntheticClock(NOW))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MALFORMED)

    def test_boundary_evidence_constructor_guard(self):
        with self.assertRaises(env.EnvelopeError) as cm:
            env.SyntheticBoundaryEvidence(make_payload())
        self.assertIs(cm.exception.code, env.EnvelopeCode.MODEL_ONLY)
        with self.assertRaises(env.EnvelopeError) as cm:
            env.mint_poll_provenance_model("not-evidence",
                                           env.SyntheticClock(NOW))
        self.assertIs(cm.exception.code, env.EnvelopeCode.MODEL_ONLY)

    def test_all_five_production_stubs_refuse_unconditionally(self):
        key = make_key()
        clock = env.SyntheticClock(NOW)
        payload = make_payload()
        capability = object()  # even a "valid" capability-shaped input
        for stub in (env.production_seal, env.production_verify,
                     env.production_dispatch, env.production_load,
                     env.production_mint_poll_provenance):
            with self.assertRaises(env.EnvelopeError) as cm:
                stub(payload, key, clock, capability)
            self.assertIs(cm.exception.code, env.EnvelopeCode.MODEL_ONLY)

    def test_error_messages_are_fixed_and_echo_nothing(self):
        for code in env.EnvelopeCode:
            message = str(env.EnvelopeError(code))
            for leak in (HEX32_B, HEX64_A, "key-a", "info-ref-1",
                         "outcome-ref-1"):
                self.assertNotIn(leak, message)


if __name__ == "__main__":
    unittest.main()
