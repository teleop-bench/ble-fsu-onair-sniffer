#!/usr/bin/env python3
"""Symmetric-control COMBINER -> provenance-bound calibration artifact.

The frozen gap_proxy_dev reference is NOT a naked number typed into near/far. It
is ESTABLISHED here from >=2 ACCEPTED symmetric controls, under a pre-registered
contract (Q2-ACCEPTANCE-PROTOCOL.md rev-12):

  - PROVENANCE GATE: each input cell must carry a manifest.json whose
    verdict==ACCEPT and observer_image_verified==true, and whose stored log hashes
    still match the on-disk logs (rejects unverified/--skip-obs-flash cells,
    quarantined runs, missing manifests, and post-hoc log edits).
  - HOMOGENEITY GATE: the controls must be MATCHED replications -- same run mode
    (symmetric), observer board + verified image, central/peripheral DEVICEIDs in
    the same roles, PHY/map/observer-channel, analyzer/combiner/protocol lineage,
    and endpoint firmware/config identity (all as VALID 64-hex digests, never
    MISSING). AA/CRCInit differ per connection and source_commit is NOT matched
    (equal HEAD would force leaving HEAD unchanged across both runs) -- both are
    still recorded per input for audit.
  - Each input cell is RE-ANALYZED from its raw logs (obs/central/periph) -- a
    stale analysis.txt is never trusted. It must ACCEPT in `symmetric` mode.
  - The artifact records tool/source LINEAGE (commit + analyzer/combiner/protocol
    hashes) and each input's manifest hash + source commit.
  - frozen_gap_proxy_dev = round-half-to-even( median of the per-run medians ),
    an integer tick (sub-tick precision is below instrument resolution).
  - CONSISTENCY: every control median must be within +-SPREAD_MAX_TICKS of the
    frozen reference; else calibration is INCONSISTENT and NO reference is
    established (exit 3, no usable artifact).
  - The artifact records each input's sha256 (obs+central+periph), its verdict
    and median, the cross-run spread, and the frozen value, so near/far can prove
    which controls produced its reference.

Usage: combine_calib.py --out gap-proxy-calibration.json <cellDir1> <cellDir2> [...]
  each cellDir holds obs.txt, central.txt, periph.txt (as the runner archives).
Exit 0 ESTABLISHED, 3 INCONSISTENT/insufficient, 2 usage.
"""
import sys, os, json, hashlib
import statistics as st
import analyze_q2 as A

# shared with the validate_calib() consumer so producer/validator never diverge
SPREAD_MAX_TICKS = A.CALIB_SPREAD_MAX_TICKS
MIN_CONTROLS = A.CALIB_MIN_CONTROLS
round_half_even = A.calib_round

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(65536), b''):
            h.update(b)
    return h.hexdigest()

def invariants(m):
    """the fields that MUST agree across matched-replication controls. AA/CRCInit
    change per connection and are excluded. source_commit is NOT matched (equal
    HEAD would force leaving HEAD unchanged across both runs -- no intervening
    commit); the tool/firmware/config hashes carry the real experimental identity
    instead. source_commit is still recorded per input for audit."""
    c = m.get('connection', {}); s = m.get('sha256', {})
    return {
        'mode': m.get('args', {}).get('mode'),
        'board': m.get('board'),
        'observer.hex': s.get('observer.hex'),
        'central_dev': m.get('devids', {}).get('central'),
        'periph_dev': m.get('devids', {}).get('periph'),
        'phy': c.get('phy'), 'map': c.get('map'), 'ch': c.get('ch'),
        'analyze_q2': s.get('analyze_q2.py'),
        'combine_calib': s.get('combine_calib.py'),
        'protocol': s.get('Q2-ACCEPTANCE-PROTOCOL.md'),
        'central_build': json.dumps(m.get('central_build'), sort_keys=True),
        'periph_build': json.dumps(m.get('periph_build'), sort_keys=True),
    }

def check_digests(m):
    """INDEPENDENT digest-schema enforcement (the combiner does not trust the
    runner): endpoint builds + observer image must be real 64-hex, never MISSING/
    UNVERIFIED/empty. This is what closes the '/does/not/exist -> MISSING' path."""
    for role in ('central_build', 'periph_build'):
        bd = m.get(role, {})
        for k in ('zephyr.hex', '.config'):
            if not A.valid_digest(bd.get(k)):
                return False, f'{role}.{k} is not a valid 64-hex digest ({bd.get(k)!r})'
    if not A.valid_digest(m.get('sha256', {}).get('observer.hex')):
        return False, 'observer.hex is not a valid 64-hex digest (unverified image)'
    return True, 'ok'

def check_homogeneity(mans):
    """all controls are matched replications with VALID (not just present) endpoint
    + observer build provenance."""
    for m in mans:
        if m.get('args', {}).get('mode') != 'symmetric':
            return False, f'a control mode != symmetric ({m.get("args",{}).get("mode")})'
        dok, dreason = check_digests(m)
        if not dok:
            return False, dreason
    ref = invariants(mans[0])
    for i, m in enumerate(mans[1:], 1):
        cur = invariants(m)
        diff = [k for k in ref if ref[k] != cur[k]]
        if diff:
            return False, f'control #{i} differs from #0 in {diff} (not a matched replication)'
    return True, 'ok'

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, '..', 'debug-evidence', 'observer-q1-20260810', 'Q2-ACCEPTANCE-PROTOCOL.md')

def lineage():
    """tool/source provenance recorded INTO the artifact, so the frozen reference
    is bound to the exact analyzer/combiner/protocol/commit that produced it."""
    import subprocess
    commit = subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True,
                            cwd=HERE).stdout.strip() or 'UNKNOWN'
    return dict(source_commit=commit,
                analyze_q2=sha256(os.path.join(HERE,'analyze_q2.py')),
                combine_calib=sha256(os.path.join(HERE,'combine_calib.py')),
                protocol=sha256(PROTO) if os.path.exists(PROTO) else 'MISSING')

def check_manifest(d):
    """PROVENANCE GATE: each control must carry an ACCEPTED, image-VERIFIED
    manifest whose stored log hashes still match the on-disk logs. Returns
    (ok, reason, manifest)."""
    mp = os.path.join(d, 'manifest.json')
    if not os.path.exists(mp):
        return False, 'no manifest.json (unarchived / not an evidence cell)', None
    try:
        m = json.load(open(mp))
    except ValueError as e:
        return False, f'unreadable manifest.json: {e}', None
    if m.get('verdict') != 'ACCEPT':
        return False, f'manifest verdict={m.get("verdict")} (not ACCEPT)', m
    if not m.get('observer_image_verified'):
        return False, 'observer_image_verified=false (--skip-obs-flash; unverified image)', m
    stored = m.get('sha256', {})
    for name in ('obs.txt', 'central.txt', 'periph.txt'):
        cur = sha256(os.path.join(d, name))
        if stored.get(name) != cur:
            return False, f'{name} hash mismatch vs manifest (logs edited/replaced)', m
    return True, 'ok', m

def combine(cell_dirs, out_path):
    if len(cell_dirs) < MIN_CONTROLS:
        print(f'REJECT: need >={MIN_CONTROLS} symmetric controls, got {len(cell_dirs)}')
        return 3
    inputs, medians, mans = [], [], []
    # inputs' `dir` is stored RELATIVE to the artifact so the consumer can resolve
    # and rehash the evidence no matter its working directory.
    art_base = os.path.dirname(os.path.abspath(out_path))
    for d in cell_dirs:
        obs, cen, per = (os.path.join(d, n) for n in ('obs.txt', 'central.txt', 'periph.txt'))
        for p in (obs, cen, per):
            if not os.path.exists(p):
                print(f'REJECT: {d} missing {os.path.basename(p)}'); return 3
        mok, reason, m = check_manifest(d)
        print(f'  {d}: manifest gate -> {"OK" if mok else "REJECT ("+reason+")"}')
        if not mok:
            print(f'REJECT: {d} failed the provenance gate: {reason}'); return 3
        r = {}
        rc = A.analyze(obs, cen, per, 'symmetric', 'central', None, quiet=True, result=r)
        verdict = 'ACCEPT' if rc == 0 else 'REJECT'
        print(f'  {d}: re-analyze -> {verdict} gap_proxy_med={r.get("med")}')
        if rc != 0 or r.get('med') is None:
            print(f'REJECT: {d} did not ACCEPT as a symmetric control'); return 3
        inputs.append(dict(dir=os.path.relpath(os.path.abspath(d), art_base),
                           verdict=verdict, median=r['med'],
                           manifest_sha256=sha256(os.path.join(d,'manifest.json')),
                           source_commit=m.get('source_commit'),
                           sha256=dict(obs=sha256(obs), central=sha256(cen), periph=sha256(per))))
        medians.append(r['med']); mans.append(m)
    # HOMOGENEITY: controls must be matched replications with endpoint provenance
    hok, hreason = check_homogeneity(mans)
    print(f'  homogeneity gate -> {"OK" if hok else "REJECT ("+hreason+")"}')
    if not hok:
        print(f'REJECT: controls are not matched replications: {hreason}'); return 3
    frozen_raw = st.median(medians)
    frozen = round_half_even(frozen_raw)
    spread = max(medians) - min(medians)
    within = all(abs(m - frozen) <= SPREAD_MAX_TICKS for m in medians)
    status = 'ESTABLISHED' if within else 'INCONSISTENT'
    art = dict(status=status,
               contract=dict(min_controls=MIN_CONTROLS, spread_max_ticks=SPREAD_MAX_TICKS,
                             rounding=A.CALIB_ROUNDING, proxy=A.CALIB_PROXY),
               lineage=lineage(),
               inputs=inputs, medians=medians,
               median_of_medians=frozen_raw, spread_ticks=spread,
               frozen_gap_proxy_dev=frozen if within else None,
               calibrated_tifs_us=(frozen - A.PREAMBLE_AA_OFF) / A.TICKS_PER_US if within else None)
    with open(out_path, 'w') as f:
        json.dump(art, f, indent=2); f.write('\n')
    print(f'\nmedians={medians} median-of-medians={frozen_raw} frozen={frozen} '
          f'spread={spread} ticks (max {SPREAD_MAX_TICKS})')
    if not within:
        print(f'=== INCONSISTENT: a control median is >{SPREAD_MAX_TICKS} ticks from '
              f'the reference; NO frozen gap_proxy_dev established -> {out_path} ==='); return 3
    print(f'=== ESTABLISHED: frozen_gap_proxy_dev={frozen} ticks '
          f'(calibrated tIFS ~= {art["calibrated_tifs_us"]:.2f}us) -> {out_path} ===')
    return 0

_H = lambda c: (c * 64)[:64]   # a valid 64-hex placeholder digest for a hue c in 0-9a-f

def _mkcell(tmp, tag, tifs, verified=True, verdict='ACCEPT', manifest=True, tamper=False,
            board='nrf52dk/nrf52832', central_dev='CDEV', periph_dev='PDEV', builds=True,
            mode='symmetric', build_missing_key=False, build_bad_digest=False, obs_hex=_H('a')):
    """build a synthetic symmetric cell dir + a HOMOGENEITY-complete manifest.json
    with VALID 64-hex digests. Knobs drive the negative tests: verified=False
    (--skip-obs-flash), non-ACCEPT verdict, missing manifest, post-manifest log edit
    (tamper), mismatched board/DEVICEID/mode, missing endpoint builds, a build dict
    missing a key, a 'MISSING'-filled build digest, or a bad observer.hex digest."""
    import shutil
    d = os.path.join(tmp, tag); os.makedirs(d, exist_ok=True)
    obs, cl, pl = A._synth(n=300, near=-50, far=-52, tifs=tifs)
    paths = {'obs.txt': obs, 'central.txt': cl, 'periph.txt': pl}
    for name, src in paths.items():
        shutil.move(src, os.path.join(d, name))
    if manifest:
        s = {n: sha256(os.path.join(d, n)) for n in paths}
        s.update({'observer.hex': obs_hex, 'analyze_q2.py': _H('b'),
                  'combine_calib.py': _H('c'), 'Q2-ACCEPTANCE-PROTOCOL.md': _H('d')})
        cbuild = {'zephyr.hex': _H('e'), '.config': _H('f')}
        pbuild = {'zephyr.hex': _H('1'), '.config': _H('2')}
        if build_missing_key: cbuild.pop('.config')          # dict present but a key absent
        if build_bad_digest:  cbuild['zephyr.hex'] = 'MISSING'  # nonexistent-artifact case
        man = dict(verdict=verdict, observer_image_verified=verified,
                   source_commit='deadbeef', args=dict(mode=mode), board=board,
                   devids=dict(obs='ODEV', central=central_dev, periph=periph_dev),
                   connection=dict(AA='0x'+tag.ljust(8,'0'), CRCInit='0x555555',
                                   map='000c000000', ch=10, phy='1M'),
                   central_build=(cbuild if builds else {}),
                   periph_build=(pbuild if builds else {}),
                   sha256=s)
        json.dump(man, open(os.path.join(d, 'manifest.json'), 'w'), indent=2)
    if tamper:   # edit a log AFTER the manifest is written -> hash mismatch
        with open(os.path.join(d, 'obs.txt'), 'a') as f:
            f.write('HOSTMS 99999 REC oseq=99999 s0=0x01 len=0 crc=1 rssi=-50 pairid=0 '
                    'txtag=0 st=0x00 addr=1 end=2 air_us=0 gap_us=0\n')
    return d

def selftest():
    import tempfile, shutil
    tmp = tempfile.mkdtemp(); T = A.TIFS_EXPECT; R = []
    out = os.path.join(tmp, 'cal.json')
    # 2 consistent controls (same gap_proxy) -> ESTABLISHED
    R.append(('two-consistent-established',
              combine([_mkcell(tmp,'a',T), _mkcell(tmp,'b',T)], out) == 0))
    est = json.load(open(out))
    R.append(('artifact ESTABLISHED w/ frozen + input hashes',
              est['status']=='ESTABLISHED' and est['frozen_gap_proxy_dev']==T
              and all('sha256' in i for i in est['inputs'])))
    # 2 controls 4 ticks apart -> each 2 from the midpoint reference -> ESTABLISHED
    R.append(('two-within-2ticks-of-ref-established',
              combine([_mkcell(tmp,'c',T), _mkcell(tmp,'d',T+4)], out) == 0))
    # 2 controls 6 ticks apart -> each 3 from the midpoint (>2) -> INCONSISTENT
    R.append(('two-6ticks-inconsistent',
              combine([_mkcell(tmp,'e',T), _mkcell(tmp,'f',T+6)], out) == 3))
    R.append(('inconsistent artifact has no frozen value',
              json.load(open(out))['frozen_gap_proxy_dev'] is None))
    # single control -> insufficient (CANDIDATE only, never ESTABLISHED)
    R.append(('single-control-insufficient',
              combine([_mkcell(tmp,'g',T)], out) == 3))
    # --- provenance-gate negative tests ---
    # unverified observer image (--skip-obs-flash) -> combiner rejects
    R.append(('unverified-observer-rejected',
              combine([_mkcell(tmp,'h',T), _mkcell(tmp,'i',T,verified=False)], out) == 3))
    # missing manifest -> rejects
    R.append(('missing-manifest-rejected',
              combine([_mkcell(tmp,'j',T), _mkcell(tmp,'k',T,manifest=False)], out) == 3))
    # non-ACCEPT (QUARANTINED) manifest verdict -> rejects
    R.append(('quarantined-verdict-rejected',
              combine([_mkcell(tmp,'l',T), _mkcell(tmp,'m',T,verdict='QUARANTINED')], out) == 3))
    # log edited after manifest (hash mismatch) -> rejects
    R.append(('hash-mismatch-rejected',
              combine([_mkcell(tmp,'n',T), _mkcell(tmp,'o',T,tamper=True)], out) == 3))
    # --- homogeneity-gate negative tests (not matched replications) ---
    R.append(('mismatched-board-rejected',
              combine([_mkcell(tmp,'r',T), _mkcell(tmp,'s',T,board='nrf52840dk')], out) == 3))
    R.append(('mismatched-DEVICEID-rejected',
              combine([_mkcell(tmp,'t',T), _mkcell(tmp,'u',T,central_dev='OTHER')], out) == 3))
    R.append(('missing-endpoint-build-rejected',
              combine([_mkcell(tmp,'v',T), _mkcell(tmp,'w',T,builds=False)], out) == 3))
    R.append(('non-symmetric-mode-rejected',
              combine([_mkcell(tmp,'x',T), _mkcell(tmp,'y',T,mode='nearfar')], out) == 3))
    # --- digest-schema negative tests (the '/does/not/exist -> MISSING' false-accept) ---
    R.append(('build-digest-MISSING-rejected',
              combine([_mkcell(tmp,'da',T), _mkcell(tmp,'db',T,build_bad_digest=True)], out) == 3))
    R.append(('build-dict-missing-key-rejected',
              combine([_mkcell(tmp,'dc',T), _mkcell(tmp,'dd',T,build_missing_key=True)], out) == 3))
    R.append(('observer-hex-non-64hex-rejected',
              combine([_mkcell(tmp,'de',T), _mkcell(tmp,'df',T,obs_hex='UNVERIFIED(--skip-obs-flash)')], out) == 3))
    # an established artifact records tool/source lineage
    combine([_mkcell(tmp,'p',T), _mkcell(tmp,'q',T)], out)
    lin = json.load(open(out)).get('lineage', {})
    R.append(('lineage recorded (commit + analyzer/combiner/protocol hashes)',
              all(k in lin for k in ('source_commit','analyze_q2','combine_calib','protocol'))))
    shutil.rmtree(tmp, ignore_errors=True)
    print('=== COMBINER SELFTEST ===')
    for n, v in R: print(f'  [{"PASS" if v else "FAIL"}] {n}')
    ok = all(v for _, v in R); print(f'COMBINER SELFTEST: {"PASS" if ok else "FAIL"}')
    return 0 if ok else 1

def main():
    a = sys.argv[1:]
    if a and a[0] == '--selftest':
        sys.exit(selftest())
    out = 'gap-proxy-calibration.json'
    if '--out' in a:
        i = a.index('--out'); out = a[i+1]; del a[i:i+2]
    if not a:
        print(__doc__); sys.exit(2)
    sys.exit(combine(a, out))

if __name__ == '__main__':
    main()
