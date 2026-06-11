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
# Path uses CMAKE_CURRENT_LIST_DIR (not CMAKE_SOURCE_DIR) — in IDF's component-
# requirements pre-pass CMAKE_SOURCE_DIR resolves to the build dir, so the
# include fails. CMAKE_CURRENT_LIST_DIR is this file's dir, stable in both passes.
include("${CMAKE_CURRENT_LIST_DIR}/../staging/main_requires.cmake")

# Generated entry point. Guard on EXISTS so a raw idf.py invocation without
# staging still configures (the spangap build path always writes it before
# idf.py; a raw idf.py build would then simply have no app_main to link).
set(_INIT_DISPATCH "${CMAKE_CURRENT_LIST_DIR}/../staging/spangap_init_dispatch.gen.cpp")
if(NOT EXISTS "${_INIT_DISPATCH}")
    set(_INIT_DISPATCH "")
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
        driver
        esp_event
        esp_netif
        esp_wifi
        lwip
)
