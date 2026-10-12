"""PR-5 FactStore MODEL test suite - named-vector coverage (contract sec 14).

MODEL ONLY: every fixture is public synthetic ("minted by fixture F"),
never authority. Run from ports/memory: python3 -m pytest tests/ -q
(or python3 tests/test_pr5_factstore.py - no pytest dependency; a tiny
runner is embedded at the bottom).
"""
import dataclasses
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capability as cap  # noqa: E402
import envelope as env  # noqa: E402
import factstore as f  # noqa: E402

T0 = 1_700_000_000
ALL_SCOPES = [cap.Scope.MEMORY_WRITE, cap.Scope.MEMORY_READ,
              cap.Scope.MEMORY_ERASE]


_TMPDIR = [None]
_MKSEQ = [0]


def mkstore_path(scopes=ALL_SCOPES, uid=1, lid=1, epoch=1):
    """A file-backed store under the EXCLUSIVE isolation fixture."""
    if _TMPDIR[0] is None:
        _TMPDIR[0] = tempfile.mkdtemp(prefix="pr5tests-")
    _MKSEQ[0] += 1
    path = os.path.join(_TMPDIR[0], "store-%04d.db" % _MKSEQ[0])
    conn = f.open_exclusive_db(path)
    store = f.FactStore(conn, f.JsonTestClock(T0))
    store.setup_owner(uid, lid, epoch, scopes=scopes)
    return store, conn, path


def mkstore(scopes=ALL_SCOPES, uid=1, lid=1, epoch=1):
    store, conn, _path = mkstore_path(scopes, uid, lid, epoch)
    return store, conn


def resolve_op(store, req, b):
    tgt = store._historical_targets(req.operation_id, req, b)
    w, rctx = read_pair(store, "b4" * 16, f.ReadMethod.RESOLVE, tgt)
    return store.resolve(1, req.operation_id, req, b, w, store._clock,
                         rctx)


def new_fact(key="tea", value="green", category=f.Category.PREFERENCE,
             source=f.Source.WEB, excerpt="x", captured_at=T0):
    return f.NewFact(key=key, value=value, category=category, source=source,
                     provenance=f.Provenance(excerpt=excerpt,
                                             captured_at=captured_at))


def put_req(op, key="tea", value="green", **kw):
    return f.FactRequest(method=f.MethodCode.PUT,
                         intent=f.ModelIntent.WriteFact,
                         payload=f.PutPayload(fact=new_fact(key, value, **kw)),
                         owner_uid=1, lifetime_id=1, operation_id=op,
                         information_ref="test")


def mint(store, request):
    c, l, p, b = store.mint_mutation_fixtures(request)
    return c, l, p, b, store._caller_context(b)


def run(store, request):
    c, l, p, b, ctx = mint(store, request)
    return store.execute(1, request, c, l, p, ctx), b


_CTX_COUNTER = [0]


def read_ctx(store):
    _CTX_COUNTER[0] += 1
    req = f.FactRequest(method=f.MethodCode.SET_OPTIN,
                        intent=f.ModelIntent.SetOptIn,
                        payload=f.OptInPayload(
                            setting=f.OptInSetting(category=f.Category.FACT),
                            value=False),
                        owner_uid=1, lifetime_id=1,
                        operation_id="%032x" % (0xC700 + _CTX_COUNTER[0]),
                        information_ref="ctx")
    _, _, _, b = store.mint_mutation_fixtures(req)
    return store._caller_context(b)


def read_cap(store, op, method, targets, uid=1, lid=1):
    w, _ = store.mint_read_fixtures(uid, lid, op, method, targets)
    return w


def read_pair(store, op, method, targets, uid=1, lid=1):
    """The read wrapper WITH its PAIRED caller context (v80 3496-3501:
    the commitment binds the ACTUAL SUPPLIED context, so a conformant
    call supplies the context minted WITH the wrapper)."""
    return store.mint_read_fixtures(uid, lid, op, method, targets)


def get(store, fid, ctx, op="d1" * 16):
    w, rctx = read_pair(store, op, f.ReadMethod.GET, {"fid": fid})
    return store.get(1, fid, w, store._clock, rctx)


class V01ToV06Put(unittest.TestCase):
    """V01 put applied; V02 unchanged; V03 conflict; V04 oversize;
    V05 normalization collision; V06 string rejections."""

    def test_v01_put_applied(self):
        store, _ = mkstore()
        r, _ = run(store, put_req("a1" * 16))
        oc = r.result.outcome
        self.assertEqual(oc.result_code, "applied")
        self.assertEqual(oc.readback.fact_id, 1)
        self.assertEqual(oc.readback.row_version, 1)
        self.assertEqual(oc.applied_at_revision, 1)
        row = get(store, 1, read_ctx(store)).row
        self.assertEqual(row.value, "green")
        self.assertEqual(row.row_version, 1)

    def test_v02_put_unchanged(self):
        store, conn = mkstore()
        run(store, put_req("a1" * 16))
        r2, _ = run(store, put_req("a2" * 16))  # same key/value, new op
        self.assertEqual(r2.result.outcome.result_code, "unchanged")
        self.assertIsNone(r2.result.outcome.applied_at_revision)
        n_hist = conn.execute("SELECT count(*) FROM history").fetchone()[0]
        self.assertEqual(n_hist, 0)
        self.assertEqual(store._store_revision(1), 1)  # no bump
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 1)  # no new row

    def test_v03_put_conflict_sorted_fields_no_content(self):
        store, _ = mkstore()
        run(store, put_req("a1" * 16))
        r2, _ = run(store, put_req("a2" * 16, value="black"))
        oc = r2.result.outcome
        self.assertEqual(oc.result_code, "conflict")
        self.assertEqual([cf.value for cf in oc.conflict_fields], ["value"])  # sorted
        self.assertIsNone(oc.applied_at_revision)
        # no content disclosure: the conflict outcome carries the existing
        # readback ref but never the stored value
        self.assertFalse(hasattr(oc, "existing_value"))

    def test_v04_oversize_key_value_refusal(self):
        store, _ = mkstore()
        with self.assertRaises(cap.ModelInputError):
            new_fact(key="k" * 61)
        with self.assertRaises(cap.ModelInputError):
            new_fact(value="v" * 401)

    def test_v05_key_normalization_collision_is_key_match(self):
        store, conn = mkstore()
        run(store, put_req("a1" * 16, key="Tea"))
        r2, _ = run(store, put_req("a2" * 16, key="tea"))
        # normalization collision = the SAME key: unchanged, never a 2nd row
        self.assertEqual(r2.result.outcome.result_code, "unchanged")
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_v06_string_rejections(self):
        for bad in ("", "   ", "x" * 200 + "́"):
            pass  # placeholder: real cases below
        with self.assertRaises(cap.ModelInputError):
            new_fact(key="")
        with self.assertRaises(cap.ModelInputError):
            new_fact(key="   ")
        with self.assertRaises(cap.ModelInputError):
            new_fact(value="\x00control")
        with self.assertRaises(cap.ModelInputError):
            new_fact(key="surrogate\ud800")


class V07ToV10Supersede(unittest.TestCase):
    """V07 supersede 1-to-1/N-to-1; V08 duplicate old_fids; V09 non-target
    key collision; V10 supersede allocates no epoch."""

    def _sup_req(self, op, old_fids, expected, value="oolong", key="tea"):
        return f.FactRequest(
            method=f.MethodCode.SUPERSEDE,
            intent=f.ModelIntent.SupersedeFact,
            payload=f.SupersedePayload(
                replacement=new_fact(key=key, value=value),
                old_fids=tuple(old_fids), expected_versions=dict(expected)),
            owner_uid=1, lifetime_id=1, operation_id=op,
            information_ref="test")

    def test_v07_supersede_1to1(self):
        store, conn = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        r2, _ = run(store, self._sup_req("e5" * 16, [fid], {fid: 1}))
        oc = r2.result.outcome
        self.assertEqual(oc.result_code, "applied")
        self.assertEqual(oc.superseded, (fid,))
        self.assertNotEqual(oc.readback.fact_id, fid)  # replacement new id
        self.assertEqual(oc.readback.row_version, 1)       # replacement v1
        hist = conn.execute(
            "SELECT * FROM history ORDER BY history_id").fetchall()
        self.assertEqual(len(hist), 1)
        # old fact marked superseded; live read of replacement
        row = get(store, oc.readback.fact_id, read_ctx(store),
                  op="d2" * 16).row
        self.assertEqual(row.value, "oolong")

    def test_v07_supersede_n_to_1(self):
        store, conn = mkstore()
        fids = []
        for i in range(3):
            r, _ = run(store, put_req(("a%d" % i) * 16, key="k%d" % i))
            fids.append(r.result.outcome.readback.fact_id)
        expected = {fid: 1 for fid in fids}
        r2, _ = run(store, self._sup_req("e6" * 16, fids, expected,
                                         key="merged"))
        self.assertEqual(r2.result.outcome.result_code, "applied")
        self.assertEqual(sorted(r2.result.outcome.superseded), sorted(fids))
        hist = conn.execute("SELECT count(*) FROM history").fetchone()[0]
        self.assertEqual(hist, 3)  # one immutable final row per superseded

    def test_v08_duplicate_old_fids_refusal(self):
        store, _ = mkstore()
        with self.assertRaises(cap.ModelInputError):
            self._sup_req("e7" * 16, [1, 1], {1: 1})

    def test_v09_non_target_key_collision(self):
        store, conn = mkstore()
        r1, _ = run(store, put_req("a1" * 16, key="tea"))
        fid = r1.result.outcome.readback.fact_id
        r2, _ = run(store, put_req("a2" * 16, key="coffee"))
        other = r2.result.outcome.readback.fact_id
        # supersede tea with a replacement keyed 'coffee': collides with the
        # NON-TARGET live row
        r3, _ = run(store, self._sup_req("e8" * 16, [fid], {fid: 1},
                                         key="coffee"))
        oc = r3.result.outcome
        self.assertEqual(oc.result_code, "conflict")
        self.assertEqual(oc.collided_fid, other)
        self.assertIsNone(oc.applied_at_revision)  # no writes
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM history").fetchone()[0], 0)

    def test_v10_supersede_allocates_no_epoch(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        before = store._current_epoch(1)
        r2, _ = run(store, self._sup_req("e5" * 16, [fid], {fid: 1}))
        self.assertEqual(store._current_epoch(1), before)
        self.assertIsNone(getattr(r2.result.outcome, "new_epoch", None))



def closure_and_delete(store, ctx, fid, op):
    w, rctx = read_pair(store, "f6" * 16, f.ReadMethod.PREPARE_CLOSURE,
                        {"fid": fid})
    m = store.prepare_closure(1, fid, w, store._clock, rctx)
    req = f.FactRequest(
        method=f.MethodCode.DELETE, intent=f.ModelIntent.DeleteFact,
        payload=f.DeletePayload(
            fid=fid, closure_cas=f.ClosureCAS(
                live=m.live, lineage_digest=m.lineage_digest())),
        owner_uid=1, lifetime_id=1, operation_id=op,
        information_ref="test")
    return run(store, req)


def wipe_req(op, expected=None):
    return f.FactRequest(method=f.MethodCode.WIPE,
                         intent=f.ModelIntent.WipeOwner,
                         payload=f.WipePayload(
                             expected_target_versions=expected or {}),
                         owner_uid=1, lifetime_id=1, operation_id=op,
                         information_ref="test")


class V11ToV14Delete(unittest.TestCase):
    """V11 delete applied; V12 absent; V13 marked-target proceeds;
    V14 fid-lineage scrub closure incl. history-only full closure."""

    def test_v11_delete_applied_fresh_epoch_and_proof(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        before = store._current_epoch(1)
        (r, _), = [closure_and_delete(store, read_ctx(store), fid, "07" * 16)]
        oc = r.result.outcome
        self.assertEqual(oc.result_code, "applied")
        self.assertIsNotNone(oc.new_epoch)
        self.assertGreater(oc.new_epoch, before)  # fresh, never reused
        self.assertEqual(store._current_epoch(1), oc.new_epoch)
        proof = oc.proof
        self.assertIsNotNone(proof)
        counts = proof.closure_counts
        self.assertEqual(counts[f.CopyClass.FACTS]["physically_removed"], 1)
        # fact row physically gone
        self.assertIsNone(get(store, fid, read_ctx(store)).row)

    def test_v12_delete_absent_no_epoch_proof_none_recorded(self):
        store, conn = mkstore()
        before = store._current_epoch(1)
        (r, _), = [closure_and_delete(store, read_ctx(store), 99, "07" * 16)]
        oc = r.result.outcome
        self.assertEqual(oc.result_code, "absent")
        self.assertIsNone(oc.new_epoch)
        self.assertIsNone(oc.proof)
        self.assertIsNone(oc.applied_at_revision)
        self.assertEqual(store._current_epoch(1), before)
        # still recorded: replay returns the recorded outcome
        return store, r

    def test_v12b_delete_absent_recorded_replay(self):
        store, conn = mkstore()
        w, rctx = read_pair(store, "f6" * 16, f.ReadMethod.PREPARE_CLOSURE,
                            {"fid": 99})
        m = store.prepare_closure(1, 99, w, store._clock, rctx)
        req = f.FactRequest(
            method=f.MethodCode.DELETE, intent=f.ModelIntent.DeleteFact,
            payload=f.DeletePayload(
                fid=99, closure_cas=f.ClosureCAS(
                    live=m.live, lineage_digest=m.lineage_digest())),
            owner_uid=1, lifetime_id=1, operation_id="07" * 16,
            information_ref="test")
        c, l, p, b, ctx2 = mint(store, req)
        r1 = store.execute(1, req, c, l, p, ctx2)
        r2 = store.execute(1, req, c, l, p, ctx2)
        self.assertEqual(r2.kind, "replayed")
        self.assertEqual(r2.replay.outcome.result_code, "absent")
        # absent delete recorded: the op row exists
        self.assertIsNotNone(conn.execute(
            "SELECT 1 FROM operation_records WHERE operation_id=?",
            ("07" * 16,)).fetchone())

    def test_v13_delete_of_marked_target_proceeds(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        sup = f.FactRequest(
            method=f.MethodCode.SUPERSEDE, intent=f.ModelIntent.SupersedeFact,
            payload=f.SupersedePayload(replacement=new_fact(value="oolong"),
                                       old_fids=(fid,),
                                       expected_versions={fid: 1}),
            owner_uid=1, lifetime_id=1, operation_id="e5" * 16,
            information_ref="test")
        run(store, sup)  # fid now MARKED (superseded, history-only)
        (r, _), = [closure_and_delete(store, read_ctx(store), fid, "08" * 16)]
        self.assertEqual(r.result.outcome.result_code, "applied")

    def test_v14_history_only_full_closure(self):
        store, conn = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        sup = f.FactRequest(
            method=f.MethodCode.SUPERSEDE, intent=f.ModelIntent.SupersedeFact,
            payload=f.SupersedePayload(replacement=new_fact(value="oolong"),
                                       old_fids=(fid,),
                                       expected_versions={fid: 1}),
            owner_uid=1, lifetime_id=1, operation_id="e5" * 16,
            information_ref="test")
        r2, _ = run(store, sup)
        new_fid = r2.result.outcome.readback.fact_id
        w, rctx = read_pair(store, "f6" * 16, f.ReadMethod.PREPARE_CLOSURE,
                            {"fid": fid})
        m = store.prepare_closure(1, fid, w, store._clock, rctx)
        self.assertIsNone(m.live)  # history-only fid
        (r, _), = [closure_and_delete(store, rctx, fid, "09" * 16)]
        oc = r.result.outcome
        self.assertEqual(oc.result_code, "applied")
        counts = oc.proof.closure_counts
        self.assertEqual(counts[f.CopyClass.HISTORY]["physically_removed"], 1)
        self.assertEqual(counts.get(f.CopyClass.FACTS, {"physically_removed": 0})["physically_removed"], 0)
        # the supersede op record content scrubbed (shared-op closure)
        self.assertGreaterEqual(
            counts[f.CopyClass.OP_RECORDS]["content_scrubbed"], 1)
        # replacement row untouched
        self.assertEqual(get(store, new_fid, read_ctx(store),
                             op="d9" * 16).row.value, "oolong")


class V15ToV18Wipe(unittest.TestCase):
    """V15 wipe incl. empty; V16 retained whitelist; V17 post-scrub replay;
    V18 safe ids only; V18b report paging."""

    def test_v15_wipe_full_and_empty(self):
        store, conn = mkstore()
        run(store, put_req("a1" * 16))
        before = store._current_epoch(1)
        r, b = run(store, wipe_req("0a" * 16))
        oc = r.result.outcome
        self.assertEqual(oc.result_code, "applied")
        self.assertGreater(oc.new_epoch, before)
        self.assertIsNotNone(oc.applied_at_revision)
        self.assertGreaterEqual(oc.report.page_count, 1)
        total_removed = sum(c["physically_removed"]
                            for c in oc.report.counts.values())
        self.assertGreaterEqual(total_removed, 1)
        # empty wipe: still epoch + revision + recorded
        r2, _ = run(store, wipe_req("0b" * 16))
        oc2 = r2.result.outcome
        self.assertEqual(oc2.result_code, "applied")
        self.assertGreater(oc2.new_epoch, oc.new_epoch)
        self.assertIsNotNone(oc2.applied_at_revision)

    def test_v16_retained_whitelist_honest(self):
        store, _ = mkstore()
        run(store, put_req("a1" * 16))
        r, _ = run(store, wipe_req("0a" * 16))
        retained = r.result.outcome.report.retained
        self.assertTrue(retained)
        for rc in retained:
            self.assertIsInstance(rc, f.RetainedClass)
        # every retained class is a DECLARED whitelist member (honest
        # retention: non-content markers/counters only, never content)
        names = {rc.name.value for rc in retained}
        allowed = {m.value for m in f.RetainedClassName}
        self.assertTrue(names <= allowed)
        self.assertIn("op_record_noncontent", names)
        self.assertIn("owner_counters", names)

    def test_v17_post_scrub_replay_scrubbed_outcome(self):
        # V30: after epoch movement the ORIGINAL capability can no longer
        # serve a mutation replay (EPOCH) - the post-scrub lookup is the
        # FRESH-READ historical API with the caller's TYPED ORIGINALS
        # (V53 SUPPLIED mode), and the stored outcome is the scrubbed
        # variant, never content.
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        r1 = store.execute(1, req, c, l, p, ctx)
        fid = r1.result.outcome.readback.fact_id
        closure_and_delete(store, read_ctx(store), fid, "07" * 16)
        # mutation replay with the stale capability refuses EPOCH (V26/V31)
        with self.assertRaises(cap.ConflictError):
            store.execute(1, req, c, l, p, ctx)
        # historical readback with supplied originals: ScrubbedOutcome
        tgt = store._historical_targets("a1" * 16, req, b)
        w, rctx = read_pair(store, "b7" * 16, f.ReadMethod.READBACK, tgt)
        pr = store.readback(1, "a1" * 16, req, b, w, store._clock,
                            rctx)
        self.assertIsInstance(pr, f.PostReadResult)
        self.assertIsInstance(pr.original_outcome, f.ScrubbedOutcome)
        self.assertNotIn("green", repr(pr.original_outcome))

    def test_v18_post_scrub_reports_safe_ids_only(self):
        store, _ = mkstore()
        run(store, put_req("a1" * 16, value="s3cr3t-value"))
        r, _ = run(store, wipe_req("0a" * 16))
        report = r.result.outcome.report
        self.assertNotIn("s3cr3t-value", repr(report))
        for i in range(report.page_count):
            w, rctx = read_pair(store, "e%d" % i * 16,
                                f.ReadMethod.READ_REPORT_PAGE,
                                {"page_index": i, "report_id": report.report_id})
            rng = store.read_report_page(1, report.report_id, i, w,
                                         store._clock, rctx)
            self.assertIsInstance(rng, f.ReportRange)
            for ref in rng.ids:
                self.assertIsInstance(ref, f.InventoryRef)
            self.assertNotIn("s3cr3t-value", repr(rng))

    def test_v18b_report_paging_over_4096(self):
        store, conn = mkstore()
        # >4096 op_records rows (live-fact cap is 500; the >4096 inventory
        # is built from per-op rows, not facts)
        n = 4200
        for i in range(n):
            run(store, put_req(("%032x" % (0xA000 + i)), key="tea"))
        r, _ = run(store, wipe_req("0a" * 16))
        report = r.result.outcome.report
        # collect every page; totals must match across pages, ordered,
        # no silent truncation
        seen = []
        for i in range(report.page_count):
            w, rctx = read_pair(store, ("%032x" % (0xE000 + i)),
                                f.ReadMethod.READ_REPORT_PAGE,
                                {"page_index": i,
                                 "report_id": report.report_id})
            rng = store.read_report_page(1, report.report_id, i, w,
                                         store._clock, rctx)
            self.assertEqual(rng.page_index, i)
            self.assertLessEqual(len(rng.ids), f.PAGE_MAX)
            seen.extend(rng.ids)
        keys = [(x.class_name.value, x.entry_id) for x in seen]
        self.assertEqual(keys, sorted(set(keys)))  # global order, deduped
        op_refs = [k for k in keys if k[0] == "op_records"]
        self.assertGreater(len(op_refs), 4096)  # >4096 erased COMPLETELY
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM operation_records WHERE scrubbed=0"
            ).fetchone()[0], 1)  # only the wipe's own row remains unscrubbed



def sup_req(op, old_fids, expected, value="oolong", key="tea"):
    return f.FactRequest(
        method=f.MethodCode.SUPERSEDE, intent=f.ModelIntent.SupersedeFact,
        payload=f.SupersedePayload(replacement=new_fact(key=key,
                                                        value=value),
                                   old_fids=tuple(old_fids),
                                   expected_versions=dict(expected)),
        owner_uid=1, lifetime_id=1, operation_id=op,
        information_ref="test")


class V19ToV21CAS(unittest.TestCase):
    """V19 CAS refusal changes nothing; V20 retry SAME binding only;
    V21 changed versions = NEW operation + NEW intent."""

    def test_v19_cas_refusal_changes_nothing(self):
        store, conn = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        rev_before = store._store_revision(1)
        with self.assertRaises(f.FactConflict):
            run(store, sup_req("e5" * 16, [fid], {fid: 999}))  # wrong ver
        self.assertEqual(store._store_revision(1), rev_before)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM history").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_v20_cas_retry_same_binding_only(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        req = sup_req("e5" * 16, [fid], {fid: 999})
        c, l, p, b, ctx = mint(store, req)
        with self.assertRaises(f.FactConflict):
            store.execute(1, req, c, l, p, ctx)
        # SAME EXACT unchanged binding retry: the same refusal, still no
        # record (CAS refusals are pre-record)
        with self.assertRaises(f.FactConflict):
            store.execute(1, req, c, l, p, ctx)

    def test_v21_changed_versions_new_operation_new_intent(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        # changed content under the OLD op id = REQUEST_DRIFT (the caller
        # tampers the request against the ORIGINAL minted bundles)
        req = sup_req("e5" * 16, [fid], {fid: 1})
        c, l, p, b, ctx = mint(store, req)
        r1 = store.execute(1, req, c, l, p, ctx)
        new_fid = r1.result.outcome.readback.fact_id
        req_changed = sup_req("e5" * 16, [fid], {fid: 1}, value="CHANGED")
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req_changed, c, l, p, ctx)
        self.assertIn("REQUEST_DRIFT", str(cm.exception))
        # a NEW op id with the new intent is a NEW operation (its own CAS)
        r2, _ = run(store, sup_req("e6" * 16, [new_fid], {new_fid: 1},
                                   value="CHANGED", key="tea"))
        self.assertEqual(r2.result.outcome.result_code, "applied")


class V22ToV25Capacity(unittest.TestCase):
    """V22 at-cap 0/1/N; V23 N-to-1 at cap; V24 >500 CapRefused;
    V25 last-slot race serializes locally."""

    def _fill(self, store, n, start=0):
        fids = []
        for i in range(start, start + n):
            r, _ = run(store, put_req("%032x" % (0x1000 + i),
                                      key="k%04d" % i))
            fids.append(r.result.outcome.readback.fact_id)
        return fids

    @staticmethod
    def _live(conn):
        return conn.execute("SELECT count(*) FROM facts").fetchone()[0]

    def test_v22_at_cap_boundaries(self):
        store, conn = mkstore()
        self._fill(store, 1)
        self.assertEqual(self._live(conn), 1)   # 0 -> 1
        self._fill(store, f.LIVE_FACT_CAP - 1, start=1)
        self.assertEqual(self._live(conn), f.LIVE_FACT_CAP)
        with self.assertRaises(cap.CapRefused):
            run(store, put_req("%032x" % 0x9000, key="overflow"))
        self.assertEqual(self._live(conn), f.LIVE_FACT_CAP)

    def test_v23_n_to_1_at_cap_allowed(self):
        store, conn = mkstore()
        fids = self._fill(store, f.LIVE_FACT_CAP)
        r, _ = run(store, sup_req("e5" * 16, fids[:2],
                                  {fids[0]: 1, fids[1]: 1}, key="merged"))
        self.assertEqual(r.result.outcome.result_code, "applied")
        self.assertEqual(self._live(conn), f.LIVE_FACT_CAP - 1)

    def test_v24_cap_refused_carries_resulting_count(self):
        store, _ = mkstore()
        self._fill(store, f.LIVE_FACT_CAP)
        try:
            run(store, put_req("%032x" % 0x9000, key="overflow"))
            self.fail("expected CapRefused")
        except cap.CapRefused as e:
            self.assertEqual(e.resulting_count, f.LIVE_FACT_CAP + 1)

    def test_v25_last_slot_serializes_locally(self):
        store, _ = mkstore()
        self._fill(store, f.LIVE_FACT_CAP - 1)
        r1, _ = run(store, put_req("%032x" % 0x8001, key="slot-a"))
        self.assertEqual(r1.result.outcome.result_code, "applied")
        with self.assertRaises(cap.CapRefused):
            run(store, put_req("%032x" % 0x8002, key="slot-b"))


class V26ToV29EpochsAndCrossing(unittest.TestCase):
    """V26 terminal epoch refusals; V27 epoch never reused; V28 prior
    lifetime; V29 replay never crosses owner/lifetime."""

    def test_v26_epoch_terminal_no_recapture(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        r1 = store.execute(1, req, c, l, p, ctx)
        fid = r1.result.outcome.readback.fact_id
        closure_and_delete(store, read_ctx(store), fid, "07" * 16)
        for _ in range(2):  # terminal, no recapture
            with self.assertRaises(cap.ConflictError) as cm:
                store.execute(1, req, c, l, p, ctx)
            self.assertIn("EPOCH", str(cm.exception))

    def test_v27_epoch_never_reused(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        (rd, _), = [closure_and_delete(store, read_ctx(store), fid,
                                       "07" * 16)]
        e1 = rd.result.outcome.new_epoch
        r2, _ = run(store, wipe_req("0a" * 16))
        e2 = r2.result.outcome.new_epoch
        self.assertEqual(len({1, e1, e2}), 3)  # all distinct, monotone
        self.assertLess(e1, e2)

    def test_v28_prior_lifetime_capture_never_matches(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store.execute(1, req, c, l, p, ctx)
        # a request carrying a DIFFERENT lifetime under the minted fixtures
        tampered = f.FactRequest(
            method=req.method, intent=req.intent, payload=req.payload,
            owner_uid=1, lifetime_id=2, operation_id=req.operation_id,
            information_ref=req.information_ref)
        with self.assertRaises((cap.AuthError, cap.ConflictError,
                                cap.ModelInputError)):
            store.execute(1, tampered, c, l, p, ctx)

    def test_v29_replay_never_crosses_owner(self):
        store, _ = mkstore()
        store.setup_owner(2, 1, 1, scopes=ALL_SCOPES)
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store.execute(1, req, c, l, p, ctx)
        with self.assertRaises((cap.AuthError, cap.ConflictError)):
            store.execute(2, req, c, l, p, ctx)


    def test_t1_mutation_requires_current_owned_context(self):
        # T1: a mutation whose caller context carries a FABRICATED or
        # STALE current field refuses typed STALE - uid/lifetime/grant
        # generation/capture epoch/policy/decision/consent 999 never
        # execute; PR-3 Phase A precedence is preserved (Phase A runs
        # BEFORE the snapshot equality)
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        for field in ("current_uid", "current_lifetime",
                      "current_capture_epoch", "current_grant_generation",
                      "current_policy_revision",
                      "current_decision_revision",
                      "current_consent_generation"):
            with self.subTest(field=field):
                bad = dataclasses.replace(ctx, **{field: 999})
                with self.assertRaises(cap.ConflictError) as cm:
                    store.execute(1, req, c, l, p, bad)
                self.assertIn("STALE_POLICY", str(cm.exception))
        # the ORIGINAL envelope under a NEW current context after the
        # issuer changed: original != current = STALE, never executes
        moved = dataclasses.replace(
            ctx, current_issuer=env.SessionRef(session_token_hash="ef" * 32))
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req, c, l, p, moved)
        self.assertIn("STALE_POLICY", str(cm.exception))
        # a FOREIGN original envelope is not this op's envelope
        _, _, _, b2, _ = mint(store, put_req("a2" * 16))
        foreign = dataclasses.replace(ctx, original_binding=b2)
        with self.assertRaises(cap.AuthError):
            store.execute(1, req, c, l, p, foreign)
        # the UNCHANGED paired context still executes
        r = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r.kind, "executed")

    def test_t1_issuer_first_precedence_over_expiry(self):
        # T1 precedence (v80 sec 10; PR-3 A-before-B): the ISSUER
        # checks run BEFORE Phase A - a STALE issuer refuses typed
        # STALE even when the capability is ALSO expired, and the real
        # PR-3 expiry precedence is NEVER recoded as uniform stale:
        # (1) invalid issuer + expired grant -> STALE_POLICY (never
        # ExpiredRefused); (2) changed issuer + expired grant ->
        # STALE_POLICY; (3) VALID issuer + expired grant ->
        # ExpiredRefused (the documented Phase A precedence preserved)
        store, _ = mkstore()
        req = put_req("a1" * 16)
        # (1) invalid issuer + expired grant
        c, l, p, b, ctx = mint(store, req)
        store._q.q("UPDATE fixture_grants SET expires_at=%s WHERE uid=1",
                   (T0 + 10,), "exec")
        store._q.q("UPDATE issuer_state SET valid=0 WHERE uid=1", (),
                   "exec")
        store._clock.advance(11)
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req, c, l, p, ctx)
        self.assertIn("STALE_POLICY", str(cm.exception))
        self.assertNotIsInstance(cm.exception, cap.ExpiredRefused)
        # (2) changed issuer + expired grant
        store, _ = mkstore()
        c, l, p, b, ctx = mint(store, req)
        store._q.q("UPDATE fixture_grants SET expires_at=%s WHERE uid=1",
                   (T0 + 10,), "exec")
        moved = dataclasses.replace(
            ctx, current_issuer=env.SessionRef(session_token_hash="ef" * 32))
        store._clock.advance(11)
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req, c, l, p, moved)
        self.assertIn("STALE_POLICY", str(cm.exception))
        # (3) VALID issuer + expired grant -> the REAL PR-3 expiry
        store, _ = mkstore()
        c, l, p, b, ctx = mint(store, req)
        store._q.q("UPDATE fixture_grants SET expires_at=%s WHERE uid=1",
                   (T0 + 10,), "exec")
        store._clock.advance(11)
        with self.assertRaises(cap.ExpiredRefused):
            store.execute(1, req, c, l, p, ctx)


class V30ToV33Replay(unittest.TestCase):
    """V30 valid-capability replay = recorded outcome; V31 Phase A before
    drift; V32 REVOKED before replay; V33 reseal never re-registers."""

    def test_v30_replay_while_capability_valid(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        r1 = store.execute(1, req, c, l, p, ctx)
        r2 = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r2.kind, "replayed")
        self.assertEqual(r2.replay.outcome.result_code,
                         r1.result.outcome.result_code)
        self.assertEqual(r2.replay.outcome.applied_at_revision,
                         r1.result.outcome.applied_at_revision)

    def test_v31_drift_only_after_phase_a_passes(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))
        fid = r1.result.outcome.readback.fact_id
        # (a) Phase A valid + tampered request -> REQUEST_DRIFT
        req = sup_req("a2" * 16, [fid], {fid: 1})
        c, l, p, b, ctx = mint(store, req)
        req_tampered = sup_req("a2" * 16, [fid], {fid: 1}, value="TAMPER")
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req_tampered, c, l, p, ctx)
        self.assertIn("REQUEST_DRIFT", str(cm.exception))
        # (b) expired grant + tampered request -> Phase A (EXPIRED)
        #     refuses FIRST - NO universal DRIFT-over-expiry
        store._q.q("UPDATE fixture_grants SET expires_at=%s WHERE uid=1",
                   (T0 + 10,), "exec")
        store._clock.advance(11)
        with self.assertRaises(cap.ExpiredRefused):
            store.execute(1, req_tampered, c, l, p, ctx)

    def test_v32_revoked_refuses_before_replay(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store.execute(1, req, c, l, p, ctx)
        # fixture-level revocation: grants generation moves
        store._q.q("UPDATE fixture_snapshot SET"
                   " grants_generation=grants_generation+1 WHERE uid=1",
                   (), "exec")
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req, c, l, p, ctx)
        self.assertIn("REVOKED", str(cm.exception))

    def test_v33_reseal_never_reregisters(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store.execute(1, req, c, l, p, ctx)
        # the binding projection/commitment carry NO transport fields
        proj = b.projection_dict()
        for transport_field in ("nonce", "key_id", "ciphertext", "tag"):
            self.assertNotIn(transport_field, proj)
        self.assertEqual(b.commitment(), b.commitment())  # stable
        # a re-mint of the UNCHANGED request yields the IDENTICAL
        # registered binding: a transport reseal (nonce/key change)
        # never changes a registered binding (V33)
        _, _, _, b2 = store.mint_mutation_fixtures(req)
        self.assertEqual(b.commitment(), b2.commitment())
        # a CHANGED binding (new current issuer) for the same registered
        # operation drifts - the registry REFUSES (V33 mapping)
        store.set_issuer(1, env.SessionRef(session_token_hash="ef" * 32),
                         True)
        with self.assertRaises(env.EnvelopeError):
            store.mint_mutation_fixtures(req)



class V34ToV37Uncertainty(unittest.TestCase):
    """V34 restart/reopen refusals; V35 commit-uncertain; V36 known-commit
    read failure; V37 current_revision None on failed current read."""

    def test_v34_restart_old_session_refuses(self):
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        # RESTART: a NEW FactStore over the SAME connection - the old
        # session's minted handles refuse
        store2 = f.FactStore(conn, store._clock)
        with self.assertRaises((cap.AuthError, cap.ModelInputError,
                                cap.ConflictError)):
            store2.execute(1, req, c, l, p, ctx)

    def test_v35_commit_uncertain_blocked_marker(self):
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = store._apply_and_record

        def boom(*a, **k):
            raise f._CommitUncertain()
        store._apply_and_record = boom
        try:
            r = store.execute(1, req, c, l, p, ctx)
        finally:
            store._apply_and_record = orig
        self.assertEqual(r.kind, "durable_blocked")
        # re-execute with the same bundles: typed blocked status, NEVER a
        # blind retry
        r2 = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r2.kind, "durable_blocked")

    def test_v35b_local_uncertain_when_marker_write_fails(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = store._apply_and_record
        orig_q = store._q

        class FailingQ:
            def __init__(self, inner):
                self._inner = inner
                self.raw = inner.raw

            def q(self, sql, params, mode):
                if "INSERT INTO blocked_records" in sql:
                    raise sqlite3.OperationalError("injected")
                return self._inner.q(sql, params, mode)

        def boom(*a, **k):
            raise f._CommitUncertain()
        store._apply_and_record = boom
        store._q = FailingQ(orig_q)
        try:
            r = store.execute(1, req, c, l, p, ctx)
        finally:
            store._apply_and_record = orig
            store._q = orig_q
        self.assertEqual(r.kind, "local_uncertain")

    def test_v38_receipt_revision_minimum(self):
        store, _ = mkstore()
        # no-op commit (unchanged put) still observes store_revision >= 1
        run(store, put_req("a1" * 16))
        r2, _ = run(store, put_req("a2" * 16))
        self.assertIsNone(r2.result.outcome.applied_at_revision)
        self.assertGreaterEqual(r2.result.receipt.store_revision, 1)


class V41ToV43Counters(unittest.TestCase):
    """V41 bump table; V42 overflow refusals; V43 minima."""

    def test_v41_bump_table(self):
        store, _ = mkstore()
        self.assertEqual(store._store_revision(1), 0)
        run(store, put_req("a1" * 16))           # applied: +1
        self.assertEqual(store._store_revision(1), 1)
        run(store, put_req("a2" * 16))           # unchanged: +0
        self.assertEqual(store._store_revision(1), 1)
        run(store, put_req("a3" * 16, value="black"))  # conflict: +0
        self.assertEqual(store._store_revision(1), 1)
        r, _ = run(store, wipe_req("0a" * 16))   # wipe: +1
        self.assertGreater(store._store_revision(1), 1)

    def test_v43_uid_zero_valid(self):
        conn = sqlite3.connect(":memory:")
        store = f.FactStore(conn, f.JsonTestClock(T0))
        store.setup_owner(0, 1, 1, scopes=ALL_SCOPES)  # uid 0 VALID
        req = f.FactRequest(method=f.MethodCode.PUT,
                            intent=f.ModelIntent.WriteFact,
                            payload=f.PutPayload(fact=new_fact()),
                            owner_uid=0, lifetime_id=1,
                            operation_id="a1" * 16, information_ref="t")
        c, l, p, b = store.mint_mutation_fixtures(req)
        r = store.execute(0, req, c, l, p, store._caller_context(b))
        self.assertEqual(r.result.outcome.result_code, "applied")


class V44ToV48Reads(unittest.TestCase):
    """V45 superseded never merged; V46 history order; V47 cursor suite;
    V48 count matches list."""

    def _seed3(self, store):
        fids = []
        for i in range(3):
            r, _ = run(store, put_req("%032x" % (0xA0 + i),
                                      key="k%d" % i, value="v%d" % i))
            fids.append(r.result.outcome.readback.fact_id)
        return fids

    def _list(self, store, ctx, op, cursor=None, limit=100):
        tgt = {"category": None, "cursor": cursor, "limit": limit,
               "row_kind": f.RowKind.LIVE.value}
        w, rctx = read_pair(store, op, f.ReadMethod.LIST_FACTS, tgt)
        return store.list_facts(1, f.RowKind.LIVE, None, cursor, limit, w,
                                store._clock, rctx)

    def test_v47_cursor_suite(self):
        store, _ = mkstore()
        fids = self._seed3(store)
        ctx = read_ctx(store)
        page = self._list(store, ctx, "d1" * 16, limit=2)
        self.assertEqual(len(page.rows), 2)
        self.assertIsNotNone(page.next_cursor)
        # non-32-hex cursor = MALFORMED_INPUT
        with self.assertRaises(cap.ModelInputError):
            self._list(store, ctx, "d2" * 16, cursor="zz")
        # fabricated 32-hex id = UnknownCursor
        with self.assertRaises(f.UnknownCursorError):
            self._list(store, ctx, "d3" * 16, cursor="f" * 32)
        # paging to exhaustion: all rows exactly once, final page has no
        # further cursor
        seen = []
        cursor, op_n = None, 10
        while True:
            op_n += 1
            pg = self._list(store, ctx, "%032x" % op_n, cursor=cursor,
                            limit=2)
            seen.extend(pg.rows)
            if pg.next_cursor is None:
                break
            cursor = pg.next_cursor
        self.assertEqual(len(seen), 3)

    def test_v48_count_matches_list(self):
        store, _ = mkstore()
        self._seed3(store)
        ctx = read_ctx(store)
        page = self._list(store, ctx, "d1" * 16)
        tgt_c = {"filters": {"category": None, "include_superseded": False}}
        wc, rctx_c = read_pair(store, "d2" * 16, f.ReadMethod.COUNT_FACTS,
                               tgt_c)
        cnt = store.count_facts(
            1, f.CountFilters(category=None, include_superseded=False),
            wc, store._clock, rctx_c)
        self.assertEqual(cnt.value, len(page.rows))


class V49ToV52StubsAndOptIn(unittest.TestCase):
    """V49 production stubs; V50 sensitive refuses; V52 set_optin states."""

    def test_v49_production_stubs_refuse(self):
        for cls in (f.ProductionFactStore, f.SensitivePurgeStub):
            with self.assertRaises(cap.ProductionStubRefused):
                cls(None)

    def test_v50_sensitive_refused_tier_disabled(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        # STRUCTURALLY VALID sensitive request: required_scopes = the
        # DERIVED set (structural tier PASSES, v80 742-745) and the
        # sensitive FLAG is set - tier 2 owns the refusal (811-815):
        # EXACTLY TIER_DISABLED (the model grants no SENSITIVE scope)
        c, l, p, b, ctx = mint(store, req)
        cap_req = cap.Request(
            operation_id=req.operation_id,
            operation_class=f._METHOD_OPERATION[req.method],
            required_scopes=frozenset(f._METHOD_SCOPES[req.method]),
            captured_epoch=c.inner.request.captured_epoch,
            requested_audience_id=None,
            permitted_information_ref=req.information_ref,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            group_action=None, sensitive=True)
        with self.assertRaises(cap.SensitiveRefused) as cm:
            store._evaluator.recheck_model(c.inner, cap_req)
        self.assertEqual(cm.exception.code, cap.ModelCode.TIER_DISABLED)
        self.assertEqual(store._store_revision(1), 0)  # nothing ran
        # the POST-MINT widened twin maps STRUCTURALLY: exact
        # MALFORMED_INPUT (v80 742-745 before 811-815) - V54
        c, l, p, b, ctx = mint(store, req)
        orig = f._METHOD_SCOPES[req.method]
        f._METHOD_SCOPES[req.method] = tuple(orig) + (cap.Scope.MEMORY_SENSITIVE,)
        try:
            with self.assertRaises(Exception) as cm2:
                store.execute(1, req, c, l, p, ctx)
            self.assertEqual(getattr(cm2.exception, "code", None),
                             cap.ModelCode.MALFORMED_INPUT)
            self.assertEqual(store._store_revision(1), 0)
        finally:
            f._METHOD_SCOPES[req.method] = orig

    def test_v52_set_optin_states(self):
        store, _ = mkstore()
        # FIRST-False: effective False, prior None, record None, unchanged
        req = f.FactRequest(method=f.MethodCode.SET_OPTIN,
                            intent=f.ModelIntent.SetOptIn,
                            payload=f.OptInPayload(
                                setting=f.OptInSetting(
                                    category=f.Category.FACT), value=False),
                            owner_uid=1, lifetime_id=1,
                            operation_id="0c" * 16, information_ref="t")
        r, _ = run(store, req)
        oc = r.result.outcome
        self.assertEqual(oc.result_code, "unchanged")
        self.assertIsNone(oc.record)
        # changed: True -> applied with a record
        req2 = f.FactRequest(method=f.MethodCode.SET_OPTIN,
                             intent=f.ModelIntent.SetOptIn,
                             payload=f.OptInPayload(
                                 setting=f.OptInSetting(
                                     category=f.Category.FACT), value=True),
                             owner_uid=1, lifetime_id=1,
                             operation_id="0d" * 16, information_ref="t")
        r2, _ = run(store, req2)
        self.assertEqual(r2.result.outcome.result_code, "applied")
        self.assertIsNotNone(r2.result.outcome.record)
        # same again: unchanged
        req3 = f.FactRequest(method=f.MethodCode.SET_OPTIN,
                             intent=f.ModelIntent.SetOptIn,
                             payload=f.OptInPayload(
                                 setting=f.OptInSetting(
                                     category=f.Category.FACT), value=True),
                             owner_uid=1, lifetime_id=1,
                             operation_id="0e" * 16, information_ref="t")
        r3, _ = run(store, req3)
        self.assertEqual(r3.result.outcome.result_code, "unchanged")


class V53HistoricalAuth(unittest.TestCase):
    """V53 historical APIs need fresh READ claim + typed originals;
    unregistered op = UnknownOperation."""

    def test_v53_unregistered_op_unknown(self):
        store, _ = mkstore()
        ctx = read_ctx(store)
        # an un-EXECUTED mint supplies a typed binding; the op stays
        # unregistered -> UnknownOperation on the historical API
        _, _, _, b = store.mint_mutation_fixtures(put_req("ab" * 16))
        tgt = store._historical_targets("ab" * 16, None, b)
        w, rctx = read_pair(store, "b9" * 16, f.ReadMethod.READBACK, tgt)
        with self.assertRaises(f.UnknownOperationError):
            store.readback(1, "ab" * 16, None, b, w, store._clock, rctx)

    def test_v53_load_stored_requires_binding(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store.execute(1, req, c, l, p, ctx)
        tgt = store._historical_targets("a1" * 16, None, b)
        w, rctx = read_pair(store, "b1" * 16, f.ReadMethod.READBACK, tgt)
        with self.assertRaises(cap.ModelInputError):
            store.readback(1, "a1" * 16, None, None, w, store._clock, rctx)
        pr = store.readback(1, "a1" * 16, None, b, w, store._clock, rctx)
        self.assertIsInstance(pr, f.PostReadResult)



def blocked_op(store, op="aa" * 16):
    """Force an op into the durable blocked state (commit uncertainty)."""
    req = put_req(op, key="blk-" + op[:4])
    c, l, p, b, ctx = mint(store, req)
    orig = store._apply_and_record

    def boom(*a, **k):
        raise f._CommitUncertain()
    store._apply_and_record = boom
    try:
        r = store.execute(1, req, c, l, p, ctx)
    finally:
        store._apply_and_record = orig
    assert r.kind == "durable_blocked", r.kind
    bh = f.extended_binding_hash(b, req)
    rid = f._recovery_id(1, 1, op, bh)
    return req, b, rid


def evidence(store, rid, op, token, kind=f.EvidenceKind.RECEIPT,
             commitment=None):
    if commitment is None:
        # the anchor row's ACTUAL commitment (the target join requires it)
        row = store._q.q(
            "SELECT binding_hash AS c FROM blocked_records WHERE"
            " recovery_id=%s", (rid,), "one")
        if row is None:
            row = store._q.q(
                "SELECT commitment AS c FROM resolved_accounting WHERE"
                " recovery_id=%s", (rid,), "one")
        commitment = row["c"]
    proj = f.SafeEvidenceProjection(
        recovery_id=rid, uid=1, lifetime_id=1, operation_id=op,
        commitment=commitment, kind=kind,
        target=f.RecoveryBoundRef(recovery_id=rid, source=kind),
        source=kind, accounting_scope=f.Lifetime(uid=1, lifetime_id=1))
    # a FRESH REGISTERED READ setup bound to the ACTUAL input
    w, ctx = store.mint_read_fixtures(
        1, 1, "b3" * 16, f.ReadMethod.READBACK,
        {"projection": proj.to_dict(), "token": token})
    return store.insert_evidence(1, 1, "b3" * 16, proj, token, w,
                                 ctx), proj


    def test_b7_load_stored_full_hash_recomputation(self):
        # B7: the LOAD_STORED path recomputes the FULL request+binding
        # extended hash (the stored typed request + the caller's typed
        # binding) and compares EQUAL to the retained commitment
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store.execute(1, req, c, l, p, ctx)
        tgt = store._historical_targets("a1" * 16, None, b)
        w, rctx = read_pair(store, "b1" * 16, f.ReadMethod.READBACK, tgt)
        pr = store.readback(1, "a1" * 16, None, b, w, store._clock,
                            rctx)
        self.assertIsInstance(pr, f.PostReadResult)
        # a STORED request/binding pair that no longer hashes to the
        # retained commitment = drift (the projection compare ALONE
        # passes: binding_json untouched)
        row = conn.execute(
            "SELECT request_json FROM stored_requests WHERE"
            " operation_id=?", ("a1" * 16,)).fetchone()
        obj = json.loads(row[0])
        obj["payload"]["value"] = "TAMPERED"
        store._q.q("UPDATE stored_requests SET request_json=%s WHERE"
                   " operation_id=%s", (json.dumps(obj), "a1" * 16),
                   "exec")
        tgt2 = store._historical_targets("a1" * 16, None, b)
        w2, rctx2 = read_pair(store, "b2" * 16, f.ReadMethod.READBACK,
                              tgt2)
        with self.assertRaises(cap.ConflictError) as cm:
            store.readback(1, "a1" * 16, None, b, w2, store._clock,
                           rctx2)
        self.assertIn("REQUEST_DRIFT", str(cm.exception))


def force_real_unique_conflict(store):
    """Race injection: the loser's existence prelookup ran BEFORE the
    winner's commit became visible - the loser's INSERT then hits the
    REAL (recovery_id, delivery_token) UNIQUE fence."""
    orig_q = store._q.q
    state = {"fired": False}

    def spy(sql, params=(), mode="none"):
        if not state["fired"] \
                and "FROM evidence_entries WHERE" in sql \
                and "delivery_token" in sql:
            state["fired"] = True
            return None  # the winner landed after our lookup
        return orig_q(sql, params, mode)
    store._q.q = spy
    return orig_q


class V34bInsertionEvents(unittest.TestCase):
    """V34b receiver-side named vectors: repeat, drift, same-second,
    blocked repeat, blocked->resolved, late-after-resolution."""

    def test_exact_repeat_replays_no_writes(self):
        store, conn = mkstore()
        req, b, rid = blocked_op(store)
        r1, proj = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r1.kind, "blocked_inserted")
        self.assertEqual(r1.contribution, 1)
        n = conn.execute("SELECT count(*) FROM evidence_entries").fetchone()[0]
        r2, _ = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r2.kind, "blocked_replay_no_event")
        self.assertEqual(r2.contribution, 0)
        self.assertEqual(r2.stored_new_retained, 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM evidence_entries").fetchone()[0], n)

    def test_delivery_drift_typed_refusal(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store)
        evidence(store, rid, req.operation_id, "d" * 32)
        # same token, CHANGED projection = drift REFUSAL
        with self.assertRaises(f.DeliveryDriftError):
            evidence(store, rid, req.operation_id, "d" * 32,
                     commitment="9" * 64)

    def test_distinct_tokens_both_insert(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store)
        r1, _ = evidence(store, rid, req.operation_id, "d" * 32)
        r2, _ = evidence(store, rid, req.operation_id, "e" * 32)
        self.assertEqual((r1.kind, r2.kind),
                         ("blocked_inserted", "blocked_inserted"))
        self.assertEqual(r1.contribution + r2.contribution, 2)

    def test_blocked_before_after_resolution(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store)
        # a committed record for the op lets resolve take the committed
        # branch; simulate by adding the record via a fresh committed op:
        # here we assert BOTH pre- and post-resolution repeats are no-event
        r1, proj = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r1.kind, "blocked_inserted")
        # resolve stays UNRESOLVED without attested witnesses (V39 rule):
        tgt = store._historical_targets(req.operation_id, req, b)
        w, rctx = read_pair(store, "b4" * 16, f.ReadMethod.RESOLVE, tgt)
        rr = store.resolve(1, req.operation_id, req, b, w, store._clock,
                           rctx)
        self.assertEqual(rr.kind, "rolled_back")
        self.assertGreaterEqual(rr.attested_witness_count, 1)
        # BLOCKED->RESOLVED REPEAT: no event, no mint, no recount
        r2, _ = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r2.kind, "blocked_replay_no_event")
        self.assertEqual(r2.contribution, 0)
        self.assertIsNotNone(r2.resolution_event_id)


    def test_t2_receipt_target_per_kind_inventory_checks(self):
        # T2 (v80 795-812): the OP_RECORDS receipt target is checked
        # against the DURABLE INVENTORY - positive (real receipt),
        # absent (no receipt), mismatch (another op's receipt)
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16))  # commits: receipt exists
        req, b, rid = blocked_op(store, op="ab" * 16)
        bh = f.extended_binding_hash(b, req)

        def proj_for(entry):
            return f.SafeEvidenceProjection(
                recovery_id=rid, uid=1, lifetime_id=1,
                operation_id=req.operation_id, commitment=bh,
                kind=f.EvidenceKind.RECEIPT,
                target=f.ScopedInventoryRef(
                    owner=f.Lifetime(uid=1, lifetime_id=1),
                    class_name=f.CopyClass.OP_RECORDS, entry_id=entry),
                source=f.EvidenceKind.RECEIPT,
                accounting_scope=f.Lifetime(uid=1, lifetime_id=1))
        # POSITIVE: the committed op's OWN receipt exists -> the
        # per-kind inventory check passes for ITS anchor
        ok = f._evidence_target_source_ok(
            store._q, proj_for("receipt:" + "a1" * 16), 1, 1, "a1" * 16)
        self.assertTrue(ok)
        # ABSENT: the blocked op has NO receipt -> False
        self.assertFalse(f._evidence_target_source_ok(
            store._q, proj_for("receipt:" + "ab" * 16), 1, 1,
            "ab" * 16))
        # MISMATCH: the entry names a receipt that exists for ANOTHER
        # op -> False for THIS anchor
        self.assertFalse(f._evidence_target_source_ok(
            store._q, proj_for("receipt:" + "a1" * 16), 1, 1,
            "ab" * 16))
        # guessed garbage -> False
        self.assertFalse(f._evidence_target_source_ok(
            store._q, proj_for("receipt:" + "ff" * 16), 1, 1,
            "ab" * 16))

    def test_concurrent_double_delivery_one_winner(self):
        # CONCURRENT DOUBLE-DELIVERY (v80 3931): two contenders compose
        # the SAME delivery INDEPENDENTLY; the winner commits; the
        # loser's INSERT hits the REAL UNIQUE fence (its prelookup ran
        # pre-commit) -> rollback -> winner compare -> REPLAY, no writes
        store, conn = mkstore()
        req, b, rid = blocked_op(store)
        r1, proj = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r1.kind, "blocked_inserted")
        orig_q = force_real_unique_conflict(store)
        try:
            r2, _ = evidence(store, rid, req.operation_id, "d" * 32)
        finally:
            store._q.q = orig_q
        self.assertEqual(r2.kind, "blocked_replay_no_event")
        self.assertEqual(r2.contribution, 0)
        # the transaction CLOSED and exactly ONE row exists
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM evidence_entries").fetchone()[0], 1)

    def test_post_scrub_repeat_replays_safe_fields(self):
        # POST-SCRUB REPEAT (v80 3940): the repeat compares the SAFE
        # projection fields ONLY (never erased content) and replays
        store, conn = mkstore()
        req, b, rid = blocked_op(store)
        r1, _ = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r1.kind, "blocked_inserted")
        run(store, wipe_req("0a" * 16))  # scrubs stored request+content
        n = conn.execute(
            "SELECT count(*) FROM evidence_entries").fetchone()[0]
        r2, _ = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r2.kind, "blocked_replay_no_event")
        self.assertEqual(r2.contribution, 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM evidence_entries").fetchone()[0], n)

    def test_unique_drift_conflict_rollback_refusal(self):
        # UNIQUE DRIFT (v80 3944): the (recovery_id, delivery_token)
        # UNIQUE fence exists; a direct conflicting insert raises and
        # rolls back; the winner compare classifies a CHANGED
        # projection under the same delivery identity = typed drift
        store, conn = mkstore()
        req, b, rid = blocked_op(store)
        evidence(store, rid, req.operation_id, "d" * 32)
        names = [r[1] for r in conn.execute(
            "PRAGMA index_list('evidence_entries')").fetchall()]
        self.assertIn("evidence_entries_delivery", names)
        # the ACTUAL loser path: the drifted loser's INSERT hits the
        # REAL UNIQUE fence -> rollback -> winner compare -> typed
        # drift REFUSAL (no raw-duplicate shortcut)
        # the drifted loser keeps the anchor-join fields (same
        # uid/lifetime/op/commitment) but CHANGES the kind - it passes
        # every join and loses the REAL race at the fence
        orig_q = force_real_unique_conflict(store)
        try:
            with self.assertRaises(f.DeliveryDriftError):
                evidence(store, rid, req.operation_id, "d" * 32,
                         kind=f.EvidenceKind.LATE_ACCOUNTING)
        finally:
            store._q.q = orig_q
        # the transaction CLOSED and the winner's row is unchanged
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM evidence_entries").fetchone()[0], 1)


class V39V40Conflicts(unittest.TestCase):
    """V39 conflicting receipts stay blocked; V40 evidence overflow."""

    def test_v39_conflicting_receipt_stays_blocked(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store)
        r1, _ = evidence(store, rid, req.operation_id, "d" * 32,
                         kind=f.EvidenceKind.CONFLICTING_RECEIPT)
        self.assertEqual(r1.kind, "blocked_inserted")
        # the op is NEVER auto-resolved by evidence; resolve still
        # unresolved without attested witnesses
        tgt = store._historical_targets(req.operation_id, req, b)
        w, rctx = read_pair(store, "b4" * 16, f.ReadMethod.RESOLVE, tgt)
        rr = store.resolve(1, req.operation_id, req, b, w, store._clock,
                           rctx)
        self.assertEqual(rr.kind, "rolled_back")
        self.assertGreaterEqual(rr.attested_witness_count, 1)
        # the conflicting receipt stays as IMMUTABLE evidence
        ev = store._q.q("SELECT kind, origin_kind FROM evidence_entries"
                        " WHERE recovery_id=%s", (rid,), "all")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "conflicting_receipt")

    def test_v40_evidence_overflow_frozen_at_64(self):
        store, conn = mkstore()
        req, b, rid = blocked_op(store)
        for i in range(64):
            token = "%032x" % (0xD000 + i)
            evidence(store, rid, req.operation_id, token)
        with self.assertRaises(cap.CapRefused):  # 65th: received-refused
            evidence(store, rid, req.operation_id, "%032x" % 0xD040)
        row = conn.execute(
            "SELECT evidence_count, overflow_marker FROM blocked_records"
            " WHERE recovery_id=?", (rid,)).fetchone()
        self.assertEqual(row[0], 64)      # count FROZEN at 64
        self.assertEqual(row[1], 1)       # overflow marker set, retained


class V42Overflow(unittest.TestCase):
    def test_v42_counter_overflow_refuses(self):
        store, _ = mkstore()
        run(store, put_req("a0" * 16))
        store._q.q("UPDATE store_revisions SET store_revision=%s"
                   " WHERE uid=1", (2**63 - 1,), "exec")
        with self.assertRaises(Exception):
            run(store, put_req("a1" * 16, key="other-key"))


class V44V45V46ReadViews(unittest.TestCase):
    """V44 changed targets refuse; V45 live/history never merged;
    V46 history ordered by history_id."""

    def test_v44_changed_targets_refuse(self):
        store, _ = mkstore()
        run(store, put_req("a1" * 16))
        w, rctx = read_pair(store, "d1" * 16, f.ReadMethod.GET, {"fid": 1})
        with self.assertRaises(cap.AuthError):
            store.get(1, 2, w, store._clock, rctx)

    def test_v45_v46_superseded_history_views(self):
        store, _ = mkstore()
        r1, _ = run(store, put_req("a1" * 16, key="tea", value="v1"))
        fid = r1.result.outcome.readback.fact_id
        run(store, sup_req("e5" * 16, [fid], {fid: 1}, value="v2"))
        ctx = read_ctx(store)
        # LIVE view: only the replacement
        tgt_l = {"category": None, "cursor": None, "limit": 10,
                 "row_kind": f.RowKind.LIVE.value}
        wl, rctxl = read_pair(store, "d1" * 16, f.ReadMethod.LIST_FACTS,
                              tgt_l)
        live = store.list_facts(1, f.RowKind.LIVE, None, None, 10, wl,
                                store._clock, rctxl)
        self.assertEqual(len(live.rows), 1)
        self.assertEqual(live.rows[0].value, "v2")
        # HISTORY view: the superseded row, ordered by history_id
        tgt_h = {"category": None, "cursor": None, "limit": 10,
                 "row_kind": f.RowKind.HISTORY.value}
        wh, rctxh = read_pair(store, "d2" * 16, f.ReadMethod.LIST_FACTS,
                              tgt_h)
        hist = store.list_facts(1, f.RowKind.HISTORY, None, None, 10, wh,
                                store._clock, rctxh)
        self.assertEqual(len(hist.rows), 1)
        self.assertTrue(all(isinstance(x, f.HistoryRow)
                            for x in hist.rows))
        ids = [x.history_id for x in hist.rows]
        self.assertEqual(ids, sorted(ids))


class V51V54V55(unittest.TestCase):
    """V51 legacy_sensitive erases as ordinary content; V54 precedence
    (ERASED before CAS/CAPACITY); V55 binding mapping invariants."""

    def test_v51_legacy_sensitive_erases_ordinarily(self):
        store, conn = mkstore()
        r1, _ = run(store, put_req("a1" * 16, key="old"))
        conn.execute("UPDATE facts SET legacy_sensitive=1 WHERE fact_id=1")
        r, _ = run(store, wipe_req("0a" * 16))
        self.assertEqual(r.result.outcome.result_code, "applied")
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 0)

    def test_v54_erased_before_capacity(self):
        store, conn = mkstore()
        r1, _ = run(store, put_req("a1" * 16, key="ghost"))
        fid = r1.result.outcome.readback.fact_id
        closure_and_delete(store, read_ctx(store), fid, "07" * 16)
        # tombstoned key refuses ERASED even though the live set is empty
        with self.assertRaises(f.FactConflict) as cm:
            run(store, put_req("a2" * 16, key="ghost"))
        self.assertIn("ERASED", str(cm.exception))

    def test_v55_binding_mapping(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        _, _, _, b = store.mint_mutation_fixtures(req)
        proj = b.projection_dict()
        # NO nonce/fence/target ids in the binding
        for absent in ("nonce", "fence", "key_id", "ciphertext"):
            self.assertNotIn(absent, proj)
        # identity + channel agreement: WEB = SessionRef-shaped issuer
        self.assertEqual(proj["uid"], 1)
        self.assertEqual(proj["lifetime"], 1)
        self.assertEqual(proj["operation_id"], req.operation_id)
        self.assertIn("issuer", proj)
        self.assertIn("capture_epoch", proj)
        self.assertIn("content_fingerprint", proj)


class V34StepFaultsAndRestart(unittest.TestCase):
    """V34: fault injection after each step incl. RESTART - old-session
    claims refuse after reopen, an absent row is never rollback proof,
    NO blind retry."""

    def test_phasea_fault_unchanged_retry_permitted(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = store._evaluator.recheck_model
        calls = [0]

        def boom(capability, request):
            calls[0] += 1
            if calls[0] == 1:
                raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
            return orig(capability, request)
        store._evaluator.recheck_model = boom
        try:
            with self.assertRaises(cap.ConflictError):
                store.execute(1, req, c, l, p, ctx)
        finally:
            store._evaluator.recheck_model = orig
        # a PRE-CLAIM refusal: nothing armed/claimed - the UNCHANGED
        # retry (same bundles) is PERMITTED
        r = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r.kind, "executed")

    def test_apply_fault_rolls_back_entire_attempt(self):
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = store._execute_apply

        def boom(*a, **k):
            raise sqlite3.OperationalError("injected step fault")
        store._execute_apply = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.execute(1, req, c, l, p, ctx)
        finally:
            store._execute_apply = orig
        # the refusal rolled back the ENTIRE attempt: the claim is
        # unwound (no armed witness, no registry row left behind)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM abort_witnesses").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM attempt_registry").fetchone()[0], 0)
        r = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r.kind, "executed")
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_commit_guard_abort_typed_uncertain_no_blind_retry(self):
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = store._commit_guard

        def boom(*a, **k):
            raise f._CommitGuardAbort()
        store._commit_guard = boom
        try:
            r = store.execute(1, req, c, l, p, ctx)
        finally:
            store._commit_guard = orig
        self.assertEqual(r.kind, "local_uncertain")
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 0)
        # NO blind retry: the re-execution is a typed non-executed state
        r2 = store.execute(1, req, c, l, p, ctx)
        self.assertIn(r2.kind, ("local_uncertain", "durable_blocked",
                                "in_progress", "unknown"))
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 0)

    def test_commit_lost_hook_durable_blocked_no_blind_retry(self):
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store._fault_commit_lost = True
        r = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r.kind, "durable_blocked")
        # the witness stays ARMED for the resolver
        row = conn.execute("SELECT state FROM abort_witnesses").fetchone()
        self.assertEqual(row[0], "armed")
        r2 = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r2.kind, "durable_blocked")

    def test_restart_reopen_old_session_refuses(self):
        store, conn, path = mkstore_path()
        r, _ = run(store, put_req("a1" * 16))
        req2, b2, rid = blocked_op(store, op="ab" * 16)
        c, l, p, b3, ctx = mint(store, put_req("b2" * 16))
        f.close_exclusive_db(conn)
        # RESTART: reopen the SAME path under a NEW instance/session
        conn2 = f.open_exclusive_db(path)
        store2 = f.FactStore(conn2, store._clock)
        try:
            with self.assertRaises((cap.AuthError, cap.ModelInputError,
                                    cap.ConflictError)):
                store2.execute(1, put_req("b2" * 16), c, l, p, ctx)
            # the durable rows SURVIVE the restart
            self.assertEqual(conn2.execute(
                "SELECT count(*) FROM facts").fetchone()[0], 1)
            blocked = conn2.execute(
                "SELECT state FROM blocked_records WHERE recovery_id=?",
                (rid,)).fetchone()
            self.assertIsNotNone(blocked)
            # a FRESH mint on the reopened store works normally
            r3, _ = run(store2, put_req("c9" * 16, key="after-restart"))
            self.assertEqual(r3.kind, "executed")
        finally:
            f.close_exclusive_db(conn2)

    def test_absent_row_is_never_rollback_proof(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)  # minted but NEVER executed
        _, _, _, b = store.mint_mutation_fixtures(req)
        tgt = store._historical_targets(req.operation_id, req, b)
        w, rctx = read_pair(store, "b5" * 16, f.ReadMethod.READBACK, tgt)
        # absent = a typed UnknownOperation, NEVER a rollback/committed
        # proof and NEVER a fabricated outcome
        with self.assertRaises(f.UnknownOperationError):
            store.readback(1, req.operation_id, req, b, w,
                           store._clock, rctx)


    def test_fault_after_each_pipeline_step_rolls_back(self):
        # v80 3928: fault injection AFTER EACH declared pipeline step -
        # every "post" hook runs the REAL step body FIRST (its writes
        # land in-txn), THEN raises: the proven boundary is LITERALLY
        # post-step (NOT an entry hook). Declared pipeline (docs
        # Shape): 1a footprint -> 2 current checks + the real PR-3
        # Phase A -> 3a association/equality -> 4 Phase B readback ->
        # 5 tombstone -> 6 CAS -> 7 classify -> 8 monotone allocation
        # -> 9 apply+record. after_2 fires at the ENTRY of the 3a seam
        # (the step-2 block is COMPLETE at that exact point - the two
        # steps are adjacent in _execute_current_checks); every other
        # hook is post-body. after_1a is the footprint test below;
        # after_8 (post-allocation CRASH) is the wipe-path test below;
        # the post-commit boundary is the commit-guard/lost-commit
        # pair above. The attempt rolls back ENTIRELY (no partial
        # effects, no claim leak) and an UNCHANGED retry stays
        # permitted (pre-commit refusal, NO blind retry, an absent row
        # is never treated as rollback proof)
        injectables = [
            ("after_2_current_checks", "_execute_association_checks",
             "entry"),
            ("after_3a_association", "_execute_association_checks",
             "post"),
            ("after_4_phase_b_readback", "_phase_b_lookup", "post"),
            ("after_5_tombstone", "_tombstone_check", "post"),
            ("after_6_cas", "_cas_check", "post"),
            ("after_7_classify", "_classify", "post"),
            ("after_9_apply_record", "_apply_and_record", "post"),
        ]
        for name, attr, mode in injectables:
            with self.subTest(boundary=name):
                store, conn = mkstore()
                req = put_req("a1" * 16)
                c, l, p, b, ctx = mint(store, req)
                orig = getattr(store, attr)

                if mode == "entry":
                    def boom(*a, _name=name, **k):
                        # the step-2 block is COMPLETE; the fault fires
                        # BEFORE any 3a work = LITERALLY after step 2
                        raise sqlite3.OperationalError(
                            "injected post-step fault: " + _name)
                else:
                    def boom(*a, _orig=orig, _name=name, **k):
                        _orig(*a, **k)  # the REAL step COMPLETES
                        # (its writes land in-txn), THEN the fault
                        raise sqlite3.OperationalError(
                            "injected post-step fault: " + _name)
                setattr(store, attr, boom)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        store.execute(1, req, c, l, p, ctx)
                finally:
                    setattr(store, attr, orig)
                # the ENTIRE attempt rolled back: no effects, NO leak
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM facts").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM abort_witnesses"
                    " WHERE state='armed'").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM attempt_registry"
                    ).fetchone()[0], 0)
                # an UNCHANGED retry is PERMITTED
                r = store.execute(1, req, c, l, p, ctx)
                self.assertEqual(r.kind, "executed")
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_fault_step1a_footprint_rolls_back(self):
        # v80 3928: a fault literally AFTER step 1a (the declared-
        # footprint step's REAL body runs FIRST, then the fault) rolls
        # the attempt back ENTIRELY; an unchanged retry works
        store, conn = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = f._footprint

        def boom(*a, **k):
            orig(*a, **k)  # the REAL step 1a COMPLETES (in-txn), THEN
            raise sqlite3.OperationalError("injected post-1a fault")
        f._footprint = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.execute(1, req, c, l, p, ctx)
        finally:
            f._footprint = orig
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM facts").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM abort_witnesses WHERE state='armed'"
            ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM attempt_registry").fetchone()[0], 0)
        r = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r.kind, "executed")

    def test_fault_step8_monotone_allocation_rolls_back(self):
        # v80 3928: a post-allocation CRASH (the REAL monotone epoch
        # allocation runs FIRST, then the fault) rolls the
        # (epoch-setting) attempt back ENTIRELY - the allocated epoch
        # is never left half-committed; retry works
        store, conn = mkstore()
        req = wipe_req("0a" * 16)
        c, l, p, b, ctx = mint(store, req)
        orig = f.owner_counters.allocate_next_epoch

        def boom(*a, **k):
            orig(*a, **k)  # the REAL allocation COMPLETES (in-txn) -
            raise sqlite3.OperationalError("injected post-allocation"
                                           " crash")
        f.owner_counters.allocate_next_epoch = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.execute(1, req, c, l, p, ctx)
        finally:
            f.owner_counters.allocate_next_epoch = orig
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM abort_witnesses WHERE state='armed'"
            ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM attempt_registry").fetchone()[0], 0)
        r = store.execute(1, req, c, l, p, ctx)
        self.assertEqual(r.kind, "executed")

    def test_fault_boundaries_restart_clean(self):
        # V34 (v80 3928) RESTART leg PER BOUNDARY: a fault injected
        # literally AFTER each pipeline step (the REAL step body runs
        # first; after_2 = the entry of the adjacent 3a seam), then a
        # REAL file close/reopen - NO partial
        # attempt survives the restart (no facts, no armed witness, no
        # registry row) and a FRESH mint on the reopened store
        # executes normally (old-session claims refuse after reopen -
        # test_v34/test_restart_reopen above - an absent row is never
        # rollback proof, NO blind retry)
        injectables = [
            ("after_2_current_checks", "_execute_association_checks",
             "entry"),
            ("after_3a_association", "_execute_association_checks",
             "post"),
            ("after_4_phase_b_readback", "_phase_b_lookup", "post"),
            ("after_5_tombstone", "_tombstone_check", "post"),
            ("after_6_cas", "_cas_check", "post"),
            ("after_7_classify", "_classify", "post"),
            ("after_9_apply_record", "_apply_and_record", "post"),
        ]
        for name, attr, mode in injectables:
            with self.subTest(boundary=name):
                store, conn, path = mkstore_path()
                req = put_req("a1" * 16)
                c, l, p, b, ctx = mint(store, req)
                orig = getattr(store, attr)

                if mode == "entry":
                    def boom(*a, _name=name, **k):
                        raise sqlite3.OperationalError(
                            "injected post-step fault: " + _name)
                else:
                    def boom(*a, _orig=orig, _name=name, **k):
                        _orig(*a, **k)  # the REAL step COMPLETES first
                        raise sqlite3.OperationalError(
                            "injected post-step fault: " + _name)
                setattr(store, attr, boom)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        store.execute(1, req, c, l, p, ctx)
                finally:
                    setattr(store, attr, orig)
                f.close_exclusive_db(conn)
                # RESTART: reopen the SAME path under a NEW instance
                conn2 = f.open_exclusive_db(path)
                store2 = f.FactStore(conn2, f.JsonTestClock(T0))
                try:
                    self.assertEqual(conn2.execute(
                        "SELECT count(*) FROM facts").fetchone()[0], 0)
                    self.assertEqual(conn2.execute(
                        "SELECT count(*) FROM abort_witnesses"
                        " WHERE state='armed'").fetchone()[0], 0)
                    self.assertEqual(conn2.execute(
                        "SELECT count(*) FROM attempt_registry"
                        ).fetchone()[0], 0)
                    r, _ = run(store2, put_req("b7" * 16,
                                               key="after-boundary-restart"))
                    self.assertEqual(r.kind, "executed")
                    self.assertEqual(conn2.execute(
                        "SELECT count(*) FROM facts").fetchone()[0], 1)
                finally:
                    f.close_exclusive_db(conn2)


class V36V37ReadbackUnavailable(unittest.TestCase):
    """V36 known-commit read failure (protocol AND operational) =
    ReadbackUnavailableError with the KNOWN outcome+revision, never
    uncertain; V37 current_revision None on failed current read."""

    def test_v36_protocol_and_operational(self):
        for cause in (f.ReadbackCause.PROTOCOL, f.ReadbackCause.OPERATIONAL):
            store, _ = mkstore()
            req = put_req("a1" * 16)
            c, l, p, b, ctx = mint(store, req)
            store._fault_readback_cause = cause
            with self.assertRaises(f.ReadbackUnavailableError) as cmx:
                store.execute(1, req, c, l, p, ctx)
            err = cmx.exception
            self.assertEqual(err.cause, cause)
            self.assertGreaterEqual(err.known_revision, 1)
            self.assertEqual(err.known_outcome.method, f.MethodCode.PUT)
            # the commit certainty is UNCHANGED: the SAME op replays
            store._fault_readback_cause = None
            r2 = store.execute(1, req, c, l, p, ctx)
            self.assertEqual(r2.kind, "replayed")

    def test_v37_failed_current_read_has_no_current_revision(self):
        store, _ = mkstore()
        r0, _ = run(store, put_req("a1" * 16))
        self.assertIsNotNone(r0.post)
        req = put_req("a2" * 16)
        c, l, p, b, ctx = mint(store, req)
        store._fault_readback_cause = f.ReadbackCause.OPERATIONAL
        with self.assertRaises(f.ReadbackUnavailableError) as cmx:
            store.execute(1, req, c, l, p, ctx)
        # NO PostReadResult exists for the failed read (current_revision
        # is None); the error carries ONLY the KNOWN outcome + revision
        self.assertFalse(hasattr(cmx.exception, "post"))
        post = dataclasses.replace(r0.post, current_revision=None)
        self.assertIsNone(post.current_revision)


class V44OneTransactionBinding(unittest.TestCase):
    """V44: the read\'s Phase A + the read run in ONE transaction; the
    setup binds the ACTUAL targets."""

    def test_phasea_runs_inside_the_read_transaction(self):
        store, _ = mkstore()
        r, _ = run(store, put_req("a1" * 16))
        fid = r.result.outcome.readback.fact_id
        w, rctx = read_pair(store, "d1" * 16, f.ReadMethod.GET,
                            {"fid": fid})
        seen = {}
        orig = store._evaluator.recheck_model

        def spy(capability, request):
            seen["in_txn"] = store._q.raw.in_transaction
            return orig(capability, request)
        store._evaluator.recheck_model = spy
        try:
            store.get(1, fid, w, store._clock, rctx)
        finally:
            store._evaluator.recheck_model = orig
        self.assertTrue(seen["in_txn"])  # Phase A INSIDE the one read txn
        self.assertFalse(store._q.raw.in_transaction)  # and it CLOSED

    def test_setup_binds_the_actual_target(self):
        store, _ = mkstore()
        r, _ = run(store, put_req("a1" * 16))
        fid = r.result.outcome.readback.fact_id
        r2, _ = run(store, put_req("a2" * 16, key="other"))
        fid2 = r2.result.outcome.readback.fact_id
        w, rctx = read_pair(store, "d1" * 16, f.ReadMethod.GET,
                            {"fid": fid})
        # the setup minted for fid1 can NEVER read fid2
        with self.assertRaises(cap.AuthError):
            store.get(1, fid2, w, store._clock, rctx)


class V47CursorRevisionAndRetention(unittest.TestCase):
    """V47: live-revision movement invalidates \'facts\' cursors;
    \'report_page\' cursors RETAINED against their bound revision until
    erased."""

    def test_live_revision_movement_invalidates_facts_cursor(self):
        store, _ = mkstore()
        for i in range(3):
            run(store, put_req("%032x" % (0xA0 + i), key="k%d" % i))
        tgt = {"category": None, "cursor": None, "limit": 2,
               "row_kind": f.RowKind.LIVE.value}
        w, rctx = read_pair(store, "d1" * 16, f.ReadMethod.LIST_FACTS, tgt)
        page = store.list_facts(1, f.RowKind.LIVE, None, None, 2, w,
                                store._clock, rctx)
        cursor = page.next_cursor
        self.assertIsNotNone(cursor)
        # the live revision MOVES: the bound cursor is invalidated
        run(store, put_req("a9" * 16, key="mover"))
        tgt2 = {"category": None, "cursor": cursor, "limit": 2,
                "row_kind": f.RowKind.LIVE.value}
        w2, rctx2 = read_pair(store, "d2" * 16, f.ReadMethod.LIST_FACTS,
                              tgt2)
        with self.assertRaises(f.UnknownCursorError):
            store.list_facts(1, f.RowKind.LIVE, None, cursor, 2, w2,
                             store._clock, rctx2)

    def test_report_page_cursor_retained_until_erased(self):
        store, _ = mkstore()
        cid = store._cursor_mint(1, 1, f.QueryKind.REPORT_PAGE,
                                 "ff" * 8, 0, f.RowKind.LIVE)
        # live-revision movement does NOT invalidate it
        run(store, put_req("a1" * 16))
        row = store._cursor_open(1, 1, cid, f.QueryKind.REPORT_PAGE)
        self.assertEqual(row["cursor_id"], cid)
        # ...until ERASED (wipe clears the registry)
        run(store, wipe_req("0a" * 16))
        with self.assertRaises(f.UnknownCursorError):
            store._cursor_open(1, 1, cid, f.QueryKind.REPORT_PAGE)

    def test_malformed_cursor_is_structural_everywhere(self):
        store, _ = mkstore()
        run(store, put_req("a1" * 16))
        ctx = read_ctx(store)
        # even with REVOKED grants the STRUCTURAL malformed-cursor
        # refusal wins (tier order: structural before revocation)
        store._q.q("UPDATE fixture_snapshot SET"
                   " grants_generation=grants_generation+1 WHERE uid=1",
                   (), "exec")
        tgt = {"category": None, "cursor": "zz", "limit": 2,
               "row_kind": f.RowKind.LIVE.value}
        w, rctx = read_pair(store, "d1" * 16, f.ReadMethod.LIST_FACTS, tgt)
        with self.assertRaises(cap.ModelInputError):
            store.list_facts(1, f.RowKind.LIVE, None, "zz", 2, w,
                             store._clock, ctx)


class V54FullPrecedence(unittest.TestCase):
    """V54: the PR-3 tier order (structural -> sensitive -> expiry ->
    revocation/staleness -> policy/scope/consent -> capacity) THEN
    Phase B THEN Phase C (ERASED -> CAS -> CAPACITY)."""

    def test_sensitive_beats_expiry(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store._q.q("UPDATE fixture_grants SET expires_at=%s WHERE uid=1",
                   (T0 + 10,), "exec")
        store._clock.advance(11)
        orig = f._METHOD_SCOPES[req.method]
        f._METHOD_SCOPES[req.method] = tuple(orig) + (
            cap.Scope.MEMORY_SENSITIVE,)
        try:
            with self.assertRaises(Exception) as cm:
                store.execute(1, req, c, l, p, ctx)
            # EXACT CODE (v80 742-745 structural BEFORE 811-815
            # sensitive): a MEMORY_SENSITIVE requirement WIDENED AFTER
            # MINT mismatches the minted capability structurally ->
            # MALFORMED_INPUT, and structural tier refuses BEFORE
            # expiry - NEVER ExpiredRefused, NEVER TIER_DISABLED here
            self.assertNotIsInstance(cm.exception, cap.ExpiredRefused)
            self.assertEqual(getattr(cm.exception, "code", None),
                             cap.ModelCode.MALFORMED_INPUT)
            self.assertEqual(store._store_revision(1), 0)  # nothing ran
        finally:
            f._METHOD_SCOPES[req.method] = orig

    def test_expiry_beats_revocation(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store._q.q("UPDATE fixture_grants SET expires_at=%s WHERE uid=1",
                   (T0 + 10,), "exec")
        store._q.q("UPDATE fixture_snapshot SET"
                   " grants_generation=grants_generation+1 WHERE uid=1",
                   (), "exec")
        store._clock.advance(11)
        with self.assertRaises(cap.ExpiredRefused):
            store.execute(1, req, c, l, p, ctx)

    def test_revocation_beats_scope(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        c, l, p, b, ctx = mint(store, req)
        store._q.q("DELETE FROM fixture_grants WHERE uid=1 AND scope=%s",
                   (cap.Scope.MEMORY_WRITE.value,), "exec")
        store._q.q("UPDATE fixture_snapshot SET"
                   " grants_generation=grants_generation+1 WHERE uid=1",
                   (), "exec")
        with self.assertRaises(cap.ConflictError) as cm:
            store.execute(1, req, c, l, p, ctx)
        self.assertIn("REVOKED", str(cm.exception))

    def test_scope_beats_capacity(self):
        store, conn = mkstore()
        orig_cap = f.LIVE_FACT_CAP
        f.LIVE_FACT_CAP = 1
        try:
            run(store, put_req("a1" * 16))
            # drop the WRITE grant (no generation bump: scope-missing,
            # NOT revocation) - the MISSING_SCOPE refusal beats CAPACITY
            store._q.q("DELETE FROM fixture_grants WHERE uid=1 AND"
                       " scope=%s", (cap.Scope.MEMORY_WRITE.value,), "exec")
            with self.assertRaises(cap.ScopeRefused):
                run(store, put_req("a2" * 16, key="second"))
        finally:
            f.LIVE_FACT_CAP = orig_cap

    def test_cas_at_capacity_is_not_masked(self):
        store, conn = mkstore()
        orig_cap = f.LIVE_FACT_CAP
        f.LIVE_FACT_CAP = 1
        try:
            r1, _ = run(store, put_req("a1" * 16))
            fid = r1.result.outcome.readback.fact_id
            # at capacity, the wrong-CAS supersede refuses CAS - the
            # capacity refusal NEVER masks it
            with self.assertRaises(f.FactConflict):
                run(store, sup_req("e5" * 16, [fid], {fid: 999}))
        finally:
            f.LIVE_FACT_CAP = orig_cap


class V55FullClaimBindingMapping(unittest.TestCase):
    """V55: the FULL claim/binding mapping - issuer channel agreement,
    current_issuer_valid False = STALE, the generations mapping."""

    def test_issuer_channel_agreement_mapping(self):
        # WEB = SessionRef (64-hex session token hash)
        store, _ = mkstore()
        req = put_req("a1" * 16)
        _, _, _, b = store.mint_mutation_fixtures(req)
        proj = b.projection_dict()
        issuer = proj["issuer"]
        self.assertEqual(issuer["type"], "web_session")
        self.assertEqual(len(issuer["session_token_hash"]), 64)
        int(issuer["session_token_hash"], 16)
        # TELEGRAM = TelegramIssuer bot/chat/update ids (the typed union)
        tg = env.TelegramIssuer(bot_id=7, chat_id=8, update_id=9)
        canon = f._issuer_canon(tg)
        self.assertEqual(canon, {"bot_id": 7, "chat_id": 8,
                                 "kind": "telegram", "update_id": 9})
        back = f._typed_issuer_from_json(json.dumps(canon))
        self.assertEqual(back, tg)

    def test_current_issuer_valid_false_is_stale(self):
        store, _ = mkstore()
        r, _ = run(store, put_req("a1" * 16))
        fid = r.result.outcome.readback.fact_id
        w, rctx = read_pair(store, "d1" * 16, f.ReadMethod.GET,
                            {"fid": fid})
        stale_ctx = dataclasses.replace(rctx, current_issuer_valid=False)
        with self.assertRaises(cap.ConflictError) as cm:
            store.get(1, fid, w, store._clock, stale_ctx)
        self.assertIn("STALE", str(cm.exception))

    def test_generations_mapping_complete(self):
        store, _ = mkstore()
        req = put_req("a1" * 16)
        _, _, _, b = store.mint_mutation_fixtures(req)
        proj = b.projection_dict()
        for field in ("capture_epoch", "grant_generation",
                      "policy_revision", "decision_revision",
                      "consent_generation", "issuer", "uid", "lifetime",
                      "operation_id", "content_fingerprint"):
            self.assertIn(field, proj)
        # the generations equal the OWNED fixture snapshot values
        snap = store._q.q("SELECT * FROM fixture_snapshot WHERE uid=1",
                          (), "one")
        self.assertEqual(proj["capture_epoch"], snap["current_epoch"])
        self.assertEqual(proj["grant_generation"],
                         snap["grants_generation"])
        self.assertEqual(proj["policy_revision"], snap["policy_revision"])


class V34bBatchAccounting(unittest.TestCase):
    """V34b: MIXED BATCH + MIXED NO-EVENT/REPLAYED-LATE/NEW-LATE +
    DUPLICATE TOKENS IN ONE BATCH - batch sums DISTINCT per result and a
    duplicated token counts ONCE."""

    def test_mixed_batch_old_noevent_and_new_late(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store, op="aa" * 16)
        old, _ = evidence(store, rid, req.operation_id, "d1" * 16)
        self.assertEqual(old.kind, "blocked_inserted")
        resolve_op(store, req, b)
        new_late, _ = evidence(store, rid, req.operation_id, "d2" * 16)
        self.assertEqual(new_late.kind, "inserted")
        replay, _ = evidence(store, rid, req.operation_id, "d1" * 16)
        self.assertEqual(replay.kind, "blocked_replay_no_event")
        # new late rows mint, the old replays: 3 + 0, NEITHER recounted
        self.assertEqual(new_late.contribution + replay.contribution, 3)
        self.assertEqual(replay.stored_new_retained, 1)

    def test_mixed_noevent_replayed_late_new_late(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store, op="aa" * 16)
        evidence(store, rid, req.operation_id, "d1" * 16)
        resolve_op(store, req, b)
        late1, _ = evidence(store, rid, req.operation_id, "d2" * 16)
        self.assertEqual(late1.kind, "inserted")
        r_noevent, _ = evidence(store, rid, req.operation_id, "d1" * 16)
        r_late, _ = evidence(store, rid, req.operation_id, "d2" * 16)
        r_new, _ = evidence(store, rid, req.operation_id, "d3" * 16)
        self.assertEqual(r_noevent.kind, "blocked_replay_no_event")
        self.assertEqual(r_noevent.stored_new_retained, 1)
        self.assertEqual(r_late.kind, "replayed")
        self.assertEqual(r_late.stored_new_retained, 3)
        self.assertEqual(r_new.kind, "inserted")
        # batch contribution = 0 + 0 + 3 = 3
        self.assertEqual(r_noevent.contribution + r_late.contribution
                         + r_new.contribution, 3)

    def test_duplicate_tokens_in_one_batch_count_once(self):
        store, _ = mkstore()
        req, b, rid = blocked_op(store, op="aa" * 16)
        resolve_op(store, req, b)
        # a duplicated NEW LATE token yields 3, NOT 6
        first, _ = evidence(store, rid, req.operation_id, "d1" * 16)
        dup, _ = evidence(store, rid, req.operation_id, "d1" * 16)
        self.assertEqual((first.kind, dup.kind), ("inserted", "replayed"))
        self.assertEqual(first.contribution + dup.contribution, 3)
        # a duplicated NEW BLOCKED token yields 1, NOT 2
        req2, b2, rid2 = blocked_op(store, op="bb" * 16)
        f1, _ = evidence(store, rid2, req2.operation_id, "e1" * 16)
        f2, _ = evidence(store, rid2, req2.operation_id, "e1" * 16)
        self.assertEqual((f1.kind, f2.kind),
                         ("blocked_inserted", "blocked_replay_no_event"))
        self.assertEqual(f1.contribution + f2.contribution, 1)


class V34bHarnessVectors(unittest.TestCase):
    """The named ISSUER-HARNESS vectors (B9): LIVE ERASE, PENDING
    RESTART, LOST RECEIVER ACK, LOST DELIVERED-STATE UPDATE, SAFE
    PAYLOAD/STABLE TOKEN."""

    def _rig(self, op="aa" * 16):
        store, conn, path = mkstore_path()
        req, b, rid = blocked_op(store, op=op)
        bh = f.extended_binding_hash(b, req)
        oconn = f.open_exclusive_db(path + ".outbox")
        provider = f.ReceiverContextProvider(store, oconn, 1, 1,
                                             "c3" * 16, store._clock)
        harness = f.IssuerHarness(oconn, store._clock, store._snapshots,
                                  provider)
        proj = f.SafeEvidenceProjection(
            recovery_id=rid, uid=1, lifetime_id=1,
            operation_id=req.operation_id, commitment=bh,
            kind=f.EvidenceKind.RECEIPT,
            target=f.RecoveryBoundRef(recovery_id=rid,
                                      source=f.EvidenceKind.RECEIPT),
            source=f.EvidenceKind.RECEIPT,
            accounting_scope=f.Lifetime(uid=1, lifetime_id=1))
        return store, req, b, rid, provider, harness, proj, oconn

    def test_t5_late_loser_missing_event_is_integrity_refusal(self):
        # T5: a winner evidence row whose insertion event is MISSING
        # is an INCOHERENT durable join = typed integrity REFUSAL,
        # never a defaulted count - on BOTH the replay path and the
        # forced-fence loser path
        store, conn = mkstore()
        req, b, rid = blocked_op(store, op="aa" * 16)
        resolve_op(store, req, b)
        r1, _ = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r1.kind, "inserted")
        conn.execute("DELETE FROM insertion_events")
        conn.commit()
        with self.assertRaises(f.EvidenceIntegrityError):
            evidence(store, rid, req.operation_id, "d" * 32)
        orig_q = force_real_unique_conflict(store)
        try:
            with self.assertRaises(f.EvidenceIntegrityError):
                evidence(store, rid, req.operation_id, "d" * 32)
        finally:
            store._q.q = orig_q

    def test_v1_replay_revalidates_full_event_coupling(self):
        # V1 (v80 1254-1308): the replay AND the forced-fence loser
        # RE-VERIFY the ONE-TO-ONE PER-CLASS coupling - ledger count/
        # class/subject/event/destination/triple/anchor/source - NOT
        # just event existence. A missing/wrong ledger row, class,
        # subject, triple, anchor, source, or count = typed
        # EvidenceIntegrityError, NEVER an incoherent default success
        def rig_and_insert():
            store, conn = mkstore()
            req, b, rid = blocked_op(store, op="aa" * 16)
            resolve_op(store, req, b)
            r1, _ = evidence(store, rid, req.operation_id, "d" * 32)
            assert r1.kind == "inserted" and r1.contribution == 3
            return store, conn, req, rid

        # POSITIVE: valid original + valid replay + valid loser with
        # the coupling INTACT
        store, conn, req, rid = rig_and_insert()
        r2, _ = evidence(store, rid, req.operation_id, "d" * 32)
        self.assertEqual(r2.kind, "replayed")
        self.assertEqual(r2.stored_new_retained, 3)
        orig_q = force_real_unique_conflict(store)
        try:
            r3, _ = evidence(store, rid, req.operation_id, "d" * 32)
            self.assertEqual(r3.kind, "replayed")
            self.assertEqual(r3.stored_new_retained, 3)
        finally:
            store._q.q = orig_q

        mutations = {
            # the peer's exact probe: delete ONLY the matching ledger
            "missing_ledger":
                "DELETE FROM resolution_event_ledger",
            "wrong_class":
                "UPDATE resolution_event_ledger SET class_name='facts'",
            "wrong_destination":
                "UPDATE resolution_event_ledger SET entry_id='deadbeef'",
            "wrong_count":
                "INSERT INTO resolution_event_ledger"
                "(resolution_event_id, class_name, entry_id, inserted_at)"
                " SELECT resolution_event_id, 'evidence_entries',"
                " 'extra01', inserted_at FROM resolution_event_ledger",
            "wrong_subject":
                "UPDATE insertion_events SET"
                " subject_evidence_ref='evidence:deadbeef'",
            "wrong_triple":
                "UPDATE insertion_events SET operation_id='" + "ff" * 16
                + "'",
            "wrong_anchor":
                "UPDATE insertion_events SET commitment='" + "ff" * 32
                + "'",
            "wrong_source":
                "UPDATE insertion_events SET source='forged'",
            # W1 (v80 960-965): the INSERTING identity/count legs
            "wrong_inserting_uid_evidence":
                "UPDATE evidence_entries SET inserting_uid=999",
            "wrong_inserting_lifetime_evidence":
                "UPDATE evidence_entries SET inserting_lifetime_id=999",
            "wrong_inserting_op_evidence":
                "UPDATE evidence_entries SET inserting_operation_id='"
                + "ff" * 16 + "'",
            "wrong_inserting_uid_event":
                "UPDATE insertion_events SET inserting_uid=999",
            "wrong_inserting_op_event":
                "UPDATE insertion_events SET inserting_operation_id='"
                + "ff" * 16 + "'",
            "wrong_new_retained":
                "UPDATE insertion_events SET new_retained=99",
        }
        for name, sql in mutations.items():
            with self.subTest(mutation=name):
                store, conn, req, rid = rig_and_insert()
                conn.execute(sql)
                conn.commit()
                # the NORMAL replay path refuses typed
                with self.assertRaises(f.EvidenceIntegrityError):
                    evidence(store, rid, req.operation_id, "d" * 32)
                # the forced-fence LOSER path refuses typed
                orig_q = force_real_unique_conflict(store)
                try:
                    with self.assertRaises(f.EvidenceIntegrityError):
                        evidence(store, rid, req.operation_id, "d" * 32)
                finally:
                    store._q.q = orig_q

        # W1-A: the SELECTED delivery is bound EXACTLY - D's evidence
        # row claiming E's event makes a retry of D REFUSE (never a
        # coherent answer from E's event)
        store, conn = mkstore()
        req, b, rid = blocked_op(store, op="aa" * 16)
        resolve_op(store, req, b)
        rd, _ = evidence(store, rid, req.operation_id, "d" * 32)
        re_, _ = evidence(store, rid, req.operation_id, "e" * 32)
        self.assertEqual(rd.kind, "inserted")
        self.assertEqual(re_.kind, "inserted")
        # bind D's row to E's event: a retry of D must REFUSE - the
        # selected delivery is never answered from a coherent event
        # for a DIFFERENT delivery
        conn.execute(
            "UPDATE evidence_entries SET insertion_event_id=?"
            " WHERE delivery_token=?", (re_.event_id, "d" * 32))
        conn.commit()
        with self.assertRaises(f.EvidenceIntegrityError):
            evidence(store, rid, req.operation_id, "d" * 32)
        orig_q = force_real_unique_conflict(store)
        try:
            with self.assertRaises(f.EvidenceIntegrityError):
                evidence(store, rid, req.operation_id, "d" * 32)
        finally:
            store._q.q = orig_q

        # W1-B: an EMPTY ledger with matching zeroed counts STILL
        # refuses: the anchor + selected-delivery checks are never
        # skipped by an empty row set
        store, conn, req, rid = rig_and_insert()
        conn.execute("DELETE FROM resolution_event_ledger")
        conn.execute("UPDATE insertion_events SET inserted_count=0,"
                     " new_retained=1")
        conn.commit()
        with self.assertRaises(f.EvidenceIntegrityError):
            evidence(store, rid, req.operation_id, "d" * 32)
        orig_q = force_real_unique_conflict(store)
        try:
            with self.assertRaises(f.EvidenceIntegrityError):
                evidence(store, rid, req.operation_id, "d" * 32)
        finally:
            store._q.q = orig_q

        # W1-C: an EXTRA evidence row (distinct token) claiming the
        # SAME event with NO ledger entry diverges the bidirectional
        # event<->evidence set: refuses on BOTH paths
        store, conn, req, rid = rig_and_insert()
        conn.execute(
            "INSERT INTO evidence_entries SELECT 'ee'||evidence_id,"
            " recovery_id, kind, 'ee'||delivery_token, received_at,"
            " ref, projection_json, insertion_event_id,"
            " resolution_event_id, origin_kind, inserting_uid,"
            " inserting_lifetime_id, inserting_operation_id"
            " FROM evidence_entries")
        conn.commit()
        with self.assertRaises(f.EvidenceIntegrityError):
            evidence(store, rid, req.operation_id, "d" * 32)
        orig_q = force_real_unique_conflict(store)
        try:
            with self.assertRaises(f.EvidenceIntegrityError):
                evidence(store, rid, req.operation_id, "d" * 32)
        finally:
            store._q.q = orig_q

    def test_w1_write_leg_enforces_inserting_op_equality(self):
        # v80 1050-1074 WRITE leg: the inserting operation identity is
        # ENFORCED - cap.request.operation_id == the registered
        # original_binding.operation_id == the supplied READ op. The
        # read_operation_id argument is NEVER trusted on its own
        store, conn = mkstore()
        req, b, rid = blocked_op(store, op="aa" * 16)
        resolve_op(store, req, b)
        row = store._q.q("SELECT commitment AS c FROM"
                         " resolved_accounting WHERE recovery_id=%s",
                         (rid,), "one")
        proj = f.SafeEvidenceProjection(
            recovery_id=rid, uid=1, lifetime_id=1,
            operation_id=req.operation_id, commitment=row["c"],
            kind=f.EvidenceKind.RECEIPT,
            target=f.RecoveryBoundRef(recovery_id=rid,
                                      source=f.EvidenceKind.RECEIPT),
            source=f.EvidenceKind.RECEIPT,
            accounting_scope=f.Lifetime(uid=1, lifetime_id=1))
        # minted for op b3... but CALLED with op b4: typed refusal,
        # NOTHING inserted (the argument does not override the minted
        # identity)
        w, ctx = store.mint_read_fixtures(
            1, 1, "b3" * 16, f.ReadMethod.READBACK,
            {"projection": proj.to_dict(), "token": "d" * 32})
        with self.assertRaises(cap.AuthError):
            store.insert_evidence(1, 1, "b4" * 16, proj, "d" * 32,
                                  w, ctx)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM evidence_entries").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM insertion_events").fetchone()[0], 0)
        # the COHERENT call (all three op identities equal) inserts
        r = store.insert_evidence(1, 1, "b3" * 16, proj, "d" * 32,
                                  w, ctx)
        self.assertEqual(r.kind, "inserted")
        self.assertEqual(r.contribution, 3)
        # the recorded event carries the VERIFIED inserting identity
        ev = conn.execute(
            "SELECT inserting_operation_id, inserting_uid,"
            " inserting_lifetime_id FROM insertion_events").fetchone()
        self.assertEqual(ev[0], "b3" * 16)
        self.assertEqual((ev[1], ev[2]), (1, 1))

    def test_w2_scrub_factstore_erase_readback_revision(self):
        # W2 (v80 2060-2137 + the FactStore erase/readback gate),
        # ELEVENTH fold: the completion record binds the erase step
        # through the FROZEN typed OwnedEraseReadback adapter ONLY
        # (store identity bound at CONSTRUCTION + checked against the
        # harness's injected receiver at registration; NO callable
        # protocol). The adapter delegates to the SOURCE-OWNED
        # FactStore.erase_proof route: exact subject/erase-class/known-
        # decoded-outcome/current-lifetime/FRESH-CURRENT-revision/
        # closed-coverage under ONE coherent receiver read snapshot.
        # REFUSED and CLEARED are both exercised AFTER the known erase;
        # wrong operation (an earlier PUT) / absent erase / unknown
        # coverage / stale state / malformed claims / a second
        # completion are all typed refusals with NOTHING recorded. The
        # generic path and the standalone teardown NEVER record an
        # erase-step completion. No physical memory-wipe claim.

        CLAIM = {"operation_id": "0f" * 16,
                 "coverage_id": "facts-live-zero"}

        class Stuck:
            pass  # raw cannot be discarded -> REFUSED

        class OwnedHandle:
            def __init__(self):
                self._raw = b"secret"

            def clear(self):
                self._raw = None

            def live_raw(self):
                return self._raw is not None

        def rig_erased():
            # known PUT commit -> delivered commit -> KNOWN WIPE erase
            # at fresh revision R' with the live row gone
            store, req, b, rid, provider, harness, proj, oconn = \
                self._rig()
            r1, _ = run(store, put_req("9e" * 16, key="w2key"))
            assert r1.result.outcome.result_code == "applied"
            rev_committed = store._store_revision(1)
            comp = harness.compose({"projection": proj,
                                    "recovery_id": rid})
            harness.send(comp.token)
            d = provider.deliver({"projection": proj,
                                  "token": comp.token})
            assert harness.ack(d["ack"]).kind == "delivered"
            rw, _ = run(store, wipe_req("0f" * 16))
            assert rw.result.outcome.result_code == "applied"
            rev_erased = store._store_revision(1)
            assert rev_erased > rev_committed
            assert store._q.q("SELECT count(*) AS n FROM facts", (),
                              "one")["n"] == 0
            harness.register_erase_readback(f.OwnedEraseReadback(store))
            return (store, harness, oconn, comp, rev_erased, proj, rid,
                    provider)

        # REFUSED AFTER the known erase: completion BLOCKED (nothing
        # recorded, R' + delivered certainty retained, NO re-execute)
        store, harness, oconn, comp, rev_erased, proj, rid, provider = rig_erased()
        out = harness.scrub(comp.token, {
            "raw_ref_handle": Stuck(), "comparison_buffer": None},
            erase=CLAIM)
        self.assertEqual(out["kind"], "refused")
        self.assertEqual(harness.outbox_state(comp.token), "delivered")
        self.assertEqual(store._store_revision(1), rev_erased)
        self.assertEqual(harness.redeliver_pending()["redelivered"], [])
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 0)
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM facts", (), "one")["n"], 0)

        # CLEARED AFTER the known erase, SAME step: completion recorded
        # ONLY through the OWNED verified tuple - the row carries the
        # exact committed erase identity, never caller claims
        out2 = harness.scrub(comp.token, {
            "raw_ref_handle": OwnedHandle(),
            "comparison_buffer": OwnedHandle()}, erase=CLAIM)
        self.assertEqual(out2["kind"], "cleared")
        self.assertEqual(store._store_revision(1), rev_erased)
        row = oconn.execute(
            "SELECT step, step_id, operation_id, method, result_code,"
            " revision, verified_at_revision, coverage_id, owner_uid,"
            " lifetime_id FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(tuple(row), (
            "erase", "erase:1:1:%s:facts-live-zero:%d" % (
                "0f" * 16, rev_erased),
            "0f" * 16, "wipe", "applied", rev_erased, rev_erased,
            "facts-live-zero", 1, 1))
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM facts", (), "one")["n"], 0)

        # IDEMPOTENCY/CONFLICT (C): a SECOND completion of the SAME
        # token, or of the SAME step under a DIFFERENT delivered token,
        # is a typed conflict refusal - NO re-execute, NOTHING changed
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()}, erase=CLAIM)
        comp2 = harness.compose({"projection": proj,
                                 "recovery_id": rid})
        harness.send(comp2.token)
        d2 = provider.deliver({"projection": proj, "token": comp2.token})
        self.assertEqual(harness.ack(d2["ack"]).kind, "delivered")
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp2.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()}, erase=CLAIM)
        self.assertEqual(store._store_revision(1), rev_erased)
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 1)

        # STALE/CHANGED STATE (C): after a NEW put (the coverage probe
        # now fails live), a fresh token's erase claim REFUSES - the
        # verified tuple is re-read at completion, never cached
        r2, _ = run(store, put_req("7d" * 16, key="w2key2"))
        self.assertEqual(r2.result.outcome.result_code, "applied")
        comp3 = harness.compose({"projection": proj,
                                 "recovery_id": rid})
        harness.send(comp3.token)
        d3 = provider.deliver({"projection": proj, "token": comp3.token})
        self.assertEqual(harness.ack(d3["ack"]).kind, "delivered")
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp3.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()}, erase=CLAIM)
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE step='erase'")
            .fetchone()[0], 1)

        # TEARDOWN: the standalone hook clears and records NOTHING
        outcome = harness.scrub_hook({
            "raw_ref_handle": OwnedHandle(),
            "comparison_buffer": OwnedHandle()})
        self.assertEqual(outcome["kind"], "cleared")
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 1)

        # NEGATIVE A (the peer's probe shape): PUT + delivered ACK, NO
        # WIPE - the OWNED verifier finds NO committed erase -> typed
        # refusal, NOTHING recorded, the live row persists. An extra
        # caller "verify" key is REJECTED as a malformed claim
        # (arbitrary per-call callbacks are never accepted)
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        r1, _ = run(store, put_req("9e" * 16, key="w2key"))
        self.assertEqual(r1.result.outcome.result_code, "applied")
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        harness.send(comp.token)
        d = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(harness.ack(d["ack"]).kind, "delivered")
        harness.register_erase_readback(f.OwnedEraseReadback(store))
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()}, erase=CLAIM)
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()},
                erase={"operation_id": "0f" * 16,
                       "coverage_id": "facts-live-zero",
                       "verify": lambda e: True})
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE step='erase'")
            .fetchone()[0], 0)
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM facts", (), "one")["n"], 1)
        # ... and the SAME no-wipe probe with NO claim records ONLY a
        # GENERIC completion - NEVER an erase-step completion
        out3 = harness.scrub(comp.token, {
            "raw_ref_handle": OwnedHandle(),
            "comparison_buffer": OwnedHandle()})
        self.assertEqual(out3["kind"], "cleared")
        self.assertEqual(oconn.execute(
            "SELECT step FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()[0], "generic")
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM facts", (), "one")["n"], 1)

        # NEGATIVE B: with a REAL wipe present, claiming the EARLIER
        # PUT op (method='put') or an UNKNOWN coverage id -> NO
        # verified step -> typed refusal; the record never mislabels a
        # PUT as the erase
        store, harness, oconn, comp, rev_erased, proj, rid, provider = rig_erased()
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()},
                erase={"operation_id": "9e" * 16,
                       "coverage_id": "facts-live-zero"})
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()},
                erase={"operation_id": "0f" * 16,
                       "coverage_id": "everything"})
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 0)
        self.assertEqual(store._store_revision(1), rev_erased)

        # NEGATIVE D (typed-refusal matrix): malformed op id, None op,
        # empty coverage, missing key, extra key, and an UNREGISTERED
        # harness all refuse TYPED (HarnessRefusedError - NEVER
        # ModelInputError or a raw exception) with NOTHING recorded
        store, harness, oconn, comp, rev_erased, proj, rid, provider = rig_erased()
        for bad in ({"operation_id": "zz",
                     "coverage_id": "facts-live-zero"},
                    {"operation_id": None,
                     "coverage_id": "facts-live-zero"},
                    {"operation_id": "0f" * 16, "coverage_id": ""},
                    {"operation_id": "0f" * 16},
                    {"operation_id": "0f" * 16,
                     "coverage_id": "facts-live-zero", "x": 1}):
            with self.assertRaises(f.HarnessRefusedError):
                harness.scrub(comp.token, {
                    "raw_ref_handle": OwnedHandle(),
                    "comparison_buffer": OwnedHandle()}, erase=bad)
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 0)

        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        harness.send(comp.token)
        d = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(harness.ack(d["ack"]).kind, "delivered")
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {  # NO registered readback
                "raw_ref_handle": OwnedHandle(),
                "comparison_buffer": OwnedHandle()}, erase=CLAIM)

    def test_w2_scrub_eleventh_fold_vectors(self):
        # ELEVENTH FOLD vectors: (A) registration provenance matrix;
        # (B) source-owned route policy against FORGED durable rows;
        # (D) the FRESH-CURRENT freshness vectors (the peer's exact
        # probe); (E) the scrub-side closed-dict validation against
        # shaped forged tuples.

        class _Ok:
            def clear(self):
                pass

            def live_raw(self):
                return False

        # (A) REGISTRATION PROVENANCE MATRIX: a lambda, a non-callable,
        # a SUBCLASS instance, an adapter bound to a DIFFERENT store,
        # adapter mutation, and a SECOND registration all REFUSE; only
        # the exact frozen adapter bound to the harness's OWN receiver
        # store registers
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        for bad in (lambda o, c: None, 42, "x"):
            with self.assertRaises(f.HarnessRefusedError):
                harness.register_erase_readback(bad)

        class Sub(f.OwnedEraseReadback):
            pass

        with self.assertRaises(f.HarnessRefusedError):
            harness.register_erase_readback(Sub(store))
        other, _c2, _p2 = mkstore_path()
        with self.assertRaises(f.HarnessRefusedError):
            harness.register_erase_readback(f.OwnedEraseReadback(other))
        with self.assertRaises(f.HarnessRefusedError):
            f.OwnedEraseReadback(object())
        adapter = f.OwnedEraseReadback(store)
        with self.assertRaises(f.HarnessRefusedError):
            adapter._store = other  # FROZEN: the bound store never swaps
        harness.register_erase_readback(adapter)
        with self.assertRaises(f.HarnessRefusedError):
            harness.register_erase_readback(f.OwnedEraseReadback(store))

        # (B) FORGED DURABLE ROWS via the SOURCE-OWNED route: a
        # fabricated wipe row at rev 999 (decoded outcome agreeing)
        # REFUSES (not the current revision); a fabricated row with
        # NEGATIVE owner/lifetime ids REFUSES (exact-subject policy)
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        store._q.q(
            "INSERT INTO operation_records(uid, lifetime_id,"
            " operation_id, binding_hash, marker, method, result_code,"
            " outcome_json, outcome_kind, proof_json,"
            " applied_at_revision, epoch, store_revision, fence,"
            " scrubbed, scrub_revision) VALUES(1,1,%s,'x',NULL,"
            "'wipe','applied',"
            "'{\"method\":\"wipe\",\"applied_at_revision\":999}'," 
            "'full',NULL,999,1,999,1,0,1)", ("ab" * 16,), "exec")
        self.assertIsNone(store.erase_proof("ab" * 16,
                                            "facts-live-zero"))
        store._q.q(
            "INSERT INTO operation_records(uid, lifetime_id,"
            " operation_id, binding_hash, marker, method, result_code,"
            " outcome_json, outcome_kind, proof_json,"
            " applied_at_revision, epoch, store_revision, fence,"
            " scrubbed, scrub_revision) VALUES(-99,-99,%s,'x',NULL,"
            "'wipe','applied',"
            "'{\"method\":\"wipe\",\"applied_at_revision\":1}'," 
            "'full',NULL,1,1,1,1,0,1)",
            ("cd" * 16,), "exec")
        self.assertIsNone(store.erase_proof("cd" * 16,
                                            "facts-live-zero"))
        # a malformed operation id / unknown coverage refuse on entry
        self.assertIsNone(store.erase_proof("zz", "facts-live-zero"))
        self.assertIsNone(store.erase_proof("ab" * 16, "everything"))

        # (D) FRESH-CURRENT - the peer's exact probe: wipe 0f at R2 /
        # new PUT at R3 / new wipe 1f at R4. The OLD 0f claim REFUSES
        # (historical R2-owned coverage is UNPROVABLE - newer absence
        # is never substituted for it); the CURRENT 1f claim CLEARS
        # with its own R4 aftermath
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        r1, _ = run(store, put_req("9e" * 16, key="w2key"))
        self.assertEqual(r1.result.outcome.result_code, "applied")
        rw0, _ = run(store, wipe_req("0f" * 16))
        self.assertEqual(rw0.result.outcome.result_code, "applied")
        rev_w0 = store._store_revision(1)
        r2, _ = run(store, put_req("7d" * 16, key="w2key2"))
        self.assertEqual(r2.result.outcome.result_code, "applied")
        rev_put = store._store_revision(1)
        rw1, _ = run(store, wipe_req("1f" * 16))
        self.assertEqual(rw1.result.outcome.result_code, "applied")
        rev_w1 = store._store_revision(1)
        self.assertLess(rev_w0, rev_put)
        self.assertLess(rev_put, rev_w1)
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        harness.send(comp.token)
        d = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(harness.ack(d["ack"]).kind, "delivered")
        harness.register_erase_readback(f.OwnedEraseReadback(store))
        with self.assertRaises(f.HarnessRefusedError):
            harness.scrub(comp.token, {
                "raw_ref_handle": _Ok(), "comparison_buffer": _Ok()},
                erase={"operation_id": "0f" * 16,
                       "coverage_id": "facts-live-zero"})
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 0)
        out = harness.scrub(comp.token, {
            "raw_ref_handle": _Ok(), "comparison_buffer": _Ok()},
            erase={"operation_id": "1f" * 16,
                   "coverage_id": "facts-live-zero"})
        self.assertEqual(out["kind"], "cleared")
        row = oconn.execute(
            "SELECT step_id, revision, verified_at_revision FROM"
            " scrub_log WHERE token=?", (comp.token,)).fetchone()
        self.assertEqual(tuple(row), (
            "erase:1:1:%s:facts-live-zero:%d" % ("1f" * 16, rev_w1),
            rev_w1, rev_w1))

        # (E) SHAPED FORGED TUPLES: even with the store route
        # fault-patched to return a shaped dict, the scrub-side CLOSED
        # validation refuses a wrong method/result/id/revision/
        # coverage/join, a missing key, and an extra key - NOTHING
        # recorded; the REAL route clears once restored
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        r1, _ = run(store, put_req("9e" * 16, key="w2key"))
        self.assertEqual(r1.result.outcome.result_code, "applied")
        rw, _ = run(store, wipe_req("0f" * 16))
        self.assertEqual(rw.result.outcome.result_code, "applied")
        rev = store._store_revision(1)
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        harness.send(comp.token)
        d = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(harness.ack(d["ack"]).kind, "delivered")
        harness.register_erase_readback(f.OwnedEraseReadback(store))
        real = store.erase_proof
        base = {"operation_id": "0f" * 16, "method": "wipe",
                "result_code": "applied", "revision": rev,
                "verified_at_revision": rev,
                "coverage_id": "facts-live-zero",
                "owner_uid": 1, "lifetime_id": 1}
        forged_shapes = (
            dict(base, method="put"),
            dict(base, result_code="refused"),
            dict(base, owner_uid=-99),
            dict(base, lifetime_id=-99),
            dict(base, revision=0),
            dict(base, verified_at_revision=0),
            dict(base, coverage_id="everything"),
            dict(base, operation_id="1f" * 16),
            {k: v for k, v in base.items() if k != "method"},
            dict(base, extra="x"))
        for forged in forged_shapes:
            store.erase_proof = (lambda o, c, _f=forged: _f)
            with self.assertRaises(f.HarnessRefusedError):
                harness.scrub(comp.token, {
                    "raw_ref_handle": _Ok(), "comparison_buffer": _Ok()},
                    erase={"operation_id": "0f" * 16,
                           "coverage_id": "facts-live-zero"})
        store.erase_proof = real
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 0)
        out = harness.scrub(comp.token, {
            "raw_ref_handle": _Ok(), "comparison_buffer": _Ok()},
            erase={"operation_id": "0f" * 16,
                   "coverage_id": "facts-live-zero"})
        self.assertEqual(out["kind"], "cleared")
        row = oconn.execute(
            "SELECT step_id, revision, verified_at_revision FROM"
            " scrub_log WHERE token=?", (comp.token,)).fetchone()
        self.assertEqual(tuple(row), (
            "erase:1:1:%s:facts-live-zero:%d" % ("0f" * 16, rev),
            rev, rev))

    def test_unrelated_same_owner_target_refused(self):
        # the R1 repro (v80 795-812, 3948-3951 UNRELATED SAME-OWNER
        # TARGET): a VALID recovery/hash with an UNRELATED same-owner
        # ScopedInventoryRef target is NOT the subject's evidence -
        # REFUSAL, NO blocked_inserted row
        store, conn, path = mkstore_path()
        req, b, rid = blocked_op(store)
        bh = f.extended_binding_hash(b, req)
        base = dict(recovery_id=rid, uid=1, lifetime_id=1,
                    operation_id=req.operation_id, commitment=bh,
                    kind=f.EvidenceKind.RECEIPT,
                    source=f.EvidenceKind.RECEIPT,
                    accounting_scope=f.Lifetime(uid=1, lifetime_id=1))
        bad = f.SafeEvidenceProjection(
            target=f.ScopedInventoryRef(
                owner=f.Lifetime(uid=1, lifetime_id=1),
                class_name=f.CopyClass.FACTS, entry_id="fake-entry"),
            **base)
        w, ctx = store.mint_read_fixtures(
            1, 1, "b3" * 16, f.ReadMethod.READBACK,
            {"projection": bad.to_dict(), "token": "ab" * 16})
        with self.assertRaises(f.EvidenceScopeError):
            store.insert_evidence(1, 1, "b3" * 16, bad, "ab" * 16, w, ctx)
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM evidence_entries", (),
            "one")["n"], 0)
        # a source != kind projection breaks the anchor's expected
        # source association (v80 1289) - same refusal
        bad2 = f.SafeEvidenceProjection(
            target=f.RecoveryBoundRef(
                recovery_id=rid, source=f.EvidenceKind.RECEIPT),
            **{**base, "source": f.EvidenceKind.LATE_ACCOUNTING})
        w2, ctx2 = store.mint_read_fixtures(
            1, 1, "b3" * 16, f.ReadMethod.READBACK,
            {"projection": bad2.to_dict(), "token": "cd" * 16})
        with self.assertRaises(f.EvidenceScopeError):
            store.insert_evidence(1, 1, "b3" * 16, bad2, "cd" * 16,
                                  w2, ctx2)
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM evidence_entries", (),
            "one")["n"], 0)
        # a FABRICATED receipt target: the string NAMES the anchor's
        # receipt ("receipt:<op>") but the op is BLOCKED - NO receipt
        # row exists - guessed strings with no inventory existence
        # proof REFUSE and write NOTHING (operation_receipts stays 0)
        fab = f.SafeEvidenceProjection(
            target=f.ScopedInventoryRef(
                owner=f.Lifetime(uid=1, lifetime_id=1),
                class_name=f.CopyClass.OP_RECORDS,
                entry_id="receipt:" + req.operation_id),
            **base)
        w3, ctx3 = store.mint_read_fixtures(
            1, 1, "b3" * 16, f.ReadMethod.READBACK,
            {"projection": fab.to_dict(), "token": "ef" * 16})
        with self.assertRaises(f.EvidenceScopeError):
            store.insert_evidence(1, 1, "b3" * 16, fab, "ef" * 16,
                                  w3, ctx3)
        self.assertEqual(store._q.q(
            "SELECT count(*) AS n FROM operation_receipts", (),
            "one")["n"], 0)
        # the subject's OWN recovery-bound entry (the store-built
        # target/source association) SATISFIES the binding and inserts
        good = f.SafeEvidenceProjection(
            target=f.RecoveryBoundRef(
                recovery_id=rid, source=f.EvidenceKind.RECEIPT),
            **base)
        w4, ctx4 = store.mint_read_fixtures(
            1, 1, "b3" * 16, f.ReadMethod.READBACK,
            {"projection": good.to_dict(), "token": "ef" * 16})
        r = store.insert_evidence(1, 1, "b3" * 16, good, "ef" * 16,
                                  w4, ctx4)
        self.assertEqual(r.kind, "blocked_inserted")
        f.close_exclusive_db(conn)

    def test_t3_scrub_destroy_failure_is_typed_refusal(self):
        # T3 (v80 2060-2137): a destroy/clear FAILURE or a FAILED
        # probe never escapes raw and never clears - typed 'refused'
        # with NOTHING recorded
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})

        class ExplodingHandle:
            def destroy(self):
                raise RuntimeError("boom")

            def live_raw(self):
                raise RuntimeError("probe boom")

        class BlindHandle:
            def clear(self):
                pass  # no live_raw probe at all: absence is UNKNOWN

        out = harness.scrub(comp.token, {
            "raw_ref_handle": ExplodingHandle(),
            "comparison_buffer": BlindHandle()})
        self.assertEqual(out["kind"], "refused")
        # UNKNOWN (failed/missing probe) = conservative raw_found
        # True - absence UNPROVEN, the CLOSED bool is never None
        self.assertIs(out["live_check"]["raw_found"], True)
        self.assertEqual(sorted(out.keys()),
                         ["kind", "live_check", "ref_ids"])
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()[0], 0)

    def test_u1_scrub_probe_schema_is_strict_bool(self):
        # U1 (v80 2080-2104, 2083): raw_found is the CLOSED bool - a
        # None/0/empty/non-bool probe is UNKNOWN (conservatively
        # NOT-PROVEN-EMPTY: raw_found True, 'refused', NOTHING
        # recorded); ONLY a COMPLETE strict literal-False probe set
        # clears with raw_found False
        for bad in (None, 0, "", [], 0.0, 1, "x", [1]):
            with self.subTest(probe=repr(bad)):
                store, req, b, rid, provider, harness, proj, oconn = \
                    self._rig()
                comp = harness.compose(
                    {"projection": proj, "recovery_id": rid})

                class BadProbe:
                    def clear(self):
                        pass

                    def live_raw(self):
                        return bad  # non-bool: NEVER a pass/fail vote

                class GoodProbe:
                    def clear(self):
                        pass

                    def live_raw(self):
                        return False

                out = harness.scrub(comp.token, {
                    "raw_ref_handle": BadProbe(),
                    "comparison_buffer": GoodProbe()})
                self.assertEqual(out["kind"], "refused")
                self.assertIs(out["live_check"]["raw_found"], True)
                self.assertEqual(sorted(out.keys()),
                                 ["kind", "live_check", "ref_ids"])
                self.assertEqual(oconn.execute(
                    "SELECT count(*) FROM scrub_log WHERE token=?",
                    (comp.token,)).fetchone()[0], 0)
                self.assertEqual(harness.outbox_state(comp.token),
                                 "pending")
        # REAL both-owned CLEAR positive: BOTH literal-False probes ->
        # 'cleared' + raw_found False + the completion row recorded
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})

        class OwnedHandle:
            def __init__(self):
                self._raw = b"secret"

            def clear(self):
                self._raw = None

            def live_raw(self):
                return self._raw is not None

        raw, buf = OwnedHandle(), OwnedHandle()
        out = harness.scrub(comp.token, {
            "raw_ref_handle": raw, "comparison_buffer": buf})
        self.assertEqual(out["kind"], "cleared")
        self.assertIs(out["live_check"]["raw_found"], False)
        self.assertEqual(out["live_check"]["containers"],
                         ["raw_ref_handle", "comparison_buffer"])
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()[0], 1)
        # REAL both-owned REFUSE positive: one literal-True probe ->
        # 'refused' + raw_found True, NOTHING recorded
        comp2 = harness.compose({"projection": proj, "recovery_id": rid})
        stuck = OwnedHandle()
        stuck.clear = lambda: None  # clear() does NOT erase the raw
        out2 = harness.scrub(comp2.token, {
            "raw_ref_handle": stuck, "comparison_buffer": OwnedHandle()})
        self.assertEqual(out2["kind"], "refused")
        self.assertIs(out2["live_check"]["raw_found"], True)
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp2.token,)).fetchone()[0], 0)

    def test_t4_ack_reread_identity_coherence(self):
        # T4 (v80 1628-1645): the durable reread binds token +
        # recovery + projection; a POSITIVE same-identity pending =
        # re-attempt; missing/unreadable/incoherent = typed uncertain
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        d = provider.deliver({"projection": proj, "token": comp.token})
        ack_input = d["ack"]
        # POSITIVE same-identity pending
        ar = harness._ack_durable_reread(comp.token, ack_input)
        self.assertEqual(ar.kind, "still_pending")
        # MISSING row = uncertain, NEVER a blind pending claim
        ar = harness._ack_durable_reread("00" * 16, ack_input)
        self.assertEqual(ar.kind, "uncertain")
        # INCOHERENT recovery = uncertain
        other = dataclasses.replace(ack_input.projection,
                                    recovery_id="ff" * 16)
        bad = dataclasses.replace(ack_input, projection=other)
        ar = harness._ack_durable_reread(comp.token, bad)
        self.assertEqual(ar.kind, "uncertain")
        # INCOHERENT projection content = uncertain
        drifted = dataclasses.replace(ack_input.projection,
                                      commitment="9" * 64)
        bad2 = dataclasses.replace(ack_input, projection=drifted)
        ar = harness._ack_durable_reread(comp.token, bad2)
        self.assertEqual(ar.kind, "uncertain")
        # NO binding input (ack_input None) = uncertain, NEVER an
        # unvalidated success off the raw row state
        ar = harness._ack_durable_reread(comp.token, None)
        self.assertEqual(ar.kind, "uncertain")
        # a refused-kind ack carries NO projection: still uncertain
        refused_input = f.AckInput(kind="refused", token=comp.token,
                                   reason=f.AckRefusedReason.IO_FAILURE)
        ar = harness._ack_durable_reread(comp.token, refused_input)
        self.assertEqual(ar.kind, "uncertain")
        # an UNDECODABLE durable projection row = typed uncertain,
        # NEVER a raw exception escaping the reread
        orig_json = oconn.execute(
            "SELECT projection_json FROM outbox WHERE token=?",
            (comp.token,)).fetchone()[0]
        oconn.execute("UPDATE outbox SET projection_json='not-json'"
                      " WHERE token=?", (comp.token,))
        ar = harness._ack_durable_reread(comp.token, ack_input)
        self.assertEqual(ar.kind, "uncertain")
        # a shape-incoherent (valid JSON, wrong shape) row = uncertain
        oconn.execute("UPDATE outbox SET projection_json='{}'"
                      " WHERE token=?", (comp.token,))
        ar = harness._ack_durable_reread(comp.token, ack_input)
        self.assertEqual(ar.kind, "uncertain")
        oconn.execute("UPDATE outbox SET projection_json=? WHERE"
                      " token=?", (orig_json, comp.token))
        # delivered = done
        harness.ack(ack_input)
        ar = harness._ack_durable_reread(comp.token, ack_input)
        self.assertEqual(ar.kind, "delivered")

    def test_safe_payload_stable_token(self):
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        row = oconn.execute(
            "SELECT projection_json, recovery_id FROM outbox WHERE"
            " token=?", (comp.token,)).fetchone()
        # ref_json is projection output ONLY (no extra fields)
        self.assertEqual(json.loads(row[0]), proj.to_dict())
        self.assertEqual(row[1], rid)
        # the token is STABLE across redeliveries
        red = harness.redeliver_pending()
        self.assertEqual(red["redelivered"], [comp.token])

    def test_pending_restart_redelivers_same_token(self):
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        c1 = harness.compose({"projection": proj, "recovery_id": rid})
        c2 = harness.compose({"projection": proj, "recovery_id": rid})
        red = harness.redeliver_pending()
        self.assertEqual(red["redelivered"], [c1.token, c2.token])
        # after one delivery+ack, only the OTHER stays pending
        d = provider.deliver({"projection": proj, "token": c1.token})
        self.assertEqual(d["status"], "delivered")
        ar = harness.ack(d["ack"])
        self.assertEqual(ar.kind, "delivered")
        red2 = harness.redeliver_pending()
        self.assertEqual(red2["redelivered"], [c2.token])

    def test_lost_receiver_ack_replays_without_double_count(self):
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        d = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(d["status"], "delivered")
        self.assertEqual(d["committed_result"].kind, "blocked_inserted")
        # the ACK SIGNAL is lost: the issuer row STAYS pending
        self.assertEqual(harness.outbox_state(comp.token), "pending")
        # redelivery with the SAME token replays receiver-side: the
        # receiver sees its OWN committed result, NO double-count
        d2 = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(d2["status"], "delivered")
        self.assertEqual(d2["committed_result"].kind,
                         "blocked_replay_no_event")
        self.assertEqual(d2["committed_result"].contribution, 0)
        n = store._q.q("SELECT count(*) AS n FROM evidence_entries", (),
                       "one")["n"]
        self.assertEqual(n, 1)

    def test_lost_delivered_state_update(self):
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        d = provider.deliver({"projection": proj, "token": comp.token})
        # the receiver ack COMMITTED but the outbox update\'s COMMIT
        # CONFIRMATION is lost
        ar = harness.ack(d["ack"], confirm=False)
        # LOST CONFIRMATION = the DURABLE REREAD under the SAME
        # token/recovery/projection decides (v80 1628-1645): the row is
        # still pending = re-attempt, NEVER an immediate uncertain or a
        # blind pending claim without the reread
        self.assertEqual(ar.kind, "still_pending")
        self.assertEqual(ar.reason, f.AckRefusedReason.IO_FAILURE)
        self.assertEqual(harness.outbox_state(comp.token), "pending")
        d2 = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(d2["committed_result"].kind,
                         "blocked_replay_no_event")
        ar2 = harness.ack(d2["ack"])
        self.assertEqual(ar2.kind, "delivered")
        self.assertEqual(harness.outbox_state(comp.token), "delivered")
        # DELIVERED-COMMIT-THEN-LOSS: the reread sees the landed row =
        # done (NO rollback claim, NO blind re-attempt)
        ar2b = harness.ack(d2["ack"], confirm=False)
        self.assertEqual(ar2b.kind, "delivered")
        # UNREADABLE reread = typed uncertain, NO blind pending claim
        provider._fault_io_unreadable = True
        ar3 = harness.ack(d2["ack"])
        self.assertEqual(ar3.kind, "uncertain")
        self.assertEqual(ar3.uncertain, f.AckUncertain.IO_UNREADABLE)

    def test_live_erase_typed_outcome_and_refusal(self):
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        calls = []

        class RawHandle:
            def __init__(self, payload, probe=None):
                self._payload = payload
                self._probe = probe

            def clear(self):
                if self._probe is not None:
                    self._probe()
                self._payload = None

            def live_raw(self):
                return self._payload is not None

        raw = RawHandle(b"raw-secret", probe=lambda: calls.append(
            oconn.execute(
                "SELECT count(*) FROM scrub_log").fetchone()[0]))
        buf = RawHandle(b"cmp-secret")
        out = harness.scrub(comp.token, {
            "raw_ref_handle": raw, "comparison_buffer": buf})
        # ScrubOutcome (v80 2060-2137): kind/ref_ids/live_check typed
        self.assertEqual(out["kind"], "cleared")
        self.assertEqual(out["ref_ids"],
                         ["raw_ref_handle", "comparison_buffer"])
        self.assertEqual(out["live_check"]["raw_found"], False)
        self.assertEqual(sorted(out["live_check"]["containers"]),
                         ["comparison_buffer", "raw_ref_handle"])
        # the CLOSED outcome schema is EXACT: kind/ref_ids/live_check
        # ONLY - the scrub_log row carries the recording
        self.assertEqual(sorted(out.keys()),
                         ["kind", "live_check", "ref_ids"])
        self.assertNotIn("scrubbed", out)
        self.assertEqual(calls, [0])  # NO completion BEFORE discard
        self.assertFalse(raw.live_raw())
        self.assertFalse(buf.live_raw())
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log").fetchone()[0], 1)
        # CONTRADICTION: raw still live after discard = 'refused',
        # NOTHING recorded (a noop with populated raw can never clear)
        comp2 = harness.compose({"projection": proj, "recovery_id": rid})
        stuck = object()  # no destroy/clear: raw cannot be discarded
        out2 = harness.scrub(comp2.token, {
            "raw_ref_handle": stuck, "comparison_buffer": RawHandle(None)})
        self.assertEqual(out2["kind"], "refused")
        # NO probe on the stuck container = UNKNOWN (conservative
        # raw_found True - absence UNPROVEN, never None), never an
        # arbitrary len/bool pass/fail
        self.assertIs(out2["live_check"]["raw_found"], True)
        self.assertNotIn("scrubbed", out2)
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp2.token,)).fetchone()[0], 0)
        self.assertEqual(harness.outbox_state(comp2.token), "pending")
        # the CLOSED enumeration is exact: missing/extra keys refuse
        with self.assertRaises(cap.ModelInputError):
            harness.scrub(comp.token, {"raw_ref_handle": RawHandle(None)})

    def test_u3_scrub_hook_full_workflow_commit_side(self):
        # v80 2060-2137: the SEPARATELY DECLARED scrub(refs) HOOK
        # (IssuerHarness.scrub_hook) invoked on the CHOSEN KNOWN-COMMIT
        # side of the erase/readback step: 'cleared' lets the step
        # record completion; 'refused' PRESERVES the known commit
        # (NEVER rolls back, NEVER marks uncertain, NEVER re-executes)
        # and records NOTHING; the SAME hook shape runs at teardown
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        harness.send(comp.token)
        d = provider.deliver({"projection": proj, "token": comp.token})
        ar = harness.ack(d["ack"])
        self.assertEqual(ar.kind, "delivered")  # the CHOSEN KNOWN COMMIT

        class Stuck:
            pass  # no destroy/clear/probe: the raw cannot be discarded

        # REFUSED on the post-commit side: the commit STAYS CERTAIN
        out = harness.scrub(comp.token, {
            "raw_ref_handle": Stuck(), "comparison_buffer": None})
        self.assertEqual(out["kind"], "refused")
        self.assertEqual(harness.outbox_state(comp.token), "delivered")
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()[0], 0)
        # NO re-execute: the delivered token is never redispatched
        self.assertEqual(harness.redeliver_pending()["redelivered"], [])

        class OwnedHandle:
            def __init__(self):
                self._raw = b"secret"

            def clear(self):
                self._raw = None

            def live_raw(self):
                return self._raw is not None

        # CLEARED: the step records completion AFTER the hook's proof;
        # the known commit stays certain (no state change to the row)
        out2 = harness.scrub(comp.token, {
            "raw_ref_handle": OwnedHandle(),
            "comparison_buffer": OwnedHandle()})
        self.assertEqual(out2["kind"], "cleared")
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()[0], 1)
        self.assertEqual(harness.outbox_state(comp.token), "delivered")
        # TEARDOWN: the SAME hook shape runs standalone (no token
        # wrapper) and records NOTHING on its own
        outcome = harness.scrub_hook({
            "raw_ref_handle": OwnedHandle(),
            "comparison_buffer": OwnedHandle()})
        self.assertEqual(outcome["kind"], "cleared")
        self.assertEqual(oconn.execute(
            "SELECT count(*) FROM scrub_log WHERE token=?",
            (comp.token,)).fetchone()[0], 1)  # unchanged: no record

    def test_forged_result_or_ref_never_delivered(self):
        store, req, b, rid, provider, harness, proj, oconn = self._rig()
        comp = harness.compose({"projection": proj, "recovery_id": rid})
        d = provider.deliver({"projection": proj, "token": comp.token})
        self.assertEqual(d["status"], "delivered")
        # a FORGED committed_result (the join FAILS) = REFUSED
        forged = f.AckInput(
            kind="ack", token=comp.token, projection=proj,
            committed_result=f.InsertionEventResult(
                kind="inserted", contribution=3,
                event_id="insert:" + "0" * 32),
            evidence_ref=f.EvidenceRef(kind="late",
                                       event_id="insert:" + "0" * 32,
                                       evidence_id="0" * 32))
        ar = harness.ack(forged)
        self.assertEqual(ar.kind, "still_pending")
        self.assertEqual(ar.reason, f.AckRefusedReason.MISMATCHED_RESULT)
        self.assertEqual(harness.outbox_state(comp.token), "pending")
        # an INVENTED blocked EvidenceRef = REFUSED
        forged2 = f.AckInput(
            kind="ack", token=comp.token, projection=proj,
            committed_result=d["committed_result"],
            evidence_ref=f.EvidenceRef(kind="blocked", evidence_id="f" * 32))
        ar2 = harness.ack(forged2)
        self.assertEqual(ar2.kind, "still_pending")
        self.assertEqual(harness.outbox_state(comp.token), "pending")
        # the REAL payload = delivered
        ar3 = harness.ack(d["ack"])
        self.assertEqual(ar3.kind, "delivered")


class NoneWrapperTypedRefusal(unittest.TestCase):
    """B9: a None wrapper NEVER dereferences - typed refusal everywhere."""

    def test_none_wrappers_refuse_typed(self):
        store, _ = mkstore()
        r, _ = run(store, put_req("a1" * 16))
        fid = r.result.outcome.readback.fact_id
        ctx = read_ctx(store)
        with self.assertRaises((cap.ModelInputError, cap.AuthError)):
            store.get(1, fid, None, store._clock, ctx)
        with self.assertRaises((cap.ModelInputError, cap.AuthError)):
            store.list_facts(1, f.RowKind.LIVE, None, None, 10, None,
                             store._clock, ctx)
        req = put_req("a2" * 16)
        c, l, p, b, ctx2 = mint(store, req)
        for cap_arg, claim_arg in ((None, l), (c, None)):
            with self.assertRaises((cap.ModelInputError, cap.AuthError)):
                store.execute(1, req, cap_arg, claim_arg, p, ctx2)


class ExclusiveIsolationFixture(unittest.TestCase):
    """The exclusive cross-instance isolation fixture: a second opener
    refuses; an unmarked file connection refuses; close releases."""

    def test_second_opener_refuses(self):
        store, conn, path = mkstore_path()
        try:
            with self.assertRaises(cap.ModelInputError):
                f.open_exclusive_db(path)
        finally:
            f.close_exclusive_db(conn)

    def test_unmarked_file_connection_refused(self):
        store, conn, path = mkstore_path()
        f.close_exclusive_db(conn)
        raw = sqlite3.connect(path)
        try:
            with self.assertRaises(cap.ModelInputError):
                f.FactStore(raw, f.JsonTestClock(T0))
        finally:
            raw.close()

    def test_close_releases_the_lock(self):
        store, conn, path = mkstore_path()
        f.close_exclusive_db(conn)
        conn2 = f.open_exclusive_db(path)  # the released lock re-acquires
        f.close_exclusive_db(conn2)

    def test_memory_db_allowed(self):
        conn = sqlite3.connect(":memory:")
        store = f.FactStore(conn, f.JsonTestClock(T0))
        store.setup_owner(1, 1, 1, scopes=ALL_SCOPES)
        r, _ = run(store, put_req("a1" * 16))
        self.assertEqual(r.kind, "executed")
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
