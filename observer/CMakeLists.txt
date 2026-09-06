cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(pca10040_radio_observer)
target_sources(app PRIVATE src/main.c)
# Build the Q1 variant (dedicated AA, whitening off, 10s window) with: -- -DQ1=1
if(Q1)
  target_compile_definitions(app PRIVATE Q1_MODE=1)
endif()
# 2M variant (must match the generator): -- -DQ1=1 -DPHY2M=1
if(PHY2M)
  target_compile_definitions(app PRIVATE Q1_PHY_2M=1)
endif()
# Q2: real connection. ALL of AA/CRCINIT/CHAN are MANDATORY (never silently
# fall back to synthetic Q1 defaults). -- -DQ2=1 -DAA=0x.. -DCRCINIT=0x.. -DCHAN=N [-DPHY2M=1]
if(Q2)
  # Q2 observer is RUNTIME-configured (AA/CRCInit/channel/PHY sent over UART as a
  # `CFG ...` line -> flash ONCE, tune per connection). Optional -DAA/-DCRCINIT/
  # -DCHAN only set fallback defaults.
  target_compile_definitions(app PRIVATE Q2_MODE=1)
  if(DEFINED AA)
    target_compile_definitions(app PRIVATE Q2_AA=${AA})
  endif()
  if(DEFINED CRCINIT)
    target_compile_definitions(app PRIVATE Q2_CRCINIT=${CRCINIT})
  endif()
  if(DEFINED CHAN)
    target_compile_definitions(app PRIVATE Q2_CHAN=${CHAN})
  endif()
endif()
