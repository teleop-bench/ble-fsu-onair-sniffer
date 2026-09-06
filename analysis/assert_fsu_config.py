#!/usr/bin/env python3
"""Hard-assert a resolved Zephyr .config is a valid Q3 FSU build.

The tIFS-52 floor + low-latency interval live inside Kconfig's "Advanced features"
menu (`visible if BT_CTLR_ADVANCED_FEATURES`); a Q3 overlay that requests them but
forgets ADVANCED_FEATURES=y builds CLEAN yet SILENTLY keeps the 150us floor. This
script fails LOUD on that (and on the non-spec in-event echo being left on).

Usage: assert_fsu_config.py <path/to/.config> --arm {f100|f150|periph}
  f100/f150 = central request arms; periph = the responder (+ on-chip bench).
Exit 0 all gates pass, 1 a gate failed, 2 usage/unreadable.
"""
import sys, re

# common to EVERY arm -- (symbol, required exact value). Covers the controller
# substrate (open ctlr, 52 floor via the advanced-features menu, host+ctlr FSU +
# feature set, M0's FORCE_FEAT to enter the procedure) AND the 1M-only pinning.
REQUIRE = [
    ('BT_LL_SW_SPLIT', 'y'),                    # open controller
    ('BT_CTLR_ADVANCED_FEATURES', 'y'),         # unhides the two low-latency symbols
    ('BT_CTLR_CONN_INTERVAL_LOW_LATENCY', 'y'), # required for the 52 floor to apply
    ('BT_CTLR_EVENT_IFS_LOW_LAT_US', '52'),     # the low-latency tIFS floor -- EXACTLY 52
    ('BT_FRAME_SPACE_UPDATE', 'y'),             # host FSU
    ('BT_CTLR_FRAME_SPACE_UPDATE', 'y'),        # controller FSU
    ('BT_LE_EXTENDED_FEAT_SET', 'y'),           # feature bit 65 is beyond the 64-bit mask
    ('BT_CTLR_EXTENDED_FEAT_SET', 'y'),
    ('BT_CTLR_FSU_BENCH_FORCE_FEAT', 'y'),      # M0 needs this to ENTER the FSU procedure
]
# 1M-only pinning (the observer decodes 1M); must be OFF for every arm.
FORBID = [
    'BT_CTLR_INEVENT_ECHO',                     # non-spec echo carried by patch 0001
    'BT_CTLR_PHY_2M',                           # 1M-only: no 2M
    'BT_AUTO_PHY_UPDATE',                       # 1M-only: no auto PHY upgrade
]
# the registered Q3 arm. Central arms carry the app request params + Q3 mode and must
# NOT run the on-chip bench; the peripheral runs the bench and has no request params.
ARM_REQUIRE = {
    'f100':   [('BT_CENTRAL', 'y'), ('APP_Q3_MODE', 'y'), ('APP_FSU_MIN_US', '100'), ('APP_FSU_MAX_US', '150')],
    'f150':   [('BT_CENTRAL', 'y'), ('APP_Q3_MODE', 'y'), ('APP_FSU_MIN_US', '150'), ('APP_FSU_MAX_US', '0')],
    'periph': [('BT_PERIPHERAL', 'y'), ('BT_CTLR_TIFS_CAPTURE_BENCH', 'y')],
}
ARM_FORBID = {
    'f100':   ['BT_CTLR_TIFS_CAPTURE_BENCH'],   # bench is peripheral-only
    'f150':   ['BT_CTLR_TIFS_CAPTURE_BENCH'],
    'periph': [],
}
ARMS = tuple(ARM_REQUIRE)


def parse(path):
    cfg = {}
    for ln in open(path, errors='replace'):
        m = re.match(r'CONFIG_(\w+)=(.+)', ln.strip())
        if m:
            cfg[m.group(1)] = m.group(2).strip()
        m = re.match(r'# CONFIG_(\w+) is not set', ln.strip())
        if m:
            cfg[m.group(1)] = None   # explicitly unset
    return cfg


def check(path, arm):
    cfg = parse(path)
    fails = []
    for sym, want in REQUIRE + ARM_REQUIRE[arm]:
        got = cfg.get(sym, '<absent>')
        if got != want:
            fails.append(f'  REQUIRE CONFIG_{sym}={want} but got {got}')
    for sym in FORBID + ARM_FORBID[arm]:
        got = cfg.get(sym, None)
        if got not in (None, 'n'):
            fails.append(f'  FORBID CONFIG_{sym} must be off but got {got}')
    return fails


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[2] != '--arm' or sys.argv[3] not in ARMS:
        print(__doc__); sys.exit(2)
    path, arm = sys.argv[1], sys.argv[3]
    try:
        fails = check(path, arm)
    except OSError as e:
        print(f'unreadable: {e}'); sys.exit(2)
    if fails:
        print(f'FSU CONFIG ASSERT: FAIL (arm={arm}: {path})')
        print('\n'.join(fails)); sys.exit(1)
    extra = ('bench on; responder' if arm == 'periph'
             else f'bench off; central request {"100->150" if arm=="f100" else "150->0 (control)"}; Q3 mode')
    print(f'FSU CONFIG ASSERT: PASS (arm={arm}: {path}) -- advanced features + low-lat '
          f'interval + tIFS=52 + host/ctlr FSU + FORCE_FEAT + 1M-only; {extra}; echo off')
    sys.exit(0)
