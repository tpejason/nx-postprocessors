set(PROCESSOR_NAME "postprocessor-python-gauge-dashboard")

find_program(PYTHON310_EXECUTABLE python3.10)
if(NOT PYTHON310_EXECUTABLE)
    find_package(Python REQUIRED)
    set(PYTHON310_EXECUTABLE ${Python_EXECUTABLE})
endif()
execute_process(
    COMMAND "${PYTHON310_EXECUTABLE}" -m venv "${CMAKE_BINARY_DIR}/venv"
    WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
    RESULT_VARIABLE VENV_RESULT
)
if(NOT VENV_RESULT EQUAL 0)
    message(FATAL_ERROR "Could not create Python virtual environment.")
endif()
if(WIN32)
    set(VENV_EXEC_DIR "${CMAKE_BINARY_DIR}/venv/Scripts")
else()
    set(VENV_EXEC_DIR "${CMAKE_BINARY_DIR}/venv/bin")
endif()

find_program(PIP_EXECUTABLE    pip    HINTS ${VENV_EXEC_DIR})
find_program(PYTHON_VENV_EXECUTABLE python HINTS ${VENV_EXEC_DIR})

if(NOT PIP_EXECUTABLE OR NOT PYTHON_VENV_EXECUTABLE)
    message(FATAL_ERROR "Could not find pip or python inside the virtual environment.")
endif()

message(STATUS "Using Pip from: ${PIP_EXECUTABLE}")
message(STATUS "Using Python from: ${PYTHON_VENV_EXECUTABLE}")

# ── Windows: automate Nuitka Dependency Walker installation ─────────────────────
if(WIN32)
    set(DEPWALKER_URL "https://dependencywalker.com/depends22_x64.zip")
    set(NUITKA_CACHE_DIR "$ENV{LOCALAPPDATA}/Nuitka/Nuitka/Cache/downloads")
    set(DEPWALKER_INSTALL_DIR "${NUITKA_CACHE_DIR}/depends/x86_64")
    set(DEPWALKER_ZIP_FILE    "${NUITKA_CACHE_DIR}/depends22_x64.zip")

    if(NOT EXISTS "${DEPWALKER_INSTALL_DIR}/depends.exe")
        message(STATUS "Dependency Walker for Nuitka not found. Automating installation...")
        file(MAKE_DIRECTORY "${NUITKA_CACHE_DIR}")
        message(STATUS "Downloading Dependency Walker from ${DEPWALKER_URL}")
        file(DOWNLOAD ${DEPWALKER_URL} ${DEPWALKER_ZIP_FILE} STATUS download_status SHOW_PROGRESS)
        list(GET download_status 0 status_code)
        if(NOT status_code EQUAL 0)
            list(GET download_status 1 error_message)
            message(FATAL_ERROR "Failed to download Dependency Walker: ${error_message}")
        endif()
        message(STATUS "Extracting to ${DEPWALKER_INSTALL_DIR}")
        file(MAKE_DIRECTORY "${DEPWALKER_INSTALL_DIR}")
        execute_process(
            COMMAND ${CMAKE_COMMAND} -E tar xf "${DEPWALKER_ZIP_FILE}"
            WORKING_DIRECTORY "${DEPWALKER_INSTALL_DIR}"
            RESULT_VARIABLE extract_result
        )
        if(NOT extract_result EQUAL 0)
            message(FATAL_ERROR "Failed to extract ${DEPWALKER_ZIP_FILE}")
        endif()
        file(REMOVE "${DEPWALKER_ZIP_FILE}")
        message(STATUS "Dependency Walker for Nuitka installed successfully.")
    else()
        message(STATUS "Dependency Walker for Nuitka already installed.")
    endif()
endif()

if(WIN32)
    set(EXECUTABLE_SUFFIX ".exe")
else()
    set(EXECUTABLE_SUFFIX "")
endif()

set(PROCESSOR_OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/${PROCESSOR_NAME}${EXECUTABLE_SUFFIX}")

add_custom_command(
    OUTPUT ${PROCESSOR_OUTPUT}
    DEPENDS
        ${CMAKE_CURRENT_SOURCE_DIR}/${PROCESSOR_NAME}.py
        ${CMAKE_CURRENT_SOURCE_DIR}/requirements.txt
        nxai-c-utilities-shared
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    COMMAND ${PIP_EXECUTABLE} install -q -r ../nxai-utilities/python-utilities/requirements.txt
    COMMAND ${PIP_EXECUTABLE} install -q -r requirements.txt
    COMMAND ${CMAKE_COMMAND} -E copy $<TARGET_FILE:nxai-c-utilities-shared> ${CMAKE_CURRENT_SOURCE_DIR}
    COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_CURRENT_SOURCE_DIR}/../nxai-utilities/python-utilities/nxai_communication_utils.py ${CMAKE_CURRENT_SOURCE_DIR}
    COMMAND ${CMAKE_COMMAND} -E env PYTHONPATH=${CMAKE_CURRENT_SOURCE_DIR} ${PYTHON_VENV_EXECUTABLE} -m nuitka ${CMAKE_CURRENT_SOURCE_DIR}/${PROCESSOR_NAME}.py --onefile --output-filename=${PROCESSOR_NAME} --output-dir=${CMAKE_CURRENT_BINARY_DIR} --static-libpython=no --python-flag=-m
    COMMAND ${CMAKE_COMMAND} -E remove ${CMAKE_CURRENT_SOURCE_DIR}/nxai_communication_utils.py
    COMMAND ${CMAKE_COMMAND} -E remove $<TARGET_FILE_NAME:nxai-c-utilities-shared>
    COMMENT "Building ${PROCESSOR_NAME}..."
)

add_custom_target(${PROCESSOR_NAME} ALL
    DEPENDS ${PROCESSOR_OUTPUT}
)

install(
    PROGRAMS ${PROCESSOR_OUTPUT}
    DESTINATION ${INSTALL_DEST_POSTPROCESSORS}
    COMPONENT ${PROCESSOR_NAME}
)

install(
    TARGETS nxai-c-utilities-shared
    DESTINATION ${INSTALL_DEST_POSTPROCESSORS}
    COMPONENT ${PROCESSOR_NAME}
)
