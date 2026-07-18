cmake_minimum_required(VERSION 3.16)

# Pick up the spangap straddles staged by `spangap make` into
# staging/components/<short>/ — short, fork-friendly component names. Set
# BEFORE the bootstrap include so the project's component layout is known
# by the time ESP-IDF processes it. Guarded by EXISTS so a raw `idf.py`
# invocation without the CLI doesn't error on a missing dir.
if(EXISTS "${CMAKE_SOURCE_DIR}/staging/components")
    list(APPEND EXTRA_COMPONENT_DIRS "${CMAKE_SOURCE_DIR}/staging/components")
    # A straddle is one ESP-IDF component, but it may bundle vendored sub-
    # components in its own `esp-idf/components/<x>/` subdir. The canonical
    # case is rns which vendors the µReticulum hard fork +
    # bzip2 as sub-components. EXTRA_COMPONENT_DIRS doesn't recurse, so
    # add each present `staging/components/<straddle>/components/` to the
    # search path explicitly.
    file(GLOB _nested_dirs "${CMAKE_SOURCE_DIR}/staging/components/*/components")
    foreach(_d IN LISTS _nested_dirs)
        if(IS_DIRECTORY "${_d}")
            list(APPEND EXTRA_COMPONENT_DIRS "${_d}")
        endif()
    endforeach()
endif()

# spangap-core's bootstrap runs pre-project (before ESP-IDF starts component
# discovery), so the consumer points at the file directly. The canonical path
# is staging/components/spangap-core/ (populated by `spangap make` before
# invoking idf.py), with fallbacks for managed_components/ (registry pickup
# once published) and the workspace-sibling layout (direct idf.py invocation
# in a fully-checked-out workspace, no CLI in the loop).
if(EXISTS "${CMAKE_SOURCE_DIR}/staging/components/spangap-core/cmake/bootstrap.cmake")
    include("${CMAKE_SOURCE_DIR}/staging/components/spangap-core/cmake/bootstrap.cmake")
elseif(EXISTS "${CMAKE_SOURCE_DIR}/managed_components/spangap__spangap-core/cmake/bootstrap.cmake")
    include("${CMAKE_SOURCE_DIR}/managed_components/spangap__spangap-core/cmake/bootstrap.cmake")
elseif(EXISTS "${CMAKE_SOURCE_DIR}/../../spangap-core/esp-idf/cmake/bootstrap.cmake")
    include("${CMAKE_SOURCE_DIR}/../../spangap-core/esp-idf/cmake/bootstrap.cmake")
else()
    message(FATAL_ERROR
        "spangap-core bootstrap.cmake not found. Looked in:\n"
        "  ${CMAKE_SOURCE_DIR}/staging/components/spangap-core/cmake/bootstrap.cmake\n"
        "  ${CMAKE_SOURCE_DIR}/managed_components/spangap__spangap-core/cmake/bootstrap.cmake\n"
        "  ${CMAKE_SOURCE_DIR}/../../spangap-core/esp-idf/cmake/bootstrap.cmake\n"
        "Run `spangap make` from this dir to populate staging/, or check that "
        "the spangap-core straddle is checked out at ../../spangap-core.")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(reticulous)

# Browser SPA and LCD icon assets live alongside the esp-idf/ project dir
# (one level up — the straddle layout has them as siblings of esp-idf/).
# spangap_create_factory_image() / spangap_lcd_icons() default their PARTITION
# argument to ${SPANGAP_FIXED_PARTITION} (set by bootstrap.cmake to fixed_a
# when OTA is on, fixed when OTA is off).
spangap_browser_build("${CMAKE_SOURCE_DIR}/../web-interface")
# Tracked /fixed files (the on-device manual, webroot/WELCOME.md — it overrides
# the viewer straddle's generic welcome page). They can't live in ../data:
# deploy.sh wipes and regenerates ../data/webroot wholesale, so it's gitignored.
# Registered here, after project(), this dir merges after every staged
# straddle's own SPANGAP_EXTRA_DATA_DIRS entry and therefore wins collisions.
set_property(GLOBAL APPEND PROPERTY SPANGAP_EXTRA_DATA_DIRS
            "${CMAKE_SOURCE_DIR}/data")
spangap_create_factory_image(DATA_DIR "${CMAKE_SOURCE_DIR}/../data")
# spangap_lcd_icons() lives in spangap-lcd's project_include.cmake, so it
# only exists when that straddle is staged. A --no-lcd build skips the
# rasterizer entirely (there are no LCD icons to ship without the launcher).
# No SRC_DIR: each straddle ships its own launcher icons in its
# esp-idf/assets/lcd-icons/, collected automatically from every staged component.
if(COMMAND spangap_lcd_icons)
    spangap_lcd_icons()
endif()
# Vector fonts (FreeType / tiny_ttf engines): subset spangap-lcd's shipped faces
# into /fixed/fonts. Same gating as icons — only exists when spangap-lcd is staged.
if(COMMAND spangap_lcd_fonts)
    spangap_lcd_fonts()
endif()
