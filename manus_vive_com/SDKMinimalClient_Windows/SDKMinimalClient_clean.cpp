// SDKMinimalClient.cpp : This file contains the 'main' function. Program execution begins and ends there.
//

#include "SDKMinimalClient.hpp"
#include "ManusSDKTypes.h"
#include <fstream>
#include <iostream>
#include <thread>
#include <sstream>
#include <iomanip>
#include <chrono>

#include "ClientLogging.hpp"

using ManusSDK::ClientLog;

SDKMinimalClient* SDKMinimalClient::s_Instance = nullptr;

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

	// Add: Initialize Socket connection
	if (!InitializeSocket()) {
		ClientLog::warn("Failed to initialize socket connection to Python. Data will not be sent.");
		// Do not return error, allow program to continue (warning only)
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
	bool t_Remote = m_ConnectionType != ConnectionType::ConnectionType_Integrated;
	const SDKReturnCode t_InitializeResult = CoreSdk_Initialize(SessionType::SessionType_CoreSDK, t_Remote);
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
	// Add: Close Socket connection
	CloseSocket();

	const SDKReturnCode t_Result = CoreSdk_ShutDown();
	if (t_Result != SDKReturnCode::SDKReturnCode_Success)
	{
		return ClientReturnCode::ClientReturnCode_FailedToShutDownSDK;
	}

	return ClientReturnCode::ClientReturnCode_Success;
}

// ==================== Add: Socket function implementations ====================

bool SDKMinimalClient::InitializeSocket() {
#ifdef _WIN32
	// Windows: Initialize Winsock
	WSADATA wsaData;
	int result = WSAStartup(MAKEWORD(2, 2), &wsaData);
	if (result != 0) {
		ClientLog::error("WSAStartup failed: {}", result);
		return false;
	}
#endif

	// Create Socket
	m_ClientSocket = socket(AF_INET, SOCK_STREAM, 0);
	if (m_ClientSocket == INVALID_SOCKET_VAL) {
		ClientLog::error("Socket creation failed");
#ifdef _WIN32
		WSACleanup();
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
#endif
		return false;
	}

	ClientLog::print("Connected to Python server successfully!");
	m_SocketInitialized = true;
	return true;
}

void SDKMinimalClient::CloseSocket() {
	if (m_ClientSocket != INVALID_SOCKET_VAL) {
		CLOSE_SOCKET(m_ClientSocket);
		m_ClientSocket = INVALID_SOCKET_VAL;
		ClientLog::print("Socket connection closed.");
	}

#ifdef _WIN32
	WSACleanup();
#endif

	m_SocketInitialized = false;
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
		json << "\"handType\":" << static_cast<int>(skeleton.info.handType) << ",";
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
		return; // Socket not initialized, don't send data
	}

	try {
		std::string jsonData = SkeletonToJSON(data);

		// Add newline as message separator
		jsonData += "\n";

		// Send data
		int bytesSent = send(m_ClientSocket, jsonData.c_str(), static_cast<int>(jsonData.length()), 0);

		if (bytesSent < 0) {
			// Send failed, connection may be lost
			ClientLog::warn("Failed to send data to Python. Socket may be disconnected.");
			CloseSocket();
		}
	} catch (const std::exception& e) {
		ClientLog::error("Error sending skeleton data: {}", e.what());
	}
}

// ==================== Modify callback function ====================

/// @brief This gets called when the client is connected and there is glove data available.
/// @param p_RawSkeletonStreamInfo contains the meta data on what data is available and needs to be retrieved from the SDK.
/// The data is not directly passed to the callback, but needs to be retrieved from the SDK for it to be used. This is demonstrated in the function below.
void SDKMinimalClient::OnRawSkeletonStreamCallback(const SkeletonStreamInfo* const p_RawSkeletonStreamInfo)
{
	if (s_Instance)
	{
		ClientRawSkeletonCollection* t_NxtClientRawSkeleton = new ClientRawSkeletonCollection();
		t_NxtClientRawSkeleton->skeletons.resize(p_RawSkeletonStreamInfo->skeletonsCount);

		for (uint32_t i = 0; i < p_RawSkeletonStreamInfo->skeletonsCount; i++)
		{
			//Retrieves info on the skeletonData, like deviceID and the amount of nodes.
			CoreSdk_GetRawSkeletonInfo(i, &t_NxtClientRawSkeleton->skeletons[i].info);
			t_NxtClientRawSkeleton->skeletons[i].nodes.resize(t_NxtClientRawSkeleton->skeletons[i].info.nodesCount);
			t_NxtClientRawSkeleton->skeletons[i].info.publishTime = p_RawSkeletonStreamInfo->publishTime;

			//Retrieves the skeletonData, which contains the node data.
			CoreSdk_GetRawSkeletonData(i, t_NxtClientRawSkeleton->skeletons[i].nodes.data(), t_NxtClientRawSkeleton->skeletons[i].info.nodesCount);
		}

		// Add: Send data to Python
		s_Instance->SendSkeletonData(t_NxtClientRawSkeleton);

		s_Instance->m_RawSkeletonMutex.lock();
		if (s_Instance->m_NextRawSkeleton != nullptr) delete s_Instance->m_NextRawSkeleton;
		s_Instance->m_NextRawSkeleton = t_NxtClientRawSkeleton;
		s_Instance->m_RawSkeletonMutex.unlock();
	}
}

// ==================== Original functions remain unchanged ====================

// ... Original RegisterAllCallbacks, Run, PrintRawSkeletonNodeInfo, Connect functions
// These remain unchanged, just ensure header includes new member functions