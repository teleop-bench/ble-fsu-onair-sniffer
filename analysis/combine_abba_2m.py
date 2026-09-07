#!/usr/bin/env python3
"""ABBA confirmation combiner at 2M -- f150/f52/f52/f150, step 1568 t (1M is f150/f100, 800 t).

Applies the 2M overrides (analyze_q3_2m._apply_2m_overrides sets Q3.Q3_ABBA_ARM_SEQ +
Q3_ABBA_EXPECT_STEP + the calibration constants) BEFORE importing combine_abba, so its
module-level ARM_SEQ / EXPECT_STEP capture the 2M values. combine_abba is otherwise
PHY-agnostic: it derives the reduced arm from ARM_SEQ and re-analyzes each cell with
q3_steady_analyze under the cell's own validated --calib.

Usage: combine_abba_2m.py --out abba-confirmation.json <A1_f150> <B1_f52> <B2_f52> <A2_f150>
"""
import os
import analyze_q3_2m as Q2M
Q2M._apply_2m_overrides()      # MUST precede the combine_abba import (it binds ARM_SEQ at import)
import combine_abba as C

# route the firmware re-verify + lineage to the 2M asserter + analyzer (the 2M runner records
# analyze_q3_2m.py's sha under the 'analyze_q3.py' key, and the 2M asserter under 'assert_fsu_config.py')
C.ASSERT_FSU = os.path.join(C.ROOT, 'zephyr-patches', 'fsu-m0-series', 'assert_fsu_config_2m.py')
C.ANALYZE_Q3 = os.path.join(C.HERE, 'analyze_q3_2m.py')

C.main()
