#!/usr/bin/env python3
"""ABBA steady-state CONFIRMATION combiner -> drift-cancelled step estimate.

The within-connection f100 STEP analyzer (analyze_q3.q3_analyze) stays the PRIMARY
experiment. This combiner is the CONFIRMATION arm: it takes FOUR steady-state cells
collected in the frozen f150/f100/f100/f150 (A B B A) order and computes the
drift-cancelling estimate

    estimate = ((A1 - B1) + (A2 - B2)) / 2

over the four single-plateau gap_proxy medians. With equal cell spacing this cancels
any LINEAR environmental drift over the campaign (A1=a, B1=b+d, B2=b+2d, A2=a+3d ->
estimate = a-b), so the surviving quantity is the 150->100us step, which must land on
the frozen 800t within the SAME immutable +/-4 primary tolerance.

Gates (ALL must pass; a single failure makes the WHOLE campaign INCOMPLETE -- there is
NO cell substitution and NO selective rerun; a failed block is repeated fresh):

  - EXACTLY FOUR cells, presented in collection order, arms == Q3_ABBA_ARM_SEQ
    (f150, f100, f100, f150).
  - CAMPAIGN BINDING: every cell manifest carries the SAME abba_campaign_id, and the
    per-cell abba_seq is exactly 0,1,2,3 in presentation order (no reorder, no cell
    borrowed from another campaign).
  - PROVENANCE per cell: an archived manifest.json with observer_image_verified==true,
    tree_dirty==false (an accepted, clean-tree cell -- a quarantined/dirty smoke can
    never enter an ABBA block), q3_arm present, and stored log hashes still matching
    the on-disk obs/central/periph logs.
  - RE-ANALYSIS is the source of truth: each cell is re-run through
    analyze_q3.q3_steady_analyze(arm=cell.q3_arm) under the cell's OWN validated calib
    (whose sha must match the manifest) and must reach STEADY-METRICS-OK. A stale
    analysis.txt is never trusted.
  - HOMOGENEITY: matched observer image + peripheral firmware + geometry (PHY/map/ch)
    + calibration artifact + analyzer/runner/protocol/assert lineage + frozen
    q3_contract (no deviation), across all four cells. The central firmware differs
    ONLY by the registered arm: the two f150 cells share one central image, the two
    f100 cells share another, and those two images differ -- nothing else varies.
  - DISTINCT reset-isolated connections: four distinct connection AAs.

Usage: combine_abba.py --out abba-confirmation.json <A1_f150> <B1_f100> <B2_f100> <A2_f150>
Exit 0 ABBA-CONFIRMED, 3 INCOMPLETE (any gate fails), 2 usage.
"""
import sys, os, json, hashlib
import statistics as st
import analyze_q2 as A
import analyze_q3 as Q3

ARM_SEQ = Q3.Q3_ABBA_ARM_SEQ          # ('f150','f100','f100','f150'), frozen
EXPECT_STEP = Q3.Q3_ABBA_EXPECT_STEP  # 800 t
TOL = Q3.Q3_ABBA_TOL_TICKS            # +/-4 t (same immutable primary gate)
N_CELLS = len(ARM_SEQ)

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(65536), b''):
            h.update(b)
    return h.hexdigest()

def matched_invariants(m):
    """the fields that MUST agree across all four ABBA cells (matched replications).
    Connection AA/CRCInit change per cell (distinct connections) and the CENTRAL build
    changes with the arm -- both are handled separately."""
    c = m.get('connection', {}); s = m.get('sha256', {}); dv = m.get('devids', {})
    return {
        'board': m.get('board'),
        'observer.hex': s.get('observer.hex'),
        'observer.elf': s.get('observer.elf'),
        'observer.config': s.get('observer.config'),
        'obs_dev': dv.get('obs'), 'central_dev': dv.get('central'), 'periph_dev': dv.get('periph'),
        'phy': c.get('phy'), 'map': c.get('map'), 'ch': c.get('ch'),
        'periph_build': json.dumps(m.get('periph_build'), sort_keys=True),
        'calibration_artifact': m.get('calibration_artifact'),
        'analyze_q2': s.get('analyze_q2.py'),
        'analyze_q3': s.get('analyze_q3.py'),
        'q2_run': s.get('q2_run.py'),
        'assert_fsu_config': s.get('assert_fsu_config.py'),
        'Q2_protocol': s.get('Q2-ACCEPTANCE-PROTOCOL.md'),
        'Q3_protocol': s.get('Q3-ACCEPTANCE-PROTOCOL.md'),
        'q3_contract': json.dumps(m.get('q3_contract'), sort_keys=True),
        'q3_contract_deviation': m.get('q3_contract_deviation') or '',
        # rev-6: the frozen final connection parameters (post auto param-update) must be
        # IDENTICAL across all four cells -- the same negotiated interval/latency/timeout.
        'q3_final_params': json.dumps(m.get('q3_final_params'), sort_keys=True),
    }

def check_digests(m):
    """endpoint + observer images must be real 64-hex for HEX, ELF, and .config on all
    three devices -- never MISSING/UNVERIFIED. The ELF pins the executable, .config the
    build; HEX alone does not."""
    for role in ('central_build', 'periph_build'):
        bd = m.get(role, {})
        for k in ('zephyr.hex', 'zephyr.elf', '.config'):
            if not A.valid_digest(bd.get(k)):
                return False, f'{role}.{k} is not a valid 64-hex digest ({bd.get(k)!r})'
    s = m.get('sha256', {})
    for k in ('observer.hex', 'observer.elf', 'observer.config'):
        if not A.valid_digest(s.get(k)):
            return False, f'{k} is not a valid 64-hex digest (unverified observer image)'
    return True, 'ok'

# manifest sha256 key -> current-tree path. The four ABBA cells must have been produced
# by the SAME frozen tooling/protocol this combiner runs under: rehash each file NOW and
# require every cell's recorded digest to equal it (binds acceptance to current lineage,
# not merely to each other -- a block sharing one stale/fabricated hash must reject).
def _current_lineage():
    return {
        'analyze_q2.py': os.path.join(HERE, 'analyze_q2.py'),
        'analyze_q3.py': ANALYZE_Q3,
        'q2_run.py': os.path.join(HERE, 'q2_run.py'),
        'combine_abba.py': os.path.join(HERE, 'combine_abba.py'),
        'combine_calib.py': os.path.join(HERE, 'combine_calib.py'),
        'assert_fsu_config.py': ASSERT_FSU,
        'Q2-ACCEPTANCE-PROTOCOL.md': os.path.join(ROOT, 'debug-evidence', 'observer-q1-20260810', 'Q2-ACCEPTANCE-PROTOCOL.md'),
        'Q3-ACCEPTANCE-PROTOCOL.md': Q3PROTO,
    }

def check_lineage_current(mans):
    """bind the block to the CURRENT frozen tooling/protocol/contract (not just to each
    other). Rehash every lineage file now; require each cell's recorded digest to match,
    and each cell's q3_contract to equal analyze_q3.q3_contract() exactly."""
    cur = {}
    for key, path in _current_lineage().items():
        if not os.path.exists(path):
            return False, f'current lineage file missing: {key} ({path})'
        cur[key] = sha256(path)
    want_contract = json.dumps(Q3.q3_contract(), sort_keys=True)
    for i, m in enumerate(mans):
        s = m.get('sha256', {})
        for key, h in cur.items():
            if s.get(key) != h:
                return False, (f'cell #{i} {key} digest {s.get(key)!r} != current-tree {h[:12]}... '
                               '(stale/foreign tooling or protocol)')
        if json.dumps(m.get('q3_contract'), sort_keys=True) != want_contract:
            return False, f'cell #{i} q3_contract != current analyze_q3.q3_contract() (contract drift)'
    return True, 'ok'

def check_homogeneity(mans):
    """matched replications + central-differs-ONLY-by-arm + distinct connections."""
    for m in mans:
        dok, dreason = check_digests(m)
        if not dok:
            return False, dreason
    ref = matched_invariants(mans[0])
    for i, m in enumerate(mans[1:], 1):
        cur = matched_invariants(m)
        diff = [k for k in ref if ref[k] != cur[k]]
        if diff:
            return False, f'cell #{i} differs from #0 in {diff} (not a matched replication)'
    # central firmware: identical WITHIN an arm, DIFFERENT between arms.
    by_arm = {}
    for m in mans:
        by_arm.setdefault(m.get('q3_arm'), []).append(json.dumps(m.get('central_build'), sort_keys=True))
    for arm, builds in by_arm.items():
        if len(set(builds)) != 1:
            return False, f'{arm} cells used DIFFERENT central images (arm is not the only variable)'
    reduced = next(a for a in ARM_SEQ if a != 'f150')   # 'f100' at 1M, 'f52' at 2M
    f150_c, red_c = by_arm['f150'][0], by_arm[reduced][0]
    if f150_c == red_c:
        return False, f'f150 and {reduced} central images are identical (registered arm did not change the image)'
    # distinct reset-isolated connections
    aas = [m.get('connection', {}).get('AA') for m in mans]
    if len(set(aas)) != N_CELLS:
        return False, f'connections are not distinct (AAs {aas}) -- not reset-isolated'
    return True, 'ok'

def check_campaign(mans):
    """no cell substitution / no reorder: one shared campaign id, seqs 0..N-1 in
    presentation order, arms == the frozen A B B A sequence."""
    cids = [m.get('abba_campaign_id') for m in mans]
    if None in cids:
        return False, 'a cell is missing abba_campaign_id (not produced by the ABBA runner)'
    if len(set(cids)) != 1:
        return False, f'cells span MULTIPLE campaigns {sorted(set(cids))} (no cross-campaign substitution)'
    seqs = [m.get('abba_seq') for m in mans]
    if seqs != list(range(N_CELLS)):
        return False, f'abba_seq {seqs} != {list(range(N_CELLS))} (reordered or a cell missing)'
    arms = [m.get('q3_arm') for m in mans]
    if tuple(arms) != tuple(ARM_SEQ):
        return False, f'arm order {arms} != frozen {list(ARM_SEQ)}'
    # CHRONOLOGY: absolute monotonic intervals must be strictly ordered + non-overlapping
    # in presentation order -- this is what PROVES A/B/B/A collection (labels alone cannot).
    ivals = [(m.get('abba_start_ns'), m.get('abba_end_ns')) for m in mans]
    for i, (s, e) in enumerate(ivals):
        if not (isinstance(s, int) and isinstance(e, int)) or not (s < e):
            return False, f'cell #{i} missing/invalid abba_start_ns/abba_end_ns interval ({s}, {e})'
    for i in range(N_CELLS - 1):
        if not (ivals[i][1] < ivals[i + 1][0]):
            return False, (f'cells #{i} and #{i+1} overlap or are out of order '
                           f'(end {ivals[i][1]} !< start {ivals[i+1][0]}) -- collection not strictly A/B/B/A')
    return True, 'ok'

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..', '..'))   # repo root (this file is apps/misc/q2-central/)
Q3PROTO = os.path.join(ROOT, 'debug-evidence', 'observer-q3-20260811', 'Q3-ACCEPTANCE-PROTOCOL.md')
# the analyzer whose sha the cells recorded under key 'analyze_q3.py'. combine_abba_2m
# overrides this to analyze_q3_2m.py (the 2M runner records that file under the 1M key).
ANALYZE_Q3 = os.path.join(HERE, 'analyze_q3.py')
# assert_fsu_config lives at the repo root; combine_abba_2m overrides this to the 2M asserter.
ASSERT_FSU = os.path.join(ROOT, 'zephyr-patches', 'fsu-m0-series', 'assert_fsu_config.py')

# archived-firmware name -> where its digest lives in the manifest. Binds the BINARY
# lineage: the combiner RE-RESOLVES firmware/<name> and rehashes it, not just the digit.
_FW_DIGEST = [
    ('observer.hex',    ('sha256', 'observer.hex')),
    ('observer.elf',    ('sha256', 'observer.elf')),
    ('observer.config', ('sha256', 'observer.config')),
    ('central.hex',     ('central_build', 'zephyr.hex')),
    ('central.elf',     ('central_build', 'zephyr.elf')),
    ('central.config',  ('central_build', '.config')),
    ('periph.hex',      ('periph_build', 'zephyr.hex')),
    ('periph.elf',      ('periph_build', 'zephyr.elf')),
    ('periph.config',   ('periph_build', '.config')),
]

def check_firmware_archive(d, m):
    """re-resolve + REHASH the nine archived firmware artifacts (firmware/<dev>.<ext>)
    and require each to equal its manifest digest -- this catches a fabricated hash, a
    missing build file, or a post-cell binary edit (schema-valid digits alone cannot).
    Then RERUN assert_fsu_config over the archived central (@arm) + periph .config so the
    accepted images are semantically the registered firmware. Returns (ok, reason)."""
    import subprocess
    fwd = os.path.join(d, 'firmware')
    for name, (top, key) in _FW_DIGEST:
        p = os.path.join(fwd, name)
        if not os.path.exists(p):
            return False, f'archived firmware missing: firmware/{name} (build dir not captured)'
        want = (m.get(top, {}) or {}).get(key)
        if not A.valid_digest(want):
            return False, f'firmware/{name} manifest digest not a valid 64-hex ({want!r})'
        got = sha256(p)
        if got != want:
            return False, f'firmware/{name} rehash {got[:12]}.. != manifest {str(want)[:12]}.. (fabricated/tampered image)'
    for cfgname, arm in (('central.config', m.get('q3_arm')), ('periph.config', 'periph')):
        p = os.path.join(fwd, cfgname)
        r = subprocess.run([sys.executable, ASSERT_FSU, p, '--arm', arm], capture_output=True, text=True)
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip().splitlines()
            return False, f'assert_fsu_config FAILED on archived {cfgname} (arm={arm}): {tail[-1] if tail else "no output"}'
    return True, 'ok'

def lineage():
    import subprocess
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True,
                            cwd=HERE).stdout.strip() or 'UNKNOWN'
    return dict(source_commit=commit,
                analyze_q2=sha256(os.path.join(HERE, 'analyze_q2.py')),
                analyze_q3=sha256(os.path.join(HERE, 'analyze_q3.py')),
                combine_abba=sha256(os.path.join(HERE, 'combine_abba.py')),
                protocol=sha256(Q3PROTO) if os.path.exists(Q3PROTO) else 'MISSING',
                contract=Q3.q3_contract())

def check_manifest(d):
    """each ABBA cell must be an accepted, clean-tree, image-verified evidence cell
    whose stored log hashes still match the on-disk logs. Returns (ok, reason, m)."""
    mp = os.path.join(d, 'manifest.json')
    if not os.path.exists(mp):
        return False, 'no manifest.json (unarchived / not an evidence cell)', None
    try:
        m = json.load(open(mp))
    except ValueError as e:
        return False, f'unreadable manifest.json: {e}', None
    if m.get('verdict') != 'ACCEPT':
        return False, f'manifest verdict={m.get("verdict")} (not a promoted ACCEPT cell)', m
    if not m.get('observer_image_verified'):
        return False, 'observer_image_verified=false (--skip-obs-flash; unverified image)', m
    if m.get('tree_dirty'):
        return False, 'tree_dirty=true (a dirty/quarantined cell can never enter an ABBA block)', m
    if not m.get('q3_arm'):
        return False, 'no q3_arm (not a Q3 cell)', m
    if not m.get('q3_steady'):
        return False, 'q3_steady!=true (an ABBA cell must be a steady-state capture)', m
    if not (m.get('q3_final_params') or {}).get('interval'):
        return False, 'no q3_final_params (rev-6 conn-param-update gate not recorded)', m
    if m.get('q3_contract_deviation'):
        return False, f'q3_contract_deviation set (a deviated cell can never enter an ABBA block)', m
    stored = m.get('sha256', {})
    for name in ('obs.txt', 'central.txt', 'periph.txt'):
        cur = sha256(os.path.join(d, name))
        if stored.get(name) != cur:
            return False, f'{name} hash mismatch vs manifest (logs edited/replaced)', m
    return True, 'ok', m

def _cell_calib(m):
    """resolve + verify the cell's OWN calibration artifact (sha must match the
    manifest), then validate it. Returns (calib_frozen, reason)."""
    cp = (m.get('args') or {}).get('calib')
    if not cp or not os.path.exists(cp):
        return None, f'calibration artifact not resolvable ({cp!r})'
    if sha256(cp) != m.get('calibration_artifact'):
        return None, 'calibration file sha != manifest calibration_artifact (calib swapped)'
    cf, kr = A.validate_calib(cp)
    if cf is None:
        return None, f'calibration invalid: {kr}'
    return cf, 'ok'

def combine(cell_dirs, out_path):
    if len(cell_dirs) != N_CELLS:
        print(f'INCOMPLETE: need EXACTLY {N_CELLS} ABBA cells ({list(ARM_SEQ)} order), got {len(cell_dirs)}')
        return 3
    art_base = os.path.dirname(os.path.abspath(out_path))
    inputs, medians, mans = [], [], []
    for idx, d in enumerate(cell_dirs):
        obs, cen, per = (os.path.join(d, n) for n in ('obs.txt', 'central.txt', 'periph.txt'))
        for p in (obs, cen, per):
            if not os.path.exists(p):
                print(f'INCOMPLETE: {d} missing {os.path.basename(p)}'); return 3
        mok, reason, m = check_manifest(d)
        print(f'  [{idx}] {d}: manifest gate -> {"OK" if mok else "INCOMPLETE ("+reason+")"}')
        if not mok:
            print(f'INCOMPLETE: {d} failed the provenance gate: {reason}'); return 3
        arm = m.get('q3_arm')
        if arm != ARM_SEQ[idx]:
            print(f'INCOMPLETE: cell #{idx} arm={arm} != expected {ARM_SEQ[idx]} (wrong ABBA slot)'); return 3
        fok, freason = check_firmware_archive(d, m)
        print(f'  [{idx}] {d}: firmware re-verify -> {"OK" if fok else "INCOMPLETE ("+freason+")"}')
        if not fok:
            print(f'INCOMPLETE: {d} {freason}'); return 3
        cf, creason = _cell_calib(m)
        if cf is None:
            print(f'INCOMPLETE: {d} {creason}'); return 3
        r, rc = Q3.q3_steady_analyze(obs, cen, per, arm, calib_frozen=cf, quiet=True)
        verdict = r.get('verdict') if isinstance(r, dict) else None
        print(f'  [{idx}] {d}: re-analyze steady({arm}) -> {verdict} median={r.get("median") if isinstance(r,dict) else None}')
        if rc != 0 or verdict != 'STEADY-METRICS-OK':
            print(f'INCOMPLETE: {d} did not reach STEADY-METRICS-OK on re-analysis'); return 3
        inputs.append(dict(dir=os.path.relpath(os.path.abspath(d), art_base), arm=arm,
                           abba_seq=m.get('abba_seq'), median=r['median'], onchip_med=r.get('onchip_med'),
                           AA=m.get('connection', {}).get('AA'),
                           manifest_sha256=sha256(os.path.join(d, 'manifest.json')),
                           source_commit=m.get('source_commit'),
                           sha256=dict(obs=sha256(obs), central=sha256(cen), periph=sha256(per))))
        medians.append(r['median']); mans.append(m)
    # campaign binding (no substitution / no reorder)
    cok, creason = check_campaign(mans)
    print(f'  campaign gate -> {"OK" if cok else "INCOMPLETE ("+creason+")"}')
    if not cok:
        print(f'INCOMPLETE: {creason}'); return 3
    # homogeneity (matched replications; central differs only by arm; distinct AAs)
    hok, hreason = check_homogeneity(mans)
    print(f'  homogeneity gate -> {"OK" if hok else "INCOMPLETE ("+hreason+")"}')
    if not hok:
        print(f'INCOMPLETE: cells are not matched replications: {hreason}'); return 3
    # current-lineage binding (tooling/protocol/contract == this combiner's frozen tree)
    lok, lreason = check_lineage_current(mans)
    print(f'  current-lineage gate -> {"OK" if lok else "INCOMPLETE ("+lreason+")"}')
    if not lok:
        print(f'INCOMPLETE: {lreason}'); return 3
    # drift-cancelling estimate over A1 B1 B2 A2
    A1, B1, B2, A2 = medians
    step1, step2 = (A1 - B1), (A2 - B2)
    estimate = (step1 + step2) / 2.0
    drift_half = (step1 - step2) / 2.0        # diagnostic: linear-drift half-difference
    within = abs(estimate - EXPECT_STEP) <= TOL
    status = 'ABBA-CONFIRMED' if within else 'INCOMPLETE'
    art = dict(status=status,
               contract=dict(arm_seq=list(ARM_SEQ), expect_step=EXPECT_STEP, tol_ticks=TOL,
                             estimator='((A1-B1)+(A2-B2))/2'),
               lineage=lineage(),
               inputs=inputs, medians=medians,
               step_A1_B1=step1, step_A2_B2=step2,
               estimate_ticks=estimate, drift_half_ticks=drift_half,
               deviation_ticks=abs(estimate - EXPECT_STEP),
               estimate_tifs_step_us=estimate / A.TICKS_PER_US)
    with open(out_path, 'w') as f:
        json.dump(art, f, indent=2); f.write('\n')
    print(f'\nmedians(A1,B1,B2,A2)={medians}  steps=({step1},{step2})  '
          f'estimate={estimate} (|d|={abs(estimate-EXPECT_STEP)} <= {TOL})  drift_half={drift_half}')
    if not within:
        print(f'=== INCOMPLETE: drift-cancelled estimate {estimate}t off the {EXPECT_STEP}t step '
              f'by >{TOL} -> repeat a FRESH ABBA block -> {out_path} ==='); return 3
    print(f'=== ABBA-CONFIRMED: drift-cancelled step = {estimate}t '
          f'(~{estimate/A.TICKS_PER_US:.2f}us) within +/-{TOL}t of {EXPECT_STEP}t -> {out_path} ===')
    return 0

# ------------------------------- selftest ---------------------------------------
_H = lambda c: (c * 64)[:64]

PROV = os.path.join(HERE, '..', 'zephyr-patches', 'fsu-m0-series', 'provenance')

def _mkcell(tmp, tag, arm, seq, campaign='CID1', central_tag=None, periph_tag='PER',
            verified=True, dirty=False, manifest=True, tamper=False, board='nrf52dk/nrf52832',
            obs_tag='OBS', aa=None, calib_path=None, calib_ok=True, calib_sha_match=True,
            wrong_median=False, contract_dev='', q3contract=None, obs_dev='ODEV',
            periph_dev='PDEV', central_dev='CDEV', verdict='ACCEPT', q3_steady=True,
            start_ns=0, end_ns=1, lineage_override=None, cfg_arm=None, fw_hash_override=None,
            drop_fw=None, tamper_fw=None, final_params=None):
    """synthesize an ABBA steady cell dir with REAL archived firmware + a homogeneity
    -complete manifest whose firmware digests are the ACTUAL file hashes. central_tag
    defaults to the arm (so f150/f100 differ, same within-arm); the .config files are the
    real per-arm provenance configs so assert_fsu_config passes. Knobs drive negatives:
    fw_hash_override (fabricate a manifest digit != file), drop_fw (missing build file),
    tamper_fw (post-manifest binary edit), cfg_arm (wrong-arm config)."""
    import shutil
    d = os.path.join(tmp, tag); os.makedirs(d, exist_ok=True)
    obs, cl, pl = Q3._synth_steady(arm, wrong_median=wrong_median)
    for name, src in (('obs.txt', obs), ('central.txt', cl), ('periph.txt', pl)):
        shutil.move(src, os.path.join(d, name))
    if calib_path is None:
        calib_path = os.path.join(d, 'calib.json')
        _write_calib(calib_path, 3047, ok=calib_ok)
    calp = calib_path
    calsha = sha256(calp) if calib_sha_match else _H('9')
    # ---- write REAL archived firmware (firmware/<dev>.<ext>) ----
    fwd = os.path.join(d, 'firmware'); os.makedirs(fwd, exist_ok=True)
    ctag = central_tag if central_tag is not None else arm       # arm-specific by default
    cfg = cfg_arm or arm
    def w(name, content): open(os.path.join(fwd, name), 'w').write(content)
    w('observer.hex', f'OBSHEX-{obs_tag}'); w('observer.elf', f'OBSELF-{obs_tag}')
    w('observer.config', f'OBSCFG-{obs_tag}')
    w('central.hex', f'CENHEX-{ctag}'); w('central.elf', f'CENELF-{ctag}')
    shutil.copy2(os.path.join(PROV, f'central-fsu-{cfg}.config'), os.path.join(fwd, 'central.config'))
    w('periph.hex', f'PERHEX-{periph_tag}'); w('periph.elf', f'PERELF-{periph_tag}')
    shutil.copy2(os.path.join(PROV, 'periph-fsu.config'), os.path.join(fwd, 'periph.config'))
    fh = lambda n: sha256(os.path.join(fwd, n))
    if manifest:
        s = {n: sha256(os.path.join(d, n)) for n in ('obs.txt', 'central.txt', 'periph.txt')}
        s.update({'observer.hex': fh('observer.hex'), 'observer.elf': fh('observer.elf'),
                  'observer.config': fh('observer.config')})
        # REAL current-tree lineage hashes so the current-lineage gate passes (unless an
        # adversarial test overrides one to simulate stale/foreign tooling).
        for key, path in _current_lineage().items():
            s[key] = sha256(path)
        s.update({k: v for k, v in (fw_hash_override or {}).items() if k.startswith('observer.')})
        s.update(lineage_override or {})
        cbuild = {'zephyr.hex': fh('central.hex'), 'zephyr.elf': fh('central.elf'), '.config': fh('central.config')}
        pbuild = {'zephyr.hex': fh('periph.hex'), 'zephyr.elf': fh('periph.elf'), '.config': fh('periph.config')}
        for k, v in (fw_hash_override or {}).items():   # fabricate a manifest digit != the real file
            if k.startswith('central.'): cbuild[{'central.hex':'zephyr.hex','central.elf':'zephyr.elf','central.config':'.config'}[k]] = v
            if k.startswith('periph.'):  pbuild[{'periph.hex':'zephyr.hex','periph.elf':'zephyr.elf','periph.config':'.config'}[k]] = v
        man = dict(verdict=verdict, observer_image_verified=verified, tree_dirty=dirty, q3_arm=arm,
                   q3_steady=q3_steady, abba_campaign_id=campaign, abba_seq=seq,
                   q3_final_params=(final_params if final_params is not None
                                    else {'interval': 40, 'latency': 0, 'timeout': 42}),
                   abba_start_ns=start_ns, abba_end_ns=end_ns,
                   source_commit='deadbeef', board=board,
                   args=dict(mode='symmetric', calib=calp),
                   calibration_artifact=calsha,
                   devids=dict(obs=obs_dev, central=central_dev, periph=periph_dev),
                   connection=dict(AA=(aa or '0x' + tag.ljust(8, '0')), CRCInit='0x555555',
                                   map='000c000000', ch=10, phy='1M'),
                   central_build=cbuild, periph_build=pbuild,
                   q3_contract=(q3contract if q3contract is not None else Q3.q3_contract()),
                   q3_contract_deviation=contract_dev,
                   sha256=s)
        json.dump(man, open(os.path.join(d, 'manifest.json'), 'w'), indent=2)
    for n in (drop_fw or []):                    # missing-build-file negative
        os.remove(os.path.join(fwd, n))
    if tamper_fw:                                # post-cell binary edit (hash mismatch)
        with open(os.path.join(fwd, tamper_fw), 'a') as f: f.write('X')
    if tamper:
        with open(os.path.join(d, 'obs.txt'), 'a') as f:
            f.write('HOSTMS 99999 REC oseq=99999 s0=0x01 len=0 crc=1 rssi=-50 pairid=0 '
                    'txtag=0 st=0x00 addr=1 end=2 air_us=0 gap_us=0\n')
    return d

def _write_calib(path, frozen, ok=True):
    """minimal ESTABLISHED calib artifact that A.validate_calib accepts (or a naked
    blob it must reject when ok=False)."""
    if not ok:
        json.dump({'status': 'ESTABLISHED', 'frozen_gap_proxy_dev': frozen}, open(path, 'w'))
        return
    # reuse the real producer so the artifact carries a valid contract/lineage; freeze
    # at 3047 to match _synth_steady's f150 plateau (the real 150.5us hardware calib).
    import tempfile
    tmp = tempfile.mkdtemp()
    import combine_calib as CC
    CC.combine([CC._mkcell(tmp, 'ca', frozen), CC._mkcell(tmp, 'cb', frozen)], path)

def _block(tmp, prefix, overrides=None):
    """a clean 4-cell ABBA block (distinct AAs, ONE shared calib, arm-specific
    central images). `overrides` maps a cell index -> per-cell kwarg dict."""
    overrides = overrides or {}
    shared_cal = os.path.join(tmp, prefix + '_calib.json')
    _write_calib(shared_cal, 3047, ok=True)
    cells = []
    for i, arm in enumerate(ARM_SEQ):
        # strictly-ordered non-overlapping default intervals (start_i=1000*(i+1))
        kw = dict(campaign='CID_' + prefix, aa=f'0x{prefix}{i}0000000'[:10], calib_path=shared_cal,
                  start_ns=1000 * (i + 1), end_ns=1000 * (i + 1) + 500)
        kw.update(overrides.get(i, {}))
        cell_arm = kw.pop('arm', arm)          # arm/seq are positional -> pull from overrides
        cell_seq = kw.pop('seq', i)
        cells.append(_mkcell(tmp, f'{prefix}{i}', cell_arm, cell_seq, **kw))
    return cells

def selftest():
    import tempfile, shutil
    tmp = tempfile.mkdtemp(); R = []
    out = os.path.join(tmp, 'abba.json')
    # clean block -> CONFIRMED
    R.append(('clean ABBA block -> CONFIRMED', combine(_block(tmp, 'A'), out) == 0))
    art = json.load(open(out))
    R.append(('artifact CONFIRMED w/ estimate ~800 + 4 inputs',
              art['status'] == 'ABBA-CONFIRMED' and abs(art['estimate_ticks'] - 800) <= TOL
              and len(art['inputs']) == 4))
    # wrong count
    R.append(('three cells -> INCOMPLETE', combine(_block(tmp, 'B')[:3], out) == 3))
    # wrong arm order (swap slot 1 to f150)
    R.append(('wrong arm in slot -> INCOMPLETE',
              combine(_block(tmp, 'C', {1: dict(arm='f150')}), out) == 3))
    # a cell fails re-analysis (wrong median plateau)
    R.append(('cell fails re-analysis -> INCOMPLETE',
              combine(_block(tmp, 'D', {2: dict(wrong_median=True)}), out) == 3))
    # cross-campaign substitution (one cell from another campaign id)
    R.append(('mixed campaign ids -> INCOMPLETE',
              combine(_block(tmp, 'E', {3: dict(campaign='OTHER')}), out) == 3))
    # reordered seq (slot 2 carries seq=3)
    R.append(('reordered abba_seq -> INCOMPLETE',
              combine(_block(tmp, 'F', {2: dict(seq=3)}), out) == 3))
    # unverified observer image
    R.append(('unverified observer -> INCOMPLETE',
              combine(_block(tmp, 'G', {0: dict(verified=False)}), out) == 3))
    # dirty tree cell
    R.append(('dirty-tree cell -> INCOMPLETE',
              combine(_block(tmp, 'H', {1: dict(dirty=True)}), out) == 3))
    # a runner-quarantined cell (verdict != ACCEPT) cannot enter an ABBA block
    R.append(('quarantined verdict -> INCOMPLETE',
              combine(_block(tmp, 'H2', {2: dict(verdict='QUARANTINED')}), out) == 3))
    # log tampered after manifest
    R.append(('log hash mismatch -> INCOMPLETE',
              combine(_block(tmp, 'I', {0: dict(tamper=True)}), out) == 3))
    # peripheral image differs (not a matched replication)
    R.append(('peripheral image differs -> INCOMPLETE',
              combine(_block(tmp, 'J', {2: dict(periph_tag='DIFF')}), out) == 3))
    # the two f150 cells used DIFFERENT central images (arm not the only variable)
    R.append(('f150 central images differ -> INCOMPLETE',
              combine(_block(tmp, 'K', {3: dict(central_tag='WEIRD')}), out) == 3))
    # an f100 cell carrying the f150 central config -> assert_fsu_config rejects it
    # (you cannot fake "arm is the only difference" past the archived-config assertion)
    R.append(('f100 cell with f150 central config -> INCOMPLETE',
              combine(_block(tmp, 'L', {1: dict(cfg_arm='f150')}), out) == 3))
    # duplicate connection AA (not reset-isolated)
    R.append(('duplicate connection AA -> INCOMPLETE',
              combine(_block(tmp, 'M', {0: dict(aa='0xdupe0000'), 3: dict(aa='0xdupe0000')}), out) == 3))
    # calibration artifact swapped (sha != manifest)
    R.append(('calib sha mismatch -> INCOMPLETE',
              combine(_block(tmp, 'N', {1: dict(calib_sha_match=False)}), out) == 3))
    # invalid calibration blob (per-cell naked blob that validate_calib rejects)
    R.append(('invalid calib blob -> INCOMPLETE',
              combine(_block(tmp, 'O', {2: dict(calib_path=None, calib_ok=False)}), out) == 3))
    # a contract deviation on one cell (frozen contract must be clean)
    R.append(('contract deviation -> INCOMPLETE',
              combine(_block(tmp, 'P', {0: dict(contract_dev='guard-override')}), out) == 3))
    # a differing frozen q3_contract (lineage drift)
    R.append(('q3_contract drift -> INCOMPLETE',
              combine(_block(tmp, 'Q', {3: dict(q3contract={'step_tol': 9})}), out) == 3))
    # --- rev-4 enforcement: current-lineage binding (F1) ---
    stale = {'analyze_q3.py': _H('9')}   # a fabricated analyzer hash on ALL four cells
    R.append(('whole-block stale analyzer lineage -> INCOMPLETE',
              combine(_block(tmp, 'R', {i: dict(lineage_override=stale) for i in range(4)}), out) == 3))
    stalec = {i: dict(q3contract={'step_tol': 4, 'foo': 1}) for i in range(4)}   # same drift on ALL cells
    R.append(('whole-block stale q3_contract -> INCOMPLETE',
              combine(_block(tmp, 'R2', stalec), out) == 3))
    R.append(('whole-block stale assert_fsu_config lineage -> INCOMPLETE',
              combine(_block(tmp, 'R3', {i: dict(lineage_override={'assert_fsu_config.py': _H('9')}) for i in range(4)}), out) == 3))
    # --- rev-4 enforcement: chronological ordering (F2) ---
    R.append(('overlapping collection intervals -> INCOMPLETE',
              combine(_block(tmp, 'S', {2: dict(start_ns=1500, end_ns=4000)}), out) == 3))   # #2 overlaps #1 and #3
    R.append(('out-of-order intervals (later cell earlier ns) -> INCOMPLETE',
              combine(_block(tmp, 'S2', {3: dict(start_ns=10, end_ns=20)}), out) == 3))
    R.append(('missing collection timestamps -> INCOMPLETE',
              combine(_block(tmp, 'S3', {1: dict(start_ns=None, end_ns=None)}), out) == 3))
    # --- rev-4 enforcement: steady flag required (F2/F4) ---
    R.append(('non-steady cell -> INCOMPLETE',
              combine(_block(tmp, 'T', {2: dict(q3_steady=False)}), out) == 3))
    # --- rev-4 enforcement: observer ELF/config digests (F4) ---
    R.append(('observer ELF not a valid digest -> INCOMPLETE',
              combine(_block(tmp, 'U', {0: dict(lineage_override={'observer.elf': 'UNVERIFIED(--skip-obs-flash)'})}), out) == 3))
    R.append(('observer .config not a valid digest -> INCOMPLETE',
              combine(_block(tmp, 'U2', {3: dict(lineage_override={'observer.config': 'MISSING'})}), out) == 3))
    # --- rev-5 enforcement: BINARY provenance (firmware re-resolved + rehashed + asserted) ---
    # fabricated manifest digit with no matching firmware file behind it
    R.append(('fabricated central.hex digest (file unchanged) -> INCOMPLETE',
              combine(_block(tmp, 'W', {1: dict(fw_hash_override={'central.hex': _H('9')})}), out) == 3))
    R.append(('fabricated observer.elf digest -> INCOMPLETE',
              combine(_block(tmp, 'W2', {0: dict(fw_hash_override={'observer.elf': _H('9')})}), out) == 3))
    # a build file not archived in the cell (missing build dir/file)
    R.append(('missing archived firmware file -> INCOMPLETE',
              combine(_block(tmp, 'X', {2: dict(drop_fw=['periph.elf'])}), out) == 3))
    # post-cell binary edit -> rehash mismatch
    R.append(('post-cell firmware edit -> INCOMPLETE',
              combine(_block(tmp, 'Y', {3: dict(tamper_fw='central.hex')}), out) == 3))
    # an archived central .config that is NOT the registered arm's firmware
    R.append(('archived central config fails assert_fsu_config -> INCOMPLETE',
              combine(_block(tmp, 'Z', {0: dict(cfg_arm='f100')}), out) == 3))   # arm f150 slot w/ f100 config
    # --- rev-6: identical final connection parameters across the block ---
    R.append(('mismatched final conn params -> INCOMPLETE',
              combine(_block(tmp, 'FP', {2: dict(final_params={'interval': 24, 'latency': 0, 'timeout': 42})}), out) == 3))
    R.append(('missing final conn params -> INCOMPLETE',
              combine(_block(tmp, 'FP2', {1: dict(final_params={})}), out) == 3))
    R.append(('clean block still CONFIRMED after all rev-4/rev-5/rev-6 gates',
              combine(_block(tmp, 'V'), out) == 0))
    shutil.rmtree(tmp, ignore_errors=True)
    print('=== ABBA COMBINER SELFTEST ===')
    for n, v in R: print(f'  [{"PASS" if v else "FAIL"}] {n}')
    ok = all(v for _, v in R); print(f'ABBA COMBINER SELFTEST: {"PASS" if ok else "FAIL"}')
    return 0 if ok else 1

def main():
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        sys.exit(selftest())
    out = 'abba-confirmation.json'
    if '--out' in a:
        i = a.index('--out'); out = a[i + 1]; del a[i:i + 2]
    if not a:
        print(__doc__); sys.exit(2)
    sys.exit(combine(a, out))

if __name__ == '__main__':
    main()
