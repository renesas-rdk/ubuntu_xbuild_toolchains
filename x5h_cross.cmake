# system information
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_CROSSCOMPILING TRUE)

# skip compilers tests
set(CMAKE_C_COMPILER_WORKS 1)
set(CMAKE_CXX_COMPILER_WORKS 1)
set(CMAKE_TRY_COMPILE_TARGET_TYPE "STATIC_LIBRARY")

# ARM64 sysroot settings
set(CMAKE_SYSROOT $ENV{ARM64_SYSROOT})
set(CMAKE_FIND_ROOT_PATH $ENV{ARM64_SYSROOT})
set(CMAKE_LIBRARY_ARCHITECTURE aarch64-linux-gnu)

# compilers settings
set(CMAKE_C_COMPILER "/usr/bin/aarch64-linux-gnu-gcc")
set(CMAKE_CXX_COMPILER "/usr/bin/aarch64-linux-gnu-g++")

# compilers flags
set(ARM_COMPILE_OPTION "-march=armv8-a -mcpu=cortex-a72 -fstack-protector-strong -ffunction-sections -fdata-sections -Wformat -Wformat-security -Werror=format-security -Wall -Wextra -Wconversion -Wunused -Wuninitialized -Werror -pedantic-errors")
if(NOT CMAKE_CXX_FLAGS MATCHES "-mcpu=cortex-a72")
  set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} ${ARM_COMPILE_OPTION}")
  set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${ARM_COMPILE_OPTION}")
endif()

# Don't look for programs in the sysroot (these are ARM programs, they won't run
# on the build machine).
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)

# Only look for libraries, headers and packages in the sysroot, don't look on
# the build machine.
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)

set(THREADS_PTHREAD_ARG "0" CACHE STRING "Result from TRY_RUN" FORCE)

set(CMAKE_THREAD_LIBS_INIT "-lpthread")
set(CMAKE_HAVE_THREADS_LIBRARY 1)
set(CMAKE_USE_WIN32_THREADS_INIT 0)
set(CMAKE_USE_PTHREADS_INIT 1)
set(THREADS_PREFER_PTHREAD_FLAG ON)

set(ENABLE_PRECOMPILED_HEADERS OFF)

set(CPACK_DEBIAN_PACKAGE_ARCHITECTURE arm64)

# Pre-set host tools for cross-compilation
set(GIT_EXECUTABLE "/usr/bin/git" CACHE FILEPATH "Git executable on host")
set(CMAKE_MAKE_PROGRAM "/usr/bin/make" CACHE FILEPATH "Make executable on host")
set(PKG_CONFIG_EXECUTABLE "/usr/bin/pkg-config" CACHE FILEPATH "pkg-config executable on host")

# W/A: Auto-detect and add library directories to rpath-link for cross-compilation
if(CMAKE_SYSROOT)
    execute_process(
        COMMAND find ${CMAKE_SYSROOT}/usr/lib/${CMAKE_LIBRARY_ARCHITECTURE} -maxdepth 2 -name "lib*.so*" -type f
        COMMAND xargs dirname
        COMMAND sort -u
        OUTPUT_VARIABLE LIB_DIRECTORIES
        OUTPUT_STRIP_TRAILING_WHITESPACE
    )

    string(REPLACE "\n" ";" LIB_DIR_LIST ${LIB_DIRECTORIES})

    if(NOT CMAKE_EXE_LINKER_FLAGS MATCHES "(^| )-Wl,--gc-sections( |$)")
        set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -Wl,--gc-sections")
    endif()

    if(NOT CMAKE_SHARED_LINKER_FLAGS MATCHES "(^| )-Wl,--gc-sections( |$)")
        set(CMAKE_SHARED_LINKER_FLAGS "${CMAKE_SHARED_LINKER_FLAGS} -Wl,--gc-sections")
    endif()

    foreach(LIB_DIR ${LIB_DIR_LIST})
        set(flag "-Wl,-rpath-link,${LIB_DIR}")

        if(NOT CMAKE_EXE_LINKER_FLAGS MATCHES "(^| )${flag}( |$)")
            set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} ${flag}")
        endif()

        if(NOT CMAKE_SHARED_LINKER_FLAGS MATCHES "(^| )${flag}( |$)")
            set(CMAKE_SHARED_LINKER_FLAGS "${CMAKE_SHARED_LINKER_FLAGS} ${flag}")
        endif()
    endforeach()
endif()