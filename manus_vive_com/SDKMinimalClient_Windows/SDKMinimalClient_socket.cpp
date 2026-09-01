// SDKMinimalClient_socket_fixed.cpp : Fixed version with proper encoding and bracket matching
//

#include "SDKMinimalClient.hpp"
#include "ManusSDKTypes.h"
#include <fstream>
#include <iostream>
#include <thread>
#include <sstream>
#include <iomanip>
#include <chrono>
#include <ctime>
#ifdef _WIN32
#include <conio.h>
#endif

#include "ClientLogging.hpp"

using ManusSDK::ClientLog;

SDKMinimalClient* SDKMinimalClient::s_Instance = nullptr;

namespace
{
	uint64_t SystemTimeUnixNs()
	{
		return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
			std::chrono::system_clock::now().time_since_epoch()).count());
	}

	uint64_t SteadyTimeNs()
	{
		return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
			std::chrono::steady_clock::now().time_since_epoch()).count());
	}

	bool ManusTimestampToUnixNs(const ManusTimestamp timestamp, uint64_t& unixNs)
	{
		ManusTimestampInfo info;
		ManusTimestampInfo_Init(&info);
		if (CoreSdk_GetTimestampInfo(timestamp, &info) != SDKReturnCode::SDKReturnCode_Success || info.timecode)
		{
			return false;
		}
		std::tm utc{};
		utc.tm_year = static_cast<int>(info.year) - 1900;
		utc.tm_mon = static_cast<int>(info.month) - 1;
		utc.tm_mday = static_cast<int>(info.day);
		utc.tm_hour = static_cast<int>(info.hour);
		utc.tm_min = static_cast<int>(info.minute);
		utc.tm_sec = static_cast<int>(info.second);
#ifdef _WIN32
		const __time64_t seconds = _mkgmtime64(&utc);
#else
		const time_t seconds = timegm(&utc);
#endif
		if (seconds < 0) return false;
		unixNs = static_cast<uint64_t>(seconds) * 1000000000ULL
			+ static_cast<uint64_t>(info.fraction) * 1000000ULL;
		return true;
	}

}

int main()
{
    ClientLog::print("Starting minimal client!");
    SDKMinimalClient t_Client;
	auto t_Response = t_Client.Initialize();
	if (t_Response != ClientReturnCode::ClientReturnCode_Success)
	{
		ClientLog::error("Failed to initialize the SDK. Are you sure the correct ManusSDKLibary is used?");
		return -1;
	}
    ClientLog::print("minimal client is initialized.");

    // SDK is setup. so now go to main loop of the program.
    t_Client.Run();

    // loop is over. disconnect it all
    ClientLog::print("minimal client is done, shutting down.");
    t_Client.ShutDown();
}

SDKMinimalClient::SDKMinimalClient()
{
	s_Instance = this;
}

SDKMinimalClient::~SDKMinimalClient()
{
	s_Instance = nullptr;
	StopRawSkeletonSender();
	CloseSocket();
}

/// @brief Initialize the sample console and the SDK.
/// This function attempts to resize the console window and then proceeds to initialize the SDK's interface.
ClientReturnCode SDKMinimalClient::Initialize()
{
	if (!PlatformSpecificInitialization())
	{
		return ClientReturnCode::ClientReturnCode_FailedPlatformSpecificInitialization;
	}

	// Initialize Socket connection
	if (!InitializeSocket()) {
		ClientLog::error("Failed to connect to capture_recorder.py. Start the recorder before this client.");
		return ClientReturnCode::ClientReturnCode_FailedToInitialize;
	}

	const ClientReturnCode t_IntializeResult = InitializeSDK();
	if (t_IntializeResult != ClientReturnCode::ClientReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToInitialize;
	}

	return ClientReturnCode::ClientReturnCode_Success;
}

/// @brief Initialize the sdk, register the callbacks and set the coordinate system.
/// This needs to be done before any of the other SDK functions can be used.
ClientReturnCode SDKMinimalClient::InitializeSDK()
{
	ClientLog::print("Select what mode you would like to start in (and press enter to submit)");
	ClientLog::print("[1] Core Integrated - This will run standalone without the need for a MANUS Core connection");
	ClientLog::print("[2] Core Local - This will connect to a MANUS Core running locally on your machine");
	ClientLog::print("[3] Core Remote - This will search for a MANUS Core running locally on your network");
	std::string t_ConnectionTypeInput;
	std::cin >> t_ConnectionTypeInput;

	switch (t_ConnectionTypeInput[0])
	{
		case '1':
			m_ConnectionType = ConnectionType::ConnectionType_Integrated;
			break;
		case '2':
			m_ConnectionType = ConnectionType::ConnectionType_Local;
			break;
		case '3':
			m_ConnectionType = ConnectionType::ConnectionType_Remote;
			break;
		default:
			m_ConnectionType = ConnectionType::ConnectionType_Invalid;
			ClientLog::print("Invalid input, try again");
			return InitializeSDK();
	}

	// Invalid connection type detected
	if (m_ConnectionType == ConnectionType::ConnectionType_Invalid
		|| m_ConnectionType == ConnectionType::ClientState_MAX_CLIENT_STATE_SIZE)
		return ClientReturnCode::ClientReturnCode_FailedToInitialize;

	// before we can use the SDK, some internal SDK bits need to be initialized.
	// MANUS Core SDK 2.4 uses the unified initializer.  Local and remote Core
	// connections are remote SDK sessions; integrated mode is not.
	const bool t_Remote = m_ConnectionType != ConnectionType::ConnectionType_Integrated;
	const SDKReturnCode t_InitializeResult =
		CoreSdk_Initialize(SessionType::SessionType_CoreSDK, t_Remote);
	if (t_InitializeResult != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToInitialize;
	}

	const ClientReturnCode t_CallBackResults = RegisterAllCallbacks();
	if (t_CallBackResults != ::ClientReturnCode::ClientReturnCode_Success)
	{
		return t_CallBackResults;
	}

	// after everything is registered and initialized
	// We specify the coordinate system in which we want to receive the data.
	// (each client can have their own settings. unreal and unity for instance use different coordinate systems)
	// if this is not set, the SDK will not function.
	// The coordinate system used for this example is z-up, x-positive, right-handed and in meter scale.
	CoordinateSystemVUH t_VUH;
	CoordinateSystemVUH_Init(&t_VUH);
	t_VUH.handedness = Side::Side_Right;
	t_VUH.up = AxisPolarity::AxisPolarity_PositiveZ;
	t_VUH.view = AxisView::AxisView_XFromViewer;
	t_VUH.unitScale = 1.0f; //1.0 is meters, 0.01 is cm, 0.001 is mm.

	// The above specified coordinate system is used to initialize and the coordinate space is specified (world vs local).
	const SDKReturnCode t_CoordinateResult = CoreSdk_InitializeCoordinateSystemWithVUH(t_VUH, true);

	/* this is an example of an alternative way of setting up the coordinate system instead of VUH (view, up, handedness)
	CoordinateSystemDirection t_Direction;
	t_Direction.x = AxisDirection::AD_Right;
	t_Direction.y = AxisDirection::AD_Up;
	t_Direction.z = AxisDirection::AD_Forward;
	const SDKReturnCode t_InitializeResult = CoreSdk_InitializeCoordinateSystemWithDirection(t_Direction, true);
	*/

	if (t_CoordinateResult != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToInitialize;
	}

	return ClientReturnCode::ClientReturnCode_Success;
}

/// @brief When shutting down the application, it's important to clean up after the SDK and call it's shutdown function.
/// this will close all connections to the host, close any threads.
/// after this is called it is expected to exit the client program. If not you would need to reinitalize the SDK.
ClientReturnCode SDKMinimalClient::ShutDown()
{
	// Stop SDK callbacks first, then drain the recording queue before closing
	// the socket. This preserves the tail of a capture session.
	const SDKReturnCode t_Result = CoreSdk_ShutDown();
	StopRawSkeletonSender();
	CloseSocket();
	if (t_Result != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToShutDownSDK;
	}

	return ClientReturnCode::ClientReturnCode_Success;
}

// ==================== Socket related functions ====================

bool SDKMinimalClient::InitializeSocket() {
#ifdef _WIN32
	// Windows: Initialize Winsock
	WSADATA wsaData;
	int result = WSAStartup(MAKEWORD(2, 2), &wsaData);
	if (result != 0) {
		ClientLog::error("WSAStartup failed: {}", result);
		return false;
	}
	m_WinsockInitialized = true;
#endif

	// Create Socket
	m_ClientSocket = socket(AF_INET, SOCK_STREAM, 0);
	if (m_ClientSocket == INVALID_SOCKET_VAL) {
		ClientLog::error("Socket creation failed");
#ifdef _WIN32
		WSACleanup();
		m_WinsockInitialized = false;
#endif
		return false;
	}

	// Set server address
	struct sockaddr_in serverAddr;
	serverAddr.sin_family = AF_INET;
	serverAddr.sin_port = htons(m_PythonPort);

#ifdef _WIN32
	serverAddr.sin_addr.s_addr = inet_addr(m_PythonHost.c_str());
#else
	inet_pton(AF_INET, m_PythonHost.c_str(), &serverAddr.sin_addr);
#endif

	// Connect to Python server
	ClientLog::print("Connecting to Python server at {}:{}...", m_PythonHost, m_PythonPort);

	int connectResult = connect(m_ClientSocket, (struct sockaddr*)&serverAddr, sizeof(serverAddr));
	if (connectResult < 0) {
		ClientLog::warn("Failed to connect to Python server. Make sure Python script is running.");
		CLOSE_SOCKET(m_ClientSocket);
		m_ClientSocket = INVALID_SOCKET_VAL;
#ifdef _WIN32
		WSACleanup();
		m_WinsockInitialized = false;
#endif
		return false;
	}

	ClientLog::print("Connected to Python server successfully!");
	m_SocketInitialized = true;
	StartRawSkeletonSender();
	return true;
}

void SDKMinimalClient::CloseSocket() {
	if (m_ClientSocket != INVALID_SOCKET_VAL) {
		CLOSE_SOCKET(m_ClientSocket);
		m_ClientSocket = INVALID_SOCKET_VAL;
		ClientLog::print("Socket connection closed.");
	}

#ifdef _WIN32
	if (m_WinsockInitialized) {
		WSACleanup();
		m_WinsockInitialized = false;
	}
#endif

	m_SocketInitialized = false;
}

void SDKMinimalClient::StartRawSkeletonSender()
{
	if (m_RawSkeletonSenderRunning.exchange(true)) return;
	m_RawSkeletonSenderThread = std::thread(&SDKMinimalClient::RawSkeletonSenderLoop, this);
}

void SDKMinimalClient::StopRawSkeletonSender()
{
	const bool wasRunning = m_RawSkeletonSenderRunning.exchange(false);
	m_RawSkeletonQueueCondition.notify_all();
	const bool wasJoinable = m_RawSkeletonSenderThread.joinable();
	if (wasJoinable) m_RawSkeletonSenderThread.join();
	{
		std::lock_guard<std::mutex> lock(m_RawSkeletonQueueMutex);
		m_RawSkeletonDropped.fetch_add(static_cast<uint64_t>(m_RawSkeletonQueue.size()));
		m_RawSkeletonQueue.clear();
	}
	if (wasRunning || wasJoinable)
	{
		ClientLog::print(
			"RawSkeleton capture summary: callbacks={}, queued={}, sent_at_{}_hz={}, coalesced={}, dropped={}.",
			m_RawSkeletonSequence.load(), m_RawSkeletonQueued.load(), RAW_SKELETON_OUTPUT_HZ, m_RawSkeletonSent.load(),
			m_RawSkeletonCoalesced.load(), m_RawSkeletonDropped.load());
	}
}

bool SDKMinimalClient::SendAll(const std::string& data)
{
	std::lock_guard<std::mutex> sendLock(m_SocketSendMutex);
	size_t totalSent = 0;
	while (totalSent < data.size())
	{
		const int sent = send(
			m_ClientSocket,
			data.data() + totalSent,
			static_cast<int>(data.size() - totalSent),
			0);
		if (sent <= 0) return false;
		totalSent += static_cast<size_t>(sent);
	}
	return true;
}

bool SDKMinimalClient::RecorderRequestedStop()
{
	if (!m_SocketInitialized || m_ClientSocket == INVALID_SOCKET_VAL) return true;
	fd_set readSet;
	FD_ZERO(&readSet);
	FD_SET(m_ClientSocket, &readSet);
	timeval timeout{};
#ifdef _WIN32
	const int ready = select(0, &readSet, nullptr, nullptr, &timeout);
#else
	const int ready = select(m_ClientSocket + 1, &readSet, nullptr, nullptr, &timeout);
#endif
	if (ready < 0) return true;
	if (ready == 0) return false;

	char buffer[256];
	const int received = recv(m_ClientSocket, buffer, static_cast<int>(sizeof(buffer)), 0);
	if (received <= 0) return true;
	m_ControlReceiveBuffer.append(buffer, static_cast<size_t>(received));

	size_t newline = std::string::npos;
	while ((newline = m_ControlReceiveBuffer.find('\n')) != std::string::npos)
	{
		const std::string command = m_ControlReceiveBuffer.substr(0, newline);
		m_ControlReceiveBuffer.erase(0, newline + 1);
		if (command.find("\"command\":\"stop\"") != std::string::npos) return true;
	}
	return false;
}

void SDKMinimalClient::EnqueueRawSkeletonFrame(std::unique_ptr<ClientRawSkeletonFrame> frame)
{
	{
		std::lock_guard<std::mutex> lock(m_RawSkeletonQueueMutex);
		if (m_RawSkeletonQueue.size() >= RAW_SKELETON_QUEUE_CAPACITY)
		{
			++m_RawSkeletonDropped;
			return;
		}
		m_RawSkeletonQueue.emplace_back(std::move(frame));
		++m_RawSkeletonQueued;
	}
	m_RawSkeletonQueueCondition.notify_one();
}

void SDKMinimalClient::RawSkeletonSenderLoop()
{
	std::unique_ptr<ClientRawSkeletonFrame> pendingFrame;
	uint64_t callbacksCoalescedIntoPending = 0;
	const auto outputPeriod = std::chrono::nanoseconds(RAW_SKELETON_OUTPUT_PERIOD_NS);
	auto nextOutputTime = std::chrono::steady_clock::now();

	auto sendFrame = [this](std::unique_ptr<ClientRawSkeletonFrame>& frame,
		const uint64_t coalescedCallbacks) -> bool
	{
		frame->sequence = m_RawSkeletonOutputSequence.fetch_add(1);
		frame->callbacksCoalescedIntoFrame = coalescedCallbacks;
		std::string message = RawSkeletonFrameToJSON(*frame);
		message.push_back('\n');
		if (!m_SocketInitialized || m_ClientSocket == INVALID_SOCKET_VAL || !SendAll(message))
		{
			ClientLog::error("RawSkeleton TCP stream disconnected; stopping capture sender.");
			++m_RawSkeletonDropped;
			m_RawSkeletonSenderRunning.store(false);
			m_Running.store(false);
			return false;
		}
		++m_RawSkeletonSent;
		return true;
	};

	while (true)
	{
		std::unique_ptr<ClientRawSkeletonFrame> frame;
		uint64_t coalescedForFrame = 0;
		bool finished = false;
		{
			std::unique_lock<std::mutex> lock(m_RawSkeletonQueueMutex);
			while (true)
			{
				while (!m_RawSkeletonQueue.empty())
				{
					if (pendingFrame)
					{
						++callbacksCoalescedIntoPending;
						++m_RawSkeletonCoalesced;
					}
					pendingFrame = std::move(m_RawSkeletonQueue.front());
					m_RawSkeletonQueue.pop_front();
				}

				const bool stopping = !m_RawSkeletonSenderRunning.load();
				const auto now = std::chrono::steady_clock::now();
				if (pendingFrame && (stopping || now >= nextOutputTime))
				{
					frame = std::move(pendingFrame);
					coalescedForFrame = callbacksCoalescedIntoPending;
					callbacksCoalescedIntoPending = 0;
					break;
				}
				if (stopping)
				{
					finished = true;
					break;
				}
				if (pendingFrame)
				{
					m_RawSkeletonQueueCondition.wait_until(lock, nextOutputTime);
				}
				else
				{
					m_RawSkeletonQueueCondition.wait(lock);
				}
			}
		}

		if (finished) break;
		if (!sendFrame(frame, coalescedForFrame)) break;
		const auto now = std::chrono::steady_clock::now();
		do
		{
			nextOutputTime += outputPeriod;
		}
		while (nextOutputTime <= now);
	}
}

std::string SDKMinimalClient::RawSkeletonFrameToJSON(const ClientRawSkeletonFrame& frame)
{
	std::stringstream json;
	json << std::fixed << std::setprecision(9);
	json << "{";
	json << "\"schema_version\":1,";
	json << "\"stream\":\"manus_raw_skeleton\",";
	json << "\"sequence\":" << frame.sequence << ",";
	json << "\"callback_sequence\":" << frame.callbackSequence << ",";
	json << "\"callbacks_coalesced_into_frame\":" << frame.callbacksCoalescedIntoFrame << ",";
	json << "\"output_rate_hz\":" << RAW_SKELETON_OUTPUT_HZ << ",";
	json << "\"manus_publish_time_raw\":" << frame.manusPublishTimeRaw << ",";
	json << "\"manus_publish_time_unix_ns\":";
	if (frame.hasManusPublishTimeUnixNs) json << frame.manusPublishTimeUnixNs;
	else json << "null";
	json << ",";
	json << "\"callback_system_time_unix_ns\":" << frame.callbackSystemTimeUnixNs << ",";
	json << "\"callback_steady_time_ns\":" << frame.callbackSteadyTimeNs << ",";
	json << "\"serialize_system_time_unix_ns\":" << SystemTimeUnixNs() << ",";
	json << "\"skeletons\":[";
	for (size_t skeletonIndex = 0; skeletonIndex < frame.data.skeletons.size(); ++skeletonIndex)
	{
		const auto& skeleton = frame.data.skeletons[skeletonIndex];
		json << "{";
		json << "\"glove_id\":" << skeleton.info.gloveId << ",";
		json << "\"nodes\":[";
		for (size_t nodeIndex = 0; nodeIndex < skeleton.nodes.size(); ++nodeIndex)
		{
			const auto& transform = skeleton.nodes[nodeIndex].transform;
			json << "{";
			json << "\"node_id\":" << nodeIndex << ",";
			json << "\"position\":[" << transform.position.x << "," << transform.position.y << "," << transform.position.z << "],";
			json << "\"quaternion_xyzw\":[" << transform.rotation.x << "," << transform.rotation.y << "," << transform.rotation.z << "," << transform.rotation.w << "]";
			json << "}";
			if (nodeIndex + 1 < skeleton.nodes.size()) json << ",";
		}
		json << "]}";
		if (skeletonIndex + 1 < frame.data.skeletons.size()) json << ",";
	}
	json << "]}";
	return json.str();
}

std::string SDKMinimalClient::SkeletonToJSON(const ClientRawSkeletonCollection* data) {
	if (!data || data->skeletons.empty()) {
		return "{}";
	}

	std::stringstream json;
	json << std::fixed << std::setprecision(6);

	// Get current timestamp (milliseconds)
	auto now = std::chrono::system_clock::now();
	auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();

	json << "{";
	json << "\"timestamp\":" << ms << ",";
	json << "\"frame\":" << m_FrameCounter << ",";
	json << "\"skeletons\":[";

	for (size_t skeletonIdx = 0; skeletonIdx < data->skeletons.size(); skeletonIdx++) {
		const auto& skeleton = data->skeletons[skeletonIdx];

		json << "{";
		json << "\"gloveId\":\"" << std::hex << skeleton.info.gloveId << std::dec << "\",";
		// handType field removed - not available in RawSkeletonInfo structure
		json << "\"nodes\":[";

		for (size_t nodeIdx = 0; nodeIdx < skeleton.nodes.size(); nodeIdx++) {
			const auto& node = skeleton.nodes[nodeIdx];
			const auto& pos = node.transform.position;
			const auto& rot = node.transform.rotation;

			json << "{";
			json << "\"id\":" << nodeIdx << ",";
			json << "\"position\":[" << pos.x << "," << pos.y << "," << pos.z << "],";
			json << "\"rotation\":[" << rot.x << "," << rot.y << "," << rot.z << "," << rot.w << "]";

			if (nodeIdx < skeleton.nodes.size() - 1) {
				json << "},";
			} else {
				json << "}";
			}
		}

		json << "]";

		if (skeletonIdx < data->skeletons.size() - 1) {
			json << "},";
		} else {
			json << "}";
		}
	}

	json << "]";
	json << "}";

	return json.str();
}

void SDKMinimalClient::SendSkeletonData(const ClientRawSkeletonCollection* data) {
	if (m_ClientSocket == INVALID_SOCKET_VAL || !m_SocketInitialized) {
		return; // Socket not initialized, do not send data
	}

	try {
		std::string jsonData = SkeletonToJSON(data);

		// Add newline as message separator
		jsonData += "\n";

		// Send data
		int bytesSent = send(m_ClientSocket, jsonData.c_str(), static_cast<int>(jsonData.length()), 0);

		if (bytesSent < 0) {
			// Send failed, connection may be broken
			ClientLog::warn("Failed to send data to Python. Socket may be disconnected.");
			CloseSocket();
		}
	} catch (const std::exception& e) {
		ClientLog::error("Error sending skeleton data: {}", e.what());
	}
}

// ==================== Modified callback functions ====================

/// @brief This gets called when the client is connected and there is glove data available.
/// @param p_RawSkeletonStreamInfo contains the meta data on what data is available and needs to be retrieved from the SDK.
/// The data is not directly passed to the callback, but needs to be retrieved from the SDK for it to be used. This is demonstrated in the function below.
void SDKMinimalClient::OnRawSkeletonStreamCallback(const SkeletonStreamInfo* const p_RawSkeletonStreamInfo)
{
	if (s_Instance)
	{
		// Capture both host clocks immediately on callback entry. system_clock is
		// close to Windows UTC; steady_clock is monotonic and suitable for deltas.
		auto frame = std::make_unique<ClientRawSkeletonFrame>();
		frame->callbackSequence = s_Instance->m_RawSkeletonSequence.fetch_add(1);
		frame->manusPublishTimeRaw = p_RawSkeletonStreamInfo->publishTime.time;
		frame->hasManusPublishTimeUnixNs = ManusTimestampToUnixNs(
			p_RawSkeletonStreamInfo->publishTime, frame->manusPublishTimeUnixNs);
		frame->callbackSystemTimeUnixNs = SystemTimeUnixNs();
		frame->callbackSteadyTimeNs = SteadyTimeNs();
		if (p_RawSkeletonStreamInfo->skeletonsCount == 0)
		{
			++s_Instance->m_RawSkeletonDropped;
			return;
		}
		frame->data.skeletons.resize(p_RawSkeletonStreamInfo->skeletonsCount);

		for (uint32_t i = 0; i < p_RawSkeletonStreamInfo->skeletonsCount; i++)
		{
			//Retrieves info on the skeletonData, like deviceID and the amount of nodes.
			const SDKReturnCode infoResult = CoreSdk_GetRawSkeletonInfo(i, &frame->data.skeletons[i].info);
			if (infoResult != SDKReturnCode::SDKReturnCode_Success)
			{
				++s_Instance->m_RawSkeletonDropped;
				return;
			}
			frame->data.skeletons[i].nodes.resize(frame->data.skeletons[i].info.nodesCount);
			frame->data.skeletons[i].info.publishTime = p_RawSkeletonStreamInfo->publishTime;

			//Retrieves the skeletonData, which contains the node data.
			const SDKReturnCode dataResult = CoreSdk_GetRawSkeletonData(
				i, frame->data.skeletons[i].nodes.data(), frame->data.skeletons[i].info.nodesCount);
			if (dataResult != SDKReturnCode::SDKReturnCode_Success)
			{
				++s_Instance->m_RawSkeletonDropped;
				return;
			}
		}

		s_Instance->EnqueueRawSkeletonFrame(std::move(frame));
	}
}

/// @brief Used to register all the stream callbacks.
/// Callbacks that are registered functions that get called when a certain 'event' happens, such as data coming in.
/// All of these are optional, but depending on what data you require you may or may not need all of them. For this example we only implement the raw skeleton data.
ClientReturnCode SDKMinimalClient::RegisterAllCallbacks()
{
	// Register the callback to receive Raw Skeleton data
	// it is optional, but without it you can not see any resulting skeleton data.
	// see OnRawSkeletonStreamCallback for more details.
	const SDKReturnCode t_RegisterRawSkeletonCallbackResult = CoreSdk_RegisterCallbackForRawSkeletonStream(*OnRawSkeletonStreamCallback);
	if (t_RegisterRawSkeletonCallbackResult != SDKReturnCode::SDKReturnCode_Success)
	{
		ClientLog::error("Failed to register callback function for processing raw skeletal data from Manus Core. The value returned was {}.", (int32_t)t_RegisterRawSkeletonCallbackResult);
		return ClientReturnCode::ClientReturnCode_FailedToInitialize;
	}
	return ClientReturnCode::ClientReturnCode_Success;
}

/// @brief main loop
void SDKMinimalClient::Run()
{
	// first loop until we get a connection
	m_ConnectionType == ConnectionType::ConnectionType_Integrated ?
		ClientLog::print("minimal client is running in integrated mode.") :
		ClientLog::print("minimal client is connecting to MANUS Core. (make sure it is running)");

	while (Connect() != ClientReturnCode::ClientReturnCode_Success)
	{
		// not yet connected. wait
		ClientLog::print("minimal client could not connect.trying again in a second.");
		std::this_thread::sleep_for(std::chrono::milliseconds(1000));
	}

	if (m_ConnectionType != ConnectionType::ConnectionType_Integrated)
		ClientLog::print("minimal client is connected, setting up skeletons.");
	ClientLog::print("Press SPACE or Q to stop capture.");

	// Match the SDK 2.4 minimal-client behavior. Without an external tracker,
	// Auto still produces the hand pose using the glove's IMU rotation.
	const SDKReturnCode t_HandMotionResult = CoreSdk_SetRawSkeletonHandMotion(HandMotion_Auto);
	if (t_HandMotionResult != SDKReturnCode::SDKReturnCode_Success)
	{
		ClientLog::error("Failed to set hand motion mode. The value returned was {}.", (int32_t)t_HandMotionResult);
	}

	while (m_Running)
	{
		if (RecorderRequestedStop())
		{
			ClientLog::print("Recorder requested stop; stopping SDK callbacks and draining queued frames.");
			m_Running.store(false);
			break;
		}
		std::this_thread::sleep_for(std::chrono::milliseconds(10));
		static uint64_t lastReported = 0;
		const uint64_t sent = m_RawSkeletonSent.load();
		if (sent >= lastReported + 600)
		{
			lastReported = sent;
			size_t queueDepth = 0;
			{
				std::lock_guard<std::mutex> lock(m_RawSkeletonQueueMutex);
				queueDepth = m_RawSkeletonQueue.size();
			}
			ClientLog::print("RawSkeleton frames sent: {}, queue depth: {}, dropped: {}.",
				sent, queueDepth, m_RawSkeletonDropped.load());
		}

		bool stopRequested = false;
#ifdef _WIN32
		// _kbhit/_getch works in Windows pseudoconsoles such as the VS Code
		// integrated terminal; GetAsyncKeyState requires a focused native window.
		if (_kbhit())
		{
			const int key = _getch();
			stopRequested = key == ' ' || key == 'q' || key == 'Q' || key == 27;
		}
#else
		stopRequested = GetKeyDown(' ');
#endif
		if (stopRequested) m_Running.store(false);
	}
}

// ==================== Other necessary functions ====================

void SDKMinimalClient::PrintRawSkeletonNodeInfo()
{
	if (m_RawSkeleton == nullptr || m_RawSkeleton->skeletons.size() == 0)
	{
		return;
	}

	// Always show some node data, even if already printed node info
	if (m_RawSkeleton->skeletons[0].nodes.size() != 0) {
		size_t nodeCount = m_RawSkeleton->skeletons[0].nodes.size();

		// Show socket status and data being sent
		if (m_SocketInitialized) {
			ClientLog::print("[SOCKET] Sending {} nodes to Python...", nodeCount);
		}

		// Always print node 0
		ManusVec3 t_Pos = m_RawSkeleton->skeletons[0].nodes[0].transform.position;
		ManusQuaternion t_Rot = m_RawSkeleton->skeletons[0].nodes[0].transform.rotation;
		ClientLog::print("Node 0 Position: x {} y {} z {} Rotation: x {} y {} z {} w {}", t_Pos.x, t_Pos.y, t_Pos.z, t_Rot.x, t_Rot.y, t_Rot.z, t_Rot.w);

		// Print additional node data every 10 frames
		static int frameCounter = 0;
		frameCounter++;

		if (frameCounter % 10 == 0) {
			// Print a few more nodes
			size_t nodesToShow = std::min<size_t>(5, nodeCount);
			ClientLog::print("[DATA] Current frame node data sample:");
			for (size_t i = 0; i < nodesToShow; i++) {
				const auto& node = m_RawSkeleton->skeletons[0].nodes[i];
				ClientLog::print("  Node {}: position [{}, {}, {}]", i, node.transform.position.x, node.transform.position.y, node.transform.position.z);
			}

			if (nodeCount >= 25) {
				ClientLog::print("  Thumb tip (node 24): position [{}, {}, {}]",
					m_RawSkeleton->skeletons[0].nodes[24].transform.position.x,
					m_RawSkeleton->skeletons[0].nodes[24].transform.position.y,
					m_RawSkeleton->skeletons[0].nodes[24].transform.position.z);
			}
		}
	}

	// If we've already printed the full node info, return early
	if (m_PrintedNodeInfo) {
		return;
	}

	// this section demonstrates how to interpret the raw skeleton data.
	// how to get the hierarchy of the skeleton, and how to know bone each node represents.

	uint32_t t_GloveId = 0;
	uint32_t t_NodeCount = 0;

	t_GloveId = m_RawSkeleton->skeletons[0].info.gloveId;
	t_NodeCount = 0;

	SDKReturnCode t_Result = CoreSdk_GetRawSkeletonNodeCount(t_GloveId, t_NodeCount);
	if (t_Result != SDKReturnCode::SDKReturnCode_Success)
	{
		ClientLog::error("Failed to get Raw Skeleton Node Count. The error given was {}.", (int32_t)t_Result);
		return;
	}

	// now get the hierarchy data, this needs to be used to reconstruct the positions of each node in case the user set up the system with a local coordinate system.
	// having a node position defined as local means that this will be related to its parent.

	NodeInfo* t_NodeInfo = new NodeInfo[t_NodeCount];
	t_Result = CoreSdk_GetRawSkeletonNodeInfoArray(t_GloveId, t_NodeInfo, t_NodeCount);
	if (t_Result != SDKReturnCode::SDKReturnCode_Success)
	{
		ClientLog::error("Failed to get Raw Skeleton Hierarchy. The error given was {}.", (int32_t)t_Result);
		return;
	}

	ClientLog::print("Received Skeleton glove data from Core. skeletons:{} first skeleton glove id:{}", m_RawSkeleton->skeletons.size(), m_RawSkeleton->skeletons[0].info.gloveId);
	ClientLog::print("Printing Node Info:");


	// prints the information for each node, the chain type will which part of the body it is. The finger joint type will be which bone of the finger it is.
	for (size_t i = 0; i < t_NodeCount; i++)
	{
		ClientLog::printWithPadding("Node ID: {} Side: {} ChainType: {} FingerJointType: {}, Parent Node ID: {}",2, std::to_string(t_NodeInfo[i].nodeId), t_NodeInfo[i].side, t_NodeInfo[i].chainType, t_NodeInfo[i].fingerJointType, std::to_string(t_NodeInfo[i].parentId));
	}

	delete[] t_NodeInfo;
	m_PrintedNodeInfo = true;
}

/// @brief the client will now try to connect to MANUS Core via the SDK when the ConnectionType is not integrated. These steps still need to be followed when using the integrated ConnectionType.
ClientReturnCode SDKMinimalClient::Connect()
{
	bool t_ConnectLocally = m_ConnectionType == ConnectionType::ConnectionType_Local;
	SDKReturnCode t_StartResult = CoreSdk_LookForHosts(1, t_ConnectLocally);
	if (t_StartResult != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToFindHosts;
	}

	uint32_t t_NumberOfHostsFound = 0;
	SDKReturnCode t_NumberResult = CoreSdk_GetNumberOfAvailableHostsFound(&t_NumberOfHostsFound);
	if (t_NumberResult != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToFindHosts;
	}

	if (t_NumberOfHostsFound == 0)
	{
		return ClientReturnCode::ClientReturnCode_FailedToFindHosts;
	}

	std::unique_ptr<ManusHost[]> t_AvailableHosts;
	t_AvailableHosts.reset(new ManusHost[t_NumberOfHostsFound]);

	SDKReturnCode t_HostsResult = CoreSdk_GetAvailableHostsFound(t_AvailableHosts.get(), t_NumberOfHostsFound);
	if (t_HostsResult != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToFindHosts;
	}

	uint32_t t_HostSelection = 0;
	if (!t_ConnectLocally && t_NumberOfHostsFound > 1)
	{
		ClientLog::print("Select which host you want to connect to (and press enter to submit)");
		for (size_t i = 0; i < t_NumberOfHostsFound; i++)
		{
			auto t_HostInfo = t_AvailableHosts[i];
			ClientLog::print("[{}] hostname: {}, IP address: {}, version {}.{}.{}", i + 1, t_HostInfo.hostName, t_HostInfo.ipAddress, t_HostInfo.manusCoreVersion.major, t_HostInfo.manusCoreVersion.minor, t_HostInfo.manusCoreVersion.patch);
		}
		uint32_t t_HostSelectionInput = 0;
		std::cin >> t_HostSelectionInput;
		if (t_HostSelectionInput <= 0 || t_HostSelectionInput > t_NumberOfHostsFound)
			return ClientReturnCode::ClientReturnCode_FailedToConnect;

		t_HostSelection = t_HostSelectionInput - 1;
	}

	SDKReturnCode t_ConnectResult = CoreSdk_ConnectToHost(t_AvailableHosts[t_HostSelection]);

	if (t_ConnectResult == SDKReturnCode::SDKReturnCode_NotConnected)
	{
		return ClientReturnCode::ClientReturnCode_FailedToConnect;
	}

	return ClientReturnCode::ClientReturnCode_Success;
}
