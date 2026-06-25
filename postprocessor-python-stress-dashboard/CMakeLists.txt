set(PROCESSOR_NAME "postprocessor-python-stress-dashboard")

# NOTE: This postprocessor uses the bundled pure-Python nxai_communication_utils.py
# (length-prefixed msgpack over a Unix socket) and therefore does NOT depend on
# the libnxai-c-utilities-shared.so C library. For fast iteration you can skip
# the build entirely and deploy the raw .py files with deploy.sh.

find_package(Python REQUIRED)
execute_process(
    COMMAND "${Python_EXECUTABLE}" -m venv "${CMAKE_BINARY_DIR}/venv"
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

find_program(PIP_EXECUTABLE pip HINTS ${VENV_EXEC_DIR})
find_program(PYTHON_VENV_EXECUTABLE python HINTS ${VENV_EXEC_DIR})
if(NOT PIP_EXECUTABLE OR NOT PYTHON_VENV_EXECUTABLE)
    message(FATAL_ERROR "Could not find pip or python inside the virtual environment.")
endif()

# Fallback install destination if not provided by a parent SDK CMakeLists.
if(NOT DEFINED INSTALL_DEST_POSTPROCESSORS)
    set(INSTALL_DEST_POSTPROCESSORS "postprocessors")
endif()

add_custom_target(${PROCESSOR_NAME} ALL
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    COMMAND ${PIP_EXECUTABLE} install nuitka
    COMMAND ${PIP_EXECUTABLE} install -r requirements.txt
    COMMAND ${PYTHON_VENV_EXECUTABLE} -m nuitka ${CMAKE_CURRENT_SOURCE_DIR}/${PROCESSOR_NAME}.py
            --onefile --output-filename=${PROCESSOR_NAME}
            --output-dir=${CMAKE_CURRENT_BINARY_DIR}
            --include-module=nxai_communication_utils
            --onefile-tempdir-spec={TEMP}/onefile_${PROCESSOR_NAME}
)

if(WIN32)
    set(EXECUTABLE_SUFFIX ".exe")
endif()

# The compiled postprocessor binary.
install(
    PROGRAMS ${CMAKE_CURRENT_BINARY_DIR}/${PROCESSOR_NAME}${EXECUTABLE_SUFFIX}
    DESTINATION ${INSTALL_DEST_POSTPROCESSORS}
    COMPONENT ${PROCESSOR_NAME}
)

# Web app + its modules are run separately with plain Python (not compiled).
install(
    FILES
        ${CMAKE_CURRENT_SOURCE_DIR}/web_app.py
        ${CMAKE_CURRENT_SOURCE_DIR}/metrics.py
        ${CMAKE_CURRENT_SOURCE_DIR}/ui_templates.py
    DESTINATION ${INSTALL_DEST_POSTPROCESSORS}
    COMPONENT ${PROCESSOR_NAME}
)
