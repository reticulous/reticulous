# The reticulous app's main component. It ships NO application sources of its
# own — the entire entry point is the build-generated app_main() + the start/
# init dispatchers (spangapStartStraddles / spangapInitStraddles), written into
# staging/ by spangap-inside from every staged straddle's start:/init: hooks.
#
# Board bring-up (tdeckStart / gpsInit / heltecv4Start …) is contributed by the
# board straddle added with `--with`. Its symbols link because a buildable's
# main REQUIRES the FULL staged set — including --with'd straddles it never
# names — see spangap-inside write_main_requires().
#
# Staging lives in the build dir. BUILD_DIR is read as a build property: it is
# the one spelling that holds in IDF's component-requirements pre-pass (a
# script-mode run, where CMAKE_SOURCE_DIR and CMAKE_BINARY_DIR mean nothing
# useful) as well as in the real configure.
idf_build_get_property(_build_dir BUILD_DIR)
include("${_build_dir}/staging/main_requires.cmake")

# Generated entry point. Guard on EXISTS so a raw idf.py invocation without
# staging still configures (the spangap build path always writes it before
# idf.py; a raw idf.py build would then simply have no app_main to link).
set(_INIT_DISPATCH "${_build_dir}/staging/spangap_init_dispatch.gen.cpp")
if(NOT EXISTS "${_INIT_DISPATCH}")
    set(_INIT_DISPATCH "")
endif()

# The radio/WiFi/lwIP components do not register on the linux host target, and
# a REQUIRES on an unregistered component is fatal. esp_event registers
# everywhere; the host build gets its `driver` from the board straddle.
set(_IDF_REQ esp_event)
if(NOT IDF_TARGET STREQUAL "linux")
    list(APPEND _IDF_REQ driver esp_netif esp_wifi lwip)
endif()

idf_component_register(
    SRCS
        ${_INIT_DISPATCH}
        # App-level conditional/<name>/ code (none today), compiled only when
        # <name> is staged.
        ${SPANGAP_CONDITIONAL_SRCS}
    INCLUDE_DIRS "." ${SPANGAP_CONDITIONAL_INCLUDE_DIRS}
    REQUIRES
        ${SPANGAP_REQUIRES}
        # ESP-IDF / managed components stay static — not gateable straddles.
        ${_IDF_REQ}
)
