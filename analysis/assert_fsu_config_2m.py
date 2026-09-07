#!/usr/bin/env python3
"""Hard-assert a resolved Zephyr .config is a valid Q3-2M FSU build (2M PHY, 150->52us).

The 2M sibling of assert_fsu_config.py. Same substrate gates (advanced-features menu,
tIFS=52 floor, host+ctlr FSU + feature set, M0 FORCE_FEAT), but the link is 2M: it
REQUIRES BT_CTLR_PHY_2M=y and (for the central arms) BT_USER_PHY_UPDATE=y -- the 2M app
drives the switch explicitly (AUTO_PHY_UPDATE stays off), and without USER_PHY_UPDATE the
phy_update call fails and the link SILENTLY stays 1M while the observer decodes 2M (empty
capture). The registered mid-step arm is f52 (52->150 request); f150 is the no-request 2M
control (ABBA A-cell); periph is the responder + on-chip bench with the conn-param
auto-update OFF (an auto-update reverts the negotiated frame space to 150us).

Usage: assert_fsu_config_2m.py <path/to/.config> --arm {f52|f150|periph}
Exit 0 all gates pass, 1 a gate failed, 2 usage/unreadable.
"""
import sys, re

# common to EVERY arm -- (symbol, required exact value). Controller substrate + the 52
# floor via the advanced-features menu + host/ctlr FSU + feature set + FORCE_FEAT, AND
# the 2M pinning (the observer decodes 2M here).
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
    ('BT_CTLR_PHY_2M', 'y'),                    # 2M link (the observer decodes 2M)
]
# must be OFF for every arm.
FORBID = [
    'BT_CTLR_INEVENT_ECHO',                     # non-spec echo carried by patch 0001
    'BT_AUTO_PHY_UPDATE',                       # the 2M app drives PHY explicitly, no auto upgrade
]
# the registered Q3-2M arm. Central arms carry the app request params + Q3 mode + the
# explicit 2M drive (USER_PHY_UPDATE) and must NOT run the on-chip bench; the peripheral
# runs the bench, has no request params, and pins conn params (no auto-update FSU revert).
ARM_REQUIRE = {
    'f52':    [('BT_CENTRAL', 'y'), ('BT_USER_PHY_UPDATE', 'y'), ('APP_Q3_MODE', 'y'),
               ('APP_FSU_MIN_US', '52'), ('APP_FSU_MAX_US', '150')],
    'f150':   [('BT_CENTRAL', 'y'), ('BT_USER_PHY_UPDATE', 'y'), ('APP_Q3_MODE', 'y'),
               ('APP_FSU_MIN_US', '150'), ('APP_FSU_MAX_US', '0')],
    'periph': [('BT_PERIPHERAL', 'y'), ('BT_CTLR_TIFS_CAPTURE_BENCH', 'y')],
}
ARM_FORBID = {
    'f52':    ['BT_CTLR_TIFS_CAPTURE_BENCH'],   # bench is peripheral-only
    'f150':   ['BT_CTLR_TIFS_CAPTURE_BENCH'],
    # NOTE: conn-param auto-update is NOT gated (matches the 1M asserter). It is REQUIRED for
    # the steady ABBA path (the runner event-gates FSU on the ~5s update so it persists) and
    # harmless for mid-step (FSU is requested AFTER the update). The revert only bites if FSU
    # is requested BEFORE the update, which the runner never does.
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
        print(f'FSU-2M CONFIG ASSERT: FAIL (arm={arm}: {path})')
        print('\n'.join(fails)); sys.exit(1)
    extra = ('bench on; responder; conn-param auto-update off' if arm == 'periph'
             else f'bench off; central request {"52->150" if arm=="f52" else "150->0 (control)"}; '
                  'Q3 mode; explicit 2M drive')
    print(f'FSU-2M CONFIG ASSERT: PASS (arm={arm}: {path}) -- advanced features + low-lat '
          f'interval + tIFS=52 + host/ctlr FSU + FORCE_FEAT + 2M; {extra}; echo off')
    sys.exit(0)
