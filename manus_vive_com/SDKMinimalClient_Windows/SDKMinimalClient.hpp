#ifndef _SDK_MINIMAL_CLIENT_HPP_
#define _SDK_MINIMAL_CLIENT_HPP_


// Set up a Doxygen group.
/** @addtogroup SDKMinimalClient
 *  @{
 */


#include "ClientPlatformSpecific.hpp"
#include "ManusSDK.h"
#include <mutex>
#include <vector>
#include <string>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <memory>
#include <thread>

// Windows Socket headers
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#define SOCKET_TYPE SOCKET
#define INVALID_SOCKET_VAL INVALID_SOCKET
#define CLOSE_SOCKET closesocket
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <unistd.h>
#define SOCKET_TYPE int
#define INVALID_SOCKET_VAL -1
#define CLOSE_SOCKET close
#endif

 /// @brief The type of connection to core.
enum class ConnectionType : int
{
	ConnectionType_Invalid = 0,
	ConnectionType_Integrated,
	ConnectionType_Local,
	ConnectionType_Remote,
	ClientState_MAX_CLIENT_STATE_SIZE
};

/// @brief Values that can be returned by this application.
enum class ClientReturnCode : int
{
	ClientReturnCode_Success = 0,
	ClientReturnCode_FailedPlatformSpecificInitialization,
	ClientReturnCode_FailedToResizeWindow,
	ClientReturnCode_FailedToInitialize,
	ClientReturnCode_FailedToFindHosts,
	ClientReturnCode_FailedToConnect,
	ClientReturnCode_UnrecognizedStateEncountered,
	ClientReturnCode_FailedToShutDownSDK,
	ClientReturnCode_FailedPlatformSpecificShutdown,
	ClientReturnCode_FailedToRestart,
	ClientReturnCode_FailedWrongTimeToGetData,

	ClientReturnCode_MAX_CLIENT_RETURN_CODE_SIZE
};

/// @brief Used to store the information about the skeleton data coming from the estimation system in Core.
class ClientRawSkeleton
{
public:
	RawSkeletonInfo info;
	std::vector<SkeletonNode> nodes;
};

/// @brief Used to store all the skeleton data coming from the estimation system in Core.
class ClientRawSkeletonCollection
{
public:
	std::vector<ClientRawSkeleton> skeletons;
};

/// One untouched MANUS RawSkeleton stream sample plus timestamps captured at
/// callback entry.  The collection may contain both gloves for the same SDK
/// publish event.
class ClientRawSkeletonFrame
{
public:
	ClientRawSkeletonCollection data;
	uint64_t sequence = 0;
	uint64_t callbackSequence = 0;
	uint64_t callbacksCoalescedIntoFrame = 0;
	uint64_t manusPublishTimeRaw = 0;
	uint64_t manusPublishTimeUnixNs = 0;
	bool hasManusPublishTimeUnixNs = false;
	uint64_t callbackSystemTimeUnixNs = 0;
	uint64_t callbackSteadyTimeNs = 0;
};

class SDKMinimalClient : public SDKClientPlatformSpecific
{
public:
	SDKMinimalClient();
	~SDKMinimalClient();
	ClientReturnCode Initialize();
	ClientReturnCode InitializeSDK();
	ClientReturnCode ShutDown();
	ClientReturnCode RegisterAllCallbacks();
	void Run();

	void PrintRawSkeletonNodeInfo();

	static void OnRawSkeletonStreamCallback(const SkeletonStreamInfo* const p_RawSkeletonStreamInfo);

	// Add: Socket related functions
	bool InitializeSocket();
	void CloseSocket();
	void SendSkeletonData(const ClientRawSkeletonCollection* data);
	std::string SkeletonToJSON(const ClientRawSkeletonCollection* data);
	std::string RawSkeletonFrameToJSON(const ClientRawSkeletonFrame& frame);
	void EnqueueRawSkeletonFrame(std::unique_ptr<ClientRawSkeletonFrame> frame);
	void RawSkeletonSenderLoop();
	void StartRawSkeletonSender();
	void StopRawSkeletonSender();
	bool SendAll(const std::string& data);
	bool RecorderRequestedStop();

protected:

	ClientReturnCode Connect();

	static SDKMinimalClient* s_Instance;
	std::atomic<bool> m_Running{ true };
	bool m_PrintedNodeInfo = false;

	ConnectionType m_ConnectionType = ConnectionType::ConnectionType_Invalid;

	std::mutex m_RawSkeletonMutex;
	ClientRawSkeletonCollection* m_NextRawSkeleton = nullptr;
	ClientRawSkeletonCollection* m_RawSkeleton = nullptr;

	uint32_t m_FrameCounter = 0;

	// The SDK callback only copies and enqueues data. The sender selects the
	// latest callback in each configured output interval, so JSON and TCP never block the
	// callback and redundant Core publish events do not inflate the output rate.
	std::mutex m_RawSkeletonQueueMutex;
	std::condition_variable m_RawSkeletonQueueCondition;
	std::deque<std::unique_ptr<ClientRawSkeletonFrame>> m_RawSkeletonQueue;
	std::thread m_RawSkeletonSenderThread;
	std::atomic<bool> m_RawSkeletonSenderRunning{ false };
	std::atomic<uint64_t> m_RawSkeletonSequence{ 0 };
	std::atomic<uint64_t> m_RawSkeletonOutputSequence{ 0 };
	std::atomic<uint64_t> m_RawSkeletonQueued{ 0 };
	std::atomic<uint64_t> m_RawSkeletonSent{ 0 };
	std::atomic<uint64_t> m_RawSkeletonCoalesced{ 0 };
	std::atomic<uint64_t> m_RawSkeletonDropped{ 0 };
	static constexpr size_t RAW_SKELETON_QUEUE_CAPACITY = 8192;
	// Keep 120 Hz as the normal source-build default.  A second executable can
	// be built with /DRAW_SKELETON_OUTPUT_HZ_VALUE=60 without maintaining a
	// divergent source copy.
#ifndef RAW_SKELETON_OUTPUT_HZ_VALUE
#define RAW_SKELETON_OUTPUT_HZ_VALUE 120
#endif
	static constexpr uint64_t RAW_SKELETON_OUTPUT_HZ = RAW_SKELETON_OUTPUT_HZ_VALUE;
	static_assert(RAW_SKELETON_OUTPUT_HZ > 0, "RawSkeleton output rate must be positive");
	static constexpr uint64_t RAW_SKELETON_OUTPUT_PERIOD_NS =
		1000000000ULL / RAW_SKELETON_OUTPUT_HZ;

	// Add: Socket related member variables
	std::mutex m_SocketSendMutex;
	std::string m_ControlReceiveBuffer;
	SOCKET_TYPE m_ClientSocket = INVALID_SOCKET_VAL;
	bool m_SocketInitialized = false;
#ifdef _WIN32
	bool m_WinsockInitialized = false;
#endif
	std::string m_PythonHost = "127.0.0.1";
	int m_PythonPort = 8888;
};

// Close the Doxygen group.
/** @} */
#endif
