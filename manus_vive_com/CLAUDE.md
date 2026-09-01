# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Last updated: 2026-04-10** - Added socket communication features, Python testing tools, and tracker data integration.

## Project Overview

This is the **MANUS Core SDK** - a C++ SDK for interfacing with MANUS VR gloves and motion capture systems. The SDK provides:

- A core C API library (`libManusSDK.so`/`ManusSDK.dll`)
- Two example client applications demonstrating SDK usage
- Cross-platform support for Linux and Windows
- Real-time hand tracking and data streaming capabilities

The SDK is designed for developers building applications that integrate with MANUS Core software and gloves.

## Development Commands

### Linux Build Commands

```bash
# Navigate to the client directory
cd SDKClient_Linux

# Build the full-featured client
make          # Debug build (default)
make debug    # Debug build with -DDEBUG flag
make release  # Release build (still includes -g for debugging)

# Build the minimal client
cd ../SDKMinimalClient_Linux
make

# Clean build artifacts
make clean

# Run the built executables
./SDKClient_Linux.out
./SDKMinimalClient_Linux.out

# Show build information
make info
```

### Windows Build Commands

```bash
# Open the Visual Studio solution
start SDKClient.sln

# Build from command line (requires MSBuild)
msbuild SDKClient.sln /p:Configuration=Debug /p:Platform=x64
msbuild SDKClient.sln /p:Configuration=Release /p:Platform=x64
```

Within Visual Studio:
1. Open `SDKClient.sln`
2. Select configuration (Debug/Release) and platform (x64)
3. Build solution (F7 or Build → Build Solution)

## Architecture Overview

### Core Components

1. **ManusSDK C Library** (`ManusSDK/include/`, `ManusSDK/lib/`)
   - Core C API for glove communication
   - Platform-specific implementations (Linux .so, Windows .dll)
   - Thread-safe design for real-time data streaming

2. **SDKClient** (Full-featured example)
   - Demonstrates all major SDK features
   - Interactive console interface with ncurses
   - Complete hand tracking, ergonomics, and calibration workflows

3. **SDKMinimalClient** (Minimal example)
   - Basic demonstration of core functionality
   - Simplified code structure for quick integration
   - Focuses on essential hand tracking data

4. **Platform Abstraction Layer**
   - `ClientPlatformSpecific` classes handle OS-specific functionality
   - Separate implementations for Linux and Windows
   - Console I/O, window management, and input handling

### Key API Functions (from `ManusSDK.h`)

```c
// Initialization
SDKReturnCode CoreSdk_Initialize(SessionType p_TypeOfSession, bool p_Remote);
SDKReturnCode CoreSdk_ShutDown();

// Connection management
SDKReturnCode CoreSdk_LookForHosts(uint32_t p_WaitSeconds, bool p_LoopbackOnly);
SDKReturnCode CoreSdk_GetNumberOfAvailableHostsFound(uint32_t* p_NumberOfAvailableHostsFound);

// Data streaming
SDKReturnCode CoreSdk_RegisterGloveDataCallback(ManusGloveDataCallback p_Callback, void* p_Context);
SDKReturnCode CoreSdk_RegisterSkeletonDataCallback(ManusSkeletonDataCallback p_Callback, void* p_Context);
```

## Project Structure

```
MANUS_Core_2.4.0.1_SDK/
├── SDKClient.sln                    # Visual Studio solution (Windows)
├── SDKClient_Linux/                 # Full-featured Linux client
│   ├── Makefile                    # Linux build configuration
│   ├── SDKClient.cpp/.hpp          # Main client implementation
│   ├── Main.cpp                    # Entry point
│   ├── ClientPlatformSpecific.cpp/.hpp  # Linux-specific implementation
│   ├── ClientLogging.hpp           # Logging utilities
│   ├── ManusSDK/                   # SDK library
│   │   ├── include/               # Header files
│   │   │   ├── ManusSDK.h         # Core API
│   │   │   ├── ManusSDKTypes.h    # Data structures
│   │   │   └── ManusSDKTypeInitializers.h
│   │   └── lib/                   # Precompiled libraries
│   │       ├── libManusSDK.so     # Linux shared library
│   │       └── libManusSDK_Integrated.so
│   └── .vscode/                   # VS Code configuration
├── SDKClient_Windows/              # Full-featured Windows client
├── SDKMinimalClient_Linux/         # Minimal Linux client
└── SDKMinimalClient_Windows/       # Minimal Windows client
```

## Dependencies & Setup

### Linux Requirements
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install g++ make libncurses-dev

# Build requirements:
# - g++ with C++17 support
# - make
# - ncurses library for console UI
# - pthread for threading
```

### Windows Requirements
- Visual Studio 2017 or later
- C++ development workload
- Windows SDK

### Compiler Requirements
- **C++17** standard required
- **g++** (Linux) or **MSVC** (Windows)
- **Thread support** for real-time data handling

## Cross-Platform Development

### Key Differences

1. **Build Systems**
   - Linux: Makefile-based (`g++`, `make`)
   - Windows: Visual Studio solution (`.sln`, `.vcxproj`)

2. **Libraries**
   - Linux: Shared objects (`.so`) with `-Wl,-rpath` for runtime linking
   - Windows: Dynamic-link libraries (`.dll`) with `__declspec(dllexport/import)`

3. **Platform-Specific Code**
   - Located in `ClientPlatformSpecific` classes
   - Handles console I/O, window management, and input
   - Separate `.cpp` implementations for each platform

### Building for Both Platforms

```bash
# Linux (from project root)
cd SDKClient_Linux && make

# Windows (from project root)
# Open SDKClient.sln in Visual Studio
# OR use msbuild from Developer Command Prompt
msbuild SDKClient.sln /p:Configuration=Debug /p:Platform=x64
```

## Troubleshooting

### Common Build Issues

**Linux: Library not found**
```bash
# Ensure the library path is set
export LD_LIBRARY_PATH=./ManusSDK/lib:$LD_LIBRARY_PATH
# OR use the rpath set in Makefile
```

**Linux: ncurses not found**
```bash
sudo apt-get install libncurses-dev  # Ubuntu/Debian
sudo yum install ncurses-devel       # RHEL/CentOS
```

**Windows: Missing SDK dependencies**
- Ensure Visual Studio C++ workload is installed
- Verify Windows SDK is available
- Check that `MANUS_SDK_EXPORTS` is defined when building the SDK

### Runtime Issues

**Connection failures**
- Verify MANUS Core software is running
- Check network connectivity for remote connections
- Ensure correct session type (local vs remote) in initialization

**Data streaming issues**
- Verify callback registration
- Check thread safety in application code
- Monitor system resource usage

## Documentation References

- **Online Documentation**: https://docs.manus-meta.com/
- **API Reference**: See `ManusSDK.h` for complete function documentation
- **Example Code**: Study `SDKClient.cpp` and `SDKMinimalClient.cpp` for usage patterns
- **Type Definitions**: Refer to `ManusSDKTypes.h` for data structures

## Development Notes

- The SDK uses a **C API** with **C++ wrapper examples**
- **Thread safety** is critical for real-time applications
- **Error handling** through `SDKReturnCode` enumeration
- **Memory management** follows RAII principles in C++ examples
- **Platform abstraction** allows for clean separation of OS-specific code

When modifying the SDK or creating new clients:
1. Follow the existing architecture patterns
2. Maintain cross-platform compatibility
3. Use the provided logging utilities (`ClientLogging.hpp`)
4. Test on both Linux and Windows when possible
5. Reference the existing example clients for implementation patterns

## Socket Communication & Python Testing Tools

### Enhanced Socket Client (SDKMinimalClient_socket.cpp)
A modified version of the minimal client that sends real-time skeleton and tracker data to Python via TCP socket:

**Key Features:**
- **Combined JSON output**: Sends unified frames with both `skeletons` and `trackers` data
- **TCP socket communication**: Connects to Python on `127.0.0.1:8888`
- **30fps data streaming**: Stable data flow with 33ms intervals
- **Debug logging**: Detailed console output for troubleshooting

**Recent Fixes (April 2026):**
1. **API version compatibility**: Updated field names to match SDK 2.4.0.1:
   - `tracker.transform.position` → `tracker.position`
   - `tracker.transform.rotation` → `tracker.rotation`
   - `tracker.deviceId` → `tracker.trackerId.id`
   - Removed `skeleton.info.handType` (not in RawSkeletonInfo)
2. **Data synchronization**: Combined skeleton and tracker data in single JSON messages
3. **Compilation fixes**: Resolved C++ stream operator errors and encoding issues

### Python Testing & Analysis Tools
Multiple Python scripts for testing and analyzing MANUS Core data:

**Core Tools:**
- `debug_demo.py` - **Primary testing tool** with interactive commands:
  - `r` = Rotation test (wrist rotation)
  - `t` = Translation test (linear movement)
  - `c` = Calibration suggestions
  - `e` = Export session data
  - `q` = Quit
- `diagnose_tracker.py` - Data flow diagnostics
- `full_calibration_demo.py` - Complete calibration workflow
- `translation_quality.py` - Linear movement analysis
- `rotation_quality.py` - Rotation quality verification

**Testing Workflow:**
1. **Start Python tool first**: `python debug_demo.py`
2. **Start C++ client second**: Run `SDKMinimalClient_Windows.exe`
3. **Wait for connection**: Python shows `[OK] 客户端已连接`
4. **Execute tests**: Use interactive commands (`r`, `t`, `c`)

### Data Format (JSON)
```json
{
  "timestamp": 1775758533125,
  "frame": 10,
  "skeletons": [
    {
      "gloveId": "2433b45d",
      "nodes": [
        {"id": 0, "position": [x,y,z], "rotation": [x,y,z,w]},
        ...
      ]
    }
  ],
  "trackers": [
    {
      "id": 0,
      "trackerId": "device_id",
      "position": [x,y,z],
      "rotation": [x,y,z,w],
      "quality": 0,
      "valid": true
    }
  ]
}
```

### Troubleshooting
**Common Issues & Solutions:**

1. **"No tracker data" in Python**
   - Check C++ client logs for `[TRACKER]` messages
   - Verify Vive Tracker is configured in MANUS Core
   - Ensure tracker callback is registered

2. **Mixed data ratio not 100%**
   - Old issue: Separate skeleton/tracker callbacks
   - Fixed: Combined data in `CombinedToJSON()`
   - Current: All frames contain both data types

3. **Connection drops after key press**
   - Fixed threading logic in `debug_demo.py`
   - Receiver thread now handles socket properly

4. **Compilation errors (C2039)**
   - API version mismatch between code and SDK headers
   - Use field names from `ManusSDKTypes.h` in SDK 2.4.0.1

### Performance Characteristics
- **Data rate**: 30fps (33ms intervals)
- **Latency**: 0-33ms (max one frame delay)
- **Network**: TCP socket, localhost only
- **Data size**: ~2-5KB per frame (compressed JSON)

### Integration Notes
- **Real-time visualization**: Use `realtime_visualization.py`
- **Auto-calibration**: `auto_calibration.py` provides axis-specific adjustments
- **Session recording**: All tools support JSON export for offline analysis
- **Multi-tracker support**: Currently tested with 2 Vive Trackers

**CLAUDE.md Update Policy:**
This file should be updated when significant changes are made to:
1. Build system or dependencies
2. API interfaces or data formats
3. Core testing workflows
4. Architecture patterns or best practices
Keep the file current to help future Claude Code sessions understand project context.
### File Structure (April 2026)

**Core Python Files (keep in main directory):**
- `diagnose_tracker.py` - Data flow diagnostics and validation
- `debug_demo.py` - Interactive testing tool (rotation/translation/calibration)
- `manus_data_receiver.py` - TCP socket receiver for MANUS Core data
- `rotation_quality.py` - Wrist rotation analysis module
- `translation_quality.py` - Linear movement analysis module
- `auto_calibration.py` - Automatic offset calibration suggestions

**Backup Files (moved to `backup/` folder):**
- `full_calibration_demo.py`, `offset_validation_demo.py`, `realtime_visualization.py`
- `session_alignment.py`, `simple_calibration.py`, `simple_demo.py`
- `steamvr_try.py`, `test_all_modules.py`, `test_connection.py`, `try.py`

**Usage:** Only the 6 core files are needed for normal operation. Backup files are preserved for reference but not required.

**Data Output Folders:**
- `calibration_results/` - JSON calibration data files generated by `debug_demo.py`
- `backup/` - Redundant Python files moved for reference

**File Naming Convention:**
- Session files: `manus_calibration_session_{timestamp}.json` (exported manually)
- Final files: `manus_calibration_final_{timestamp}.json` (exported automatically on exit)
- {timestamp} format: YYYYMMDD_HHMMSS (e.g., 20260410_003838)
