cmake_minimum_required(VERSION 3.22)

project(ra8d1_eit C CXX ASM)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)
set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(TARGET_NAME ra8d1_eit)
set(PYOCD_TARGET "r7fa8d1bh" CACHE STRING "pyOCD target name")
set(PYOCD_PROBE_UID "0F7A117605A6" CACHE STRING "pyOCD probe unique ID")
set(FLASH_HEX ${CMAKE_CURRENT_BINARY_DIR}/${TARGET_NAME}.flash.hex)

find_program(PYOCD_COMMAND
    NAMES pyocd
    HINTS "$ENV{HOME}/.local/bin"
)

set(MCU_FLAGS
    -mthumb
    -mfloat-abi=hard
    -mcpu=cortex-m85+nopacbti
)

# Collect D/AVE 2D GPU sources
file(GLOB DAVE2D_SOURCES ra/tes/dave2d/src/*.c)

set(PROJECT_SOURCES
    ra/fsp/src/bsp/cmsis/Device/RENESAS/Source/startup.c
    ra/fsp/src/bsp/cmsis/Device/RENESAS/Source/system.c
    ra/fsp/src/bsp/mcu/all/bsp_clocks.c
    ra/fsp/src/bsp/mcu/all/bsp_common.c
    ra/fsp/src/bsp/mcu/all/bsp_delay.c
    ra/fsp/src/bsp/mcu/all/bsp_group_irq.c
    ra/fsp/src/bsp/mcu/all/bsp_guard.c
    ra/fsp/src/bsp/mcu/all/bsp_io.c
    ra/fsp/src/bsp/mcu/all/bsp_ipc.c
    ra/fsp/src/bsp/mcu/all/bsp_irq.c
    ra/fsp/src/bsp/mcu/all/bsp_macl.c
    ra/fsp/src/bsp/mcu/all/bsp_ospi_b.c
    ra/fsp/src/bsp/mcu/all/bsp_register_protection.c
    ra/fsp/src/bsp/mcu/all/bsp_sbrk.c
    ra/fsp/src/bsp/mcu/all/bsp_sdram.c
    ra/fsp/src/bsp/mcu/all/bsp_security.c
    ra/fsp/src/bsp/mcu/ra8d1/bsp_linker.c
    ra/fsp/src/r_ioport/r_ioport.c
    ra/fsp/src/r_dmac/r_dmac.c
    ra/fsp/src/r_gpt/r_gpt.c
    ra/fsp/src/r_sci_b_spi/r_sci_b_spi.c
    ra/fsp/src/r_sci_b_i2c/r_sci_b_i2c.c
    ra/fsp/src/r_sci_b_uart/r_sci_b_uart.c
    # LCD/Display FSP drivers (from SDK)
    ra/fsp/src/r_glcdc/r_glcdc.c
    ra/fsp/src/r_mipi_dsi/r_mipi_dsi.c
    ra/fsp/src/r_mipi_phy/r_mipi_phy.c
    ra/fsp/src/r_drw/r_drw_base.c
    ra/fsp/src/r_drw/r_drw_irq.c
    ra/fsp/src/r_drw/r_drw_memory.c
    ${DAVE2D_SOURCES}
    # Generated FSP configuration
    ra_gen/common_data.c
    ra_gen/hal_data.c
    ra_gen/main.c
    ra_gen/pin_data.c
    ra_gen/vector_data.c
    # EIT application
    src/eit_board.c
    src/eit_recon.c
    src/eit_recon_model.c
    src/hal_entry.c
    src/hal_warmstart.c
    # LCD subsystem (bare-metal, ported from RT-Thread SDK)
    src/eit_sdram.c
    src/eit_lcd.c
    src/eit_lcd_panel_mipi2.c
    src/eit_ui.c
    src/eit_gesture_icons.c
)

add_executable(${TARGET_NAME}.elf ${PROJECT_SOURCES})

target_link_libraries(${TARGET_NAME}.elf PRIVATE m)

target_include_directories(${TARGET_NAME}.elf PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_BINARY_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/ra_gen
    ${CMAKE_CURRENT_SOURCE_DIR}/ra_cfg/fsp_cfg/bsp
    ${CMAKE_CURRENT_SOURCE_DIR}/ra_cfg/fsp_cfg
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/fsp/inc
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/fsp/inc/api
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/fsp/inc/instances
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/arm/CMSIS_6/CMSIS/Core/Include
    # D/AVE 2D GPU headers
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/tes/dave2d/inc
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/tes/dave2d/src
    # DRW FSP driver headers
    ${CMAKE_CURRENT_SOURCE_DIR}/ra/fsp/src/r_drw
)

target_compile_definitions(${TARGET_NAME}.elf PRIVATE
    _RENESAS_RA_
    _RA_CORE=CM85
    _RA_ORDINAL=1
)

target_compile_options(${TARGET_NAME}.elf PRIVATE
    ${MCU_FLAGS}
    -fmessage-length=0
    -fsigned-char
    -ffunction-sections
    -fdata-sections
    -fno-strict-aliasing
    -Wunused
    -Wuninitialized
    -Wall
    -Wextra
    -Wmissing-declarations
    -Wconversion
    -Wpointer-arith
    -Wshadow
    -Wlogical-op
    -Waggregate-return
    -Wfloat-equal
    -Wno-stringop-overflow
    -Wno-format-truncation
    -flax-vector-conversions
    --param=min-pagesize=0
    $<$<CONFIG:Debug>:-Og -g3>
    $<$<CONFIG:Release>:-O2 -g>
)

configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/script/fsp_gen.ld
    ${CMAKE_CURRENT_BINARY_DIR}/fsp_gen.ld
    COPYONLY
)
configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/script/memory_regions.ld
    ${CMAKE_CURRENT_BINARY_DIR}/memory_regions.ld
    COPYONLY
)
configure_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/script/bsp_linker_info.h
    ${CMAKE_CURRENT_BINARY_DIR}/bsp_linker_info.h
    COPYONLY
)

target_link_options(${TARGET_NAME}.elf PRIVATE
    ${MCU_FLAGS}
    -T${CMAKE_CURRENT_SOURCE_DIR}/script/fsp.ld
    -Wl,--gc-sections
    -Wl,-Map,${CMAKE_CURRENT_BINARY_DIR}/${TARGET_NAME}.map
    -L${CMAKE_CURRENT_BINARY_DIR}
    -L${CMAKE_CURRENT_SOURCE_DIR}/script
    --specs=nano.specs
)

add_custom_command(TARGET ${TARGET_NAME}.elf POST_BUILD
    COMMAND ${CMAKE_OBJCOPY} -O ihex
            $<TARGET_FILE:${TARGET_NAME}.elf>
            ${FLASH_HEX}
    COMMAND ${CMAKE_OBJCOPY} -O srec
            $<TARGET_FILE:${TARGET_NAME}.elf>
            ${CMAKE_CURRENT_BINARY_DIR}/${TARGET_NAME}.srec
    COMMAND ${CMAKE_OBJCOPY} -O binary -w
            --only-section=__flash_*
            --only-section=__ram_from_flash*
            $<TARGET_FILE:${TARGET_NAME}.elf>
            ${CMAKE_CURRENT_BINARY_DIR}/${TARGET_NAME}.bin
    COMMAND ${CMAKE_SIZE} --format=berkeley
            $<TARGET_FILE:${TARGET_NAME}.elf>
    BYPRODUCTS
            ${FLASH_HEX}
            ${CMAKE_CURRENT_BINARY_DIR}/${TARGET_NAME}.srec
            ${CMAKE_CURRENT_BINARY_DIR}/${TARGET_NAME}.bin
    COMMENT "Generating HEX/SREC/BIN and printing image size"
)

if(PYOCD_COMMAND)
    add_custom_target(flash
        COMMAND ${PYOCD_COMMAND} load
                --target ${PYOCD_TARGET}
                --uid ${PYOCD_PROBE_UID}
                --format hex
                ${FLASH_HEX}
        DEPENDS ${TARGET_NAME}.elf
        USES_TERMINAL
    )

    add_custom_target(reset
        COMMAND ${PYOCD_COMMAND} reset
                --target ${PYOCD_TARGET}
                --uid ${PYOCD_PROBE_UID}
        USES_TERMINAL
    )
else()
    message(WARNING "pyOCD was not found. Build works, but flash/reset targets are disabled.")
endif()
