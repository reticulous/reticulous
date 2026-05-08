idf_component_register(
    SRCS
        "app_build_epoch.c"
        "main.cpp"
        "fs.cpp"
        "pm.cpp"
        "log.cpp"
        "cli.cpp"
        "cli_cmd_fs.cpp"
        "cli_cmd_sys.cpp"
        "storage.cpp"
        "cron.cpp"
        "audio.cpp"
        "detect.cpp"
        "camera.cpp"
        "net.cpp"
        "wg.cpp"
        "ntp.cpp"
        "rtsp.cpp"
        "web.cpp"
        "tls.cpp"
        "rec_task.cpp"
        "play.cpp"
        "its.cpp"
        "webrtc_task.cpp"
        "webrtc_sctp.cpp"
        "live.cpp"
        "upnp.cpp"
        "duckdns.cpp"
        "acme.cpp"
        "sc_mdns.cpp"
        "auth.cpp"
        "frameslot.cpp"
        "ota.cpp"
        "heap_track_stub.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        esp_driver_gpio
        esp_driver_i2s
        esp_pm
        esp_driver_usb_serial_jtag
        esp_wifi
        esp_netif
        esp_event
        nvs_flash
        json
        esp_timer
        esp_psram
        fatfs
        sdmmc
        joltwallet__littlefs
        mbedtls
        esp_rom
        espressif__esp_jpeg
        cam_driver
        espressif__esp32-camera
        espressif__mdns
        esp_app_format
        mbedtls
        esp_http_client
        app_update
        esp_wireguard
)

# IDF 5.5 heap-task-tracking eager-bookkeeping bypass — see
# docs/idf-tweaks.md and main/heap_track_stub.c.  We --wrap three IDF
# heap-tracking entry points to no-ops; if Espressif renames or splits
# them, the --wrap silently reverts to the broken behavior, so verify
# their names still appear at the expected call sites and fail loud
# otherwise.
set(_HEAP_TRACK_FNS
    heap_caps_update_per_task_info_alloc
    heap_caps_update_per_task_info_free
    heap_caps_update_per_task_info_realloc)
set(_HEAP_TRACK_FILES
    "${IDF_PATH}/components/heap/heap_caps_base.c"
    "${IDF_PATH}/components/heap/heap_caps_init.c")
set(_HEAP_TRACK_OK TRUE)
foreach(_f IN LISTS _HEAP_TRACK_FILES)
    if(NOT EXISTS "${_f}")
        set(_HEAP_TRACK_OK FALSE)
        set(_HEAP_TRACK_REASON "missing IDF source ${_f}")
        break()
    endif()
    file(READ "${_f}" _src)
    foreach(_fn IN LISTS _HEAP_TRACK_FNS)
        string(FIND "${_src}" "${_fn}(" _idx)
        if(_idx EQUAL -1)
            # only need each name in at least one of the files
        endif()
    endforeach()
endforeach()
# Each function must appear at least once across the listed files.
if(_HEAP_TRACK_OK)
    foreach(_fn IN LISTS _HEAP_TRACK_FNS)
        set(_found FALSE)
        foreach(_f IN LISTS _HEAP_TRACK_FILES)
            file(READ "${_f}" _src)
            string(FIND "${_src}" "${_fn}(" _idx)
            if(NOT _idx EQUAL -1)
                set(_found TRUE)
                break()
            endif()
        endforeach()
        if(NOT _found)
            set(_HEAP_TRACK_OK FALSE)
            set(_HEAP_TRACK_REASON "expected function '${_fn}' not found in IDF heap component")
            break()
        endif()
    endforeach()
endif()
if(NOT _HEAP_TRACK_OK)
    message(FATAL_ERROR
        "seccam: heap-task-tracking --wrap guard failed — ${_HEAP_TRACK_REASON}.\n"
        "ESP-IDF appears to have renamed or restructured the heap tracking call sites.\n"
        "The --wrap intercept in main/CMakeLists.txt + main/heap_track_stub.c will silently\n"
        "no longer take effect, restoring the IWDT crash documented in docs/idf-tweaks.md.\n"
        "Either:\n"
        "  - update _HEAP_TRACK_FNS / heap_track_stub.c to match the new IDF symbols, or\n"
        "  - set CONFIG_HEAP_TASK_TRACKING=n in sdkconfig.defaults to drop the feature\n"
        "    entirely (loses per-task DRAM/PSRAM columns in the 'top' CLI command and\n"
        "    makes heapDump() in pm.cpp report no per-task attribution).")
endif()
target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=heap_caps_update_per_task_info_alloc"
    "-Wl,--wrap=heap_caps_update_per_task_info_free"
    "-Wl,--wrap=heap_caps_update_per_task_info_realloc")

# cam_driver vendors driver/cam_hal.c — exclude duplicate from managed esp32-camera
if(TARGET __idf_espressif__esp32-camera)
    get_target_property(_seccam_cam_srcs __idf_espressif__esp32-camera SOURCES)
    set(_seccam_cam_new "")
    foreach(_seccam_f IN LISTS _seccam_cam_srcs)
        get_filename_component(_seccam_n "${_seccam_f}" NAME)
        if(_seccam_n STREQUAL "cam_hal.c")
            continue()
        endif()
        list(APPEND _seccam_cam_new "${_seccam_f}")
    endforeach()
    set_target_properties(__idf_espressif__esp32-camera PROPERTIES SOURCES "${_seccam_cam_new}")
endif()

